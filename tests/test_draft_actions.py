"""Regression: draft approve/reject must not 400 on an empty JSON body.

The draft-detail buttons POST with Content-Type: application/json but no body.
The reject route used to call request.json on that empty body, which raises
BadRequest (400) on a real draft. Both actions must succeed regardless.
"""
import os
import tempfile
import unittest

from app.database import (
    create_access_key,
    create_session,
    get_connection,
    init_db,
    insert_draft,
    insert_sender_profile,
    update_draft_status,
    upsert_professor,
)
from app.models import Draft, Professor, SenderProfile


class DraftActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.db = os.path.join(root, "o.db")
        os.environ.update({
            "DB_PATH": self.db, "LOG_DIR": os.path.join(root, "l"),
            "OUTPUT_DIR": os.path.join(root, "o"), "FLASK_SECRET_KEY": "t",
        })
        init_db(self.db)
        conn = get_connection(self.db)
        self.kid = create_access_key(conn, "ao_actions_key", "Tester", "user", "test")
        conn.close()
        ws = get_connection(self.db, workspace_id=self.kid)
        pid = upsert_professor(ws, Professor(name="Test Prof", email="t@x.edu", university="U", field="ML"))
        spid = insert_sender_profile(ws, SenderProfile(
            name="Me", school="HS", grade="12", email="me@x.com", interests="ML", background="b"))
        sid = create_session(ws, spid, notes="test")
        self.did = insert_draft(ws, Draft(
            professor_id=pid, sender_profile_id=spid, session_id=sid,
            subject_lines='["Hi"]', body="Hello", status="generated"))
        ws.close()

        from app.web.app import create_app
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        with self.client.session_transaction() as s:
            s["authenticated"] = True
            s["key_id"] = self.kid
            s["key_label"] = "Tester"
            s["role"] = "user"

    def _reset(self):
        ws = get_connection(self.db, workspace_id=self.kid)
        update_draft_status(ws, self.did, "generated")
        ws.close()

    def test_approve_with_empty_json_body(self):
        r = self.client.post(f"/drafts/{self.did}/approve", content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["success"])

    def test_reject_with_empty_json_body(self):
        self._reset()
        r = self.client.post(f"/drafts/{self.did}/reject", content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["success"])


if __name__ == "__main__":
    unittest.main()
