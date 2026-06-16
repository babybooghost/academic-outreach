"""No fake placeholder emails, and an on-site search that accepts a pasted URL."""
import os
import tempfile
import unittest
from unittest import mock

from app.database import (create_access_key, get_connection, get_professor,
                          init_db, upsert_professor)
from app.models import Professor


class EmailSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "o.db")
        os.environ.update({"DB_PATH": self.db, "FLASK_SECRET_KEY": "t",
                           "LOG_DIR": self.tmp.name, "OUTPUT_DIR": self.tmp.name})
        init_db(self.db)
        conn = get_connection(self.db)
        self.kid = create_access_key(conn, "ao_es", "A", "user", "t")
        conn.close()
        self.ws = get_connection(self.db, workspace_id=self.kid)

    def tearDown(self):
        try: self.ws.close()
        except Exception: pass

    def _client(self):
        from app.web.app import create_app
        app = create_app(); app.config["TESTING"] = True
        c = app.test_client()
        with c.session_transaction() as s:
            s.update({"authenticated": True, "key_id": self.kid, "key_label": "A", "role": "user"})
        return c

    def test_save_without_email_stores_empty_not_placeholder(self):
        c = self._client()
        r = c.post("/finder/save", json={"professors": [
            {"name": "Dr. NoEmail", "university": "MIT", "field": "ML"}  # no email, no profile_url
        ]})
        self.assertTrue(r.get_json()["success"])
        rows = self.ws.execute("SELECT email, status FROM professors WHERE name = ?", ("Dr. NoEmail",)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "")                 # empty, not a fake address
        self.assertNotIn(".placeholder", rows[0]["email"])
        self.assertEqual(rows[0]["status"], "needs_email")

    def test_multiple_needs_email_professors_coexist(self):
        # Regression: empty emails must not collide under the unique index
        # (it's partial, WHERE email != '').
        c = self._client()
        r = c.post("/finder/save", json={"professors": [
            {"name": "Dr. A", "university": "MIT", "field": "ML"},
            {"name": "Dr. B", "university": "MIT", "field": "ML"},
            {"name": "Dr. C", "university": "Stanford", "field": "NLP"},
        ]})
        self.assertTrue(r.get_json()["success"], r.get_json())
        rows = self.ws.execute("SELECT email FROM professors").fetchall()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["email"] == "" for row in rows))

    def test_migration_blanks_multiple_placeholders(self):
        from app.database import _migrate_schema
        a = upsert_professor(self.ws, Professor(name="P1", email="", university="MIT", field="ML"))
        b = upsert_professor(self.ws, Professor(name="P2", email="", university="MIT", field="ML"))
        # Force legacy placeholders (bypass the partial index by dropping it first).
        self.ws.execute("DROP INDEX IF EXISTS ux_professors_ws_email")
        self.ws.execute("UPDATE professors SET email = ? WHERE id = ?", ("p1@mit.placeholder", a))
        self.ws.execute("UPDATE professors SET email = ? WHERE id = ?", ("p2@mit.placeholder", b))
        self.ws.commit()
        _migrate_schema(self.ws)   # must not raise, must blank both
        self.assertEqual(get_professor(self.ws, a).email, "")
        self.assertEqual(get_professor(self.ws, b).email, "")

    def test_find_email_with_pasted_url(self):
        pid = upsert_professor(self.ws, Professor(name="Dr. X", email="", university="MIT", field="ML", status="needs_email"))
        c = self._client()
        with mock.patch("app.enricher.find_professor_email", return_value="x@mit.edu") as m:
            r = c.post(f"/professors/{pid}/find-email", json={"url": "https://lab.mit.edu/people"})
        data = r.get_json()
        self.assertTrue(data["success"], data)
        self.assertEqual(data["email"], "x@mit.edu")
        m.assert_called_once()
        p = get_professor(self.ws, pid)
        self.assertEqual(p.email, "x@mit.edu")
        self.assertEqual(p.status, "new")
        self.assertEqual(p.profile_url, "https://lab.mit.edu/people")  # remembered

    def test_find_email_web_search_fallback(self):
        # No URL anywhere -> the route searches the web for the page, then scrapes it.
        pid = upsert_professor(self.ws, Professor(name="Dr. Web", email="", university="MIT",
                                                  field="ML", status="needs_email"))
        c = self._client()
        with mock.patch("app.enricher._search_faculty_page", return_value="https://mit.edu/~web") as s, \
             mock.patch("app.enricher.find_professor_email", wraps=__import__("app.enricher", fromlist=["find_professor_email"]).find_professor_email) as _, \
             mock.patch("app.enricher.extract_email_from_html", return_value="web@mit.edu"), \
             mock.patch("app.enricher._is_allowed_by_robots", return_value=True), \
             mock.patch("app.enricher.requests.get") as g:
            g.return_value = mock.Mock(status_code=200, text="<html>web@mit.edu</html>", raise_for_status=lambda: None)
            r = c.post(f"/professors/{pid}/find-email", json={})
        data = r.get_json()
        self.assertTrue(data["success"], data)
        self.assertEqual(data["email"], "web@mit.edu")
        s.assert_called()  # the web-search fallback ran

    def test_search_faculty_page_scores_edu(self):
        # _search_faculty_page picks an .edu page that matches the name.
        from app import enricher
        html = (
            '<a class="result__a" href="https://example.com/blog">Some blog about AI</a>'
            '<a class="result__a" href="/l/?uddg=https%3A%2F%2Fcs.mit.edu%2Fpeople%2Fjane-smith">'
            'Jane Smith — MIT CS Faculty</a>'
        )
        with mock.patch("app.enricher.requests.post") as p:
            p.return_value = mock.Mock(status_code=200, text=html, raise_for_status=lambda: None)
            url = enricher._search_faculty_page("Jane Smith", "MIT")
        self.assertEqual(url, "https://cs.mit.edu/people/jane-smith")

    def test_migration_clears_legacy_placeholder_emails(self):
        from app.database import _migrate_schema
        pid = upsert_professor(self.ws, Professor(name="Dr. Z", email="", university="MIT", field="ML"))
        # simulate a legacy placeholder address
        self.ws.execute("UPDATE professors SET email = ? WHERE id = ?", ("dr.z@mit.placeholder", pid))
        self.ws.commit()
        _migrate_schema(self.ws)
        self.assertEqual(get_professor(self.ws, pid).email, "")

    def test_find_email_search_finds_nothing_is_graceful(self):
        # Named professor, no URL: route searches; when nothing turns up it
        # returns a clean message (and makes no real network call here).
        pid = upsert_professor(self.ws, Professor(name="Dr. Y", email="", university="MIT", field="ML", status="needs_email"))
        c = self._client()
        with mock.patch("app.enricher._search_faculty_page", return_value=None):
            r = c.post(f"/professors/{pid}/find-email", json={})
        data = r.get_json()
        self.assertFalse(data["success"])
        self.assertIn("manually", data["error"].lower())

    def test_find_email_no_url_no_name_asks_for_url(self):
        pid = upsert_professor(self.ws, Professor(name="", email="", university="MIT", field="ML", status="needs_email"))
        c = self._client()
        r = c.post(f"/professors/{pid}/find-email", json={})
        data = r.get_json()
        self.assertFalse(data["success"])
        self.assertIn("paste", data["error"].lower())


if __name__ == "__main__":
    unittest.main()
