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

    def test_migration_clears_legacy_placeholder_emails(self):
        from app.database import _migrate_schema
        pid = upsert_professor(self.ws, Professor(name="Dr. Z", email="", university="MIT", field="ML"))
        # simulate a legacy placeholder address
        self.ws.execute("UPDATE professors SET email = ? WHERE id = ?", ("dr.z@mit.placeholder", pid))
        self.ws.commit()
        _migrate_schema(self.ws)
        self.assertEqual(get_professor(self.ws, pid).email, "")

    def test_find_email_no_url_no_profile(self):
        pid = upsert_professor(self.ws, Professor(name="Dr. Y", email="", university="MIT", field="ML", status="needs_email"))
        c = self._client()
        r = c.post(f"/professors/{pid}/find-email", json={})
        data = r.get_json()
        self.assertFalse(data["success"])
        self.assertIn("paste", data["error"].lower())


if __name__ == "__main__":
    unittest.main()
