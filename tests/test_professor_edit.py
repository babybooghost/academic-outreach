"""Editing a faculty file persists changes (esp. research_summary, which the AI
email writer now grounds drafts in) and preserves auto-generated fields."""
import os
import tempfile
import unittest

from app.database import (create_access_key, get_connection, get_professor,
                          init_db, upsert_professor)
from app.models import Professor


class ProfessorEditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "o.db")
        os.environ.update({"DB_PATH": self.db, "FLASK_SECRET_KEY": "t",
                           "LOG_DIR": self.tmp.name, "OUTPUT_DIR": self.tmp.name})
        init_db(self.db)
        conn = get_connection(self.db)
        self.kid = create_access_key(conn, "ao_pe", "A", "user", "t")
        conn.close()
        self.ws = get_connection(self.db, workspace_id=self.kid)
        self.pid = upsert_professor(self.ws, Professor(
            name="Old Name", email="old@x.edu", university="Old U", field="ML",
            research_summary="thin scrape", keywords='["graph neural networks"]',
            talking_points='["a specific anchor"]'))

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

    def test_edit_updates_fields_and_keeps_autogen(self):
        c = self._client()
        r = c.post(f"/professors/{self.pid}/edit", data={
            "name": "Dr. New Name", "email": "new@mit.edu", "university": "MIT",
            "field": "Machine Learning", "title": "Associate Professor",
            "research_summary": "Develops scalable graph neural networks for molecular property prediction.",
            "recent_work": "2024 paper on GNN scaling.", "notes": "met at conference",
        })
        self.assertEqual(r.status_code, 302)
        p = get_professor(self.ws, self.pid)
        self.assertEqual(p.name, "Dr. New Name")
        self.assertEqual(p.email, "new@mit.edu")
        self.assertEqual(p.university, "MIT")
        self.assertIn("molecular property", p.research_summary)
        self.assertEqual(p.recent_work, "2024 paper on GNN scaling.")
        # auto-generated fields untouched
        self.assertEqual(p.keywords_list, ["graph neural networks"])
        self.assertEqual(p.talking_points_list, ["a specific anchor"])

    def test_edit_missing_professor(self):
        c = self._client()
        r = c.post("/professors/999999/edit", data={"name": "X"})
        self.assertEqual(r.status_code, 302)  # redirects with a flash, no crash

    def test_edit_requires_login(self):
        from app.web.app import create_app
        app = create_app(); app.config["TESTING"] = True
        r = app.test_client().post(f"/professors/{self.pid}/edit", data={"name": "X"})
        self.assertIn(r.status_code, (302, 401))


if __name__ == "__main__":
    unittest.main()
