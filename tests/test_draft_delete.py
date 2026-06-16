"""Deleting drafts: clears them from the queue, cascades, stays in-workspace."""
import os
import tempfile
import unittest

from app.database import (
    create_access_key,
    create_session,
    delete_drafts,
    get_connection,
    get_drafts,
    insert_draft,
    insert_sender_profile,
    init_db,
    update_draft_status,
    upsert_professor,
)
from app.models import Draft, Professor, SenderProfile


class DraftDeleteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "o.db")
        os.environ.update({"DB_PATH": self.db, "FLASK_SECRET_KEY": "t",
                           "LOG_DIR": self.tmp.name, "OUTPUT_DIR": self.tmp.name})
        init_db(self.db)
        conn = get_connection(self.db)
        self.kid = create_access_key(conn, "ao_dd", "A", "user", "t")
        self.other = create_access_key(conn, "ao_dd2", "B", "user", "t")
        conn.close()

    def _seed(self, kid):
        ws = get_connection(self.db, workspace_id=kid)
        pid = upsert_professor(ws, Professor(name="P", email="p@x.edu", university="U", field="ML"))
        sp = insert_sender_profile(ws, SenderProfile(name="Me", school="HS", grade="12", email="me@x.com"))
        sid = create_session(ws, sp, notes="t")
        ids = []
        for _ in range(3):
            ids.append(insert_draft(ws, Draft(professor_id=pid, sender_profile_id=sp, session_id=sid,
                                              subject_lines='["Hi"]', body="b", status="generated")))
        return ws, ids

    def test_delete_removes_from_queue(self):
        ws, ids = self._seed(self.kid)
        removed = delete_drafts(ws, ids[:2])
        self.assertEqual(removed, 2)
        self.assertEqual(len(get_drafts(ws)), 1)
        ws.close()

    def test_delete_is_workspace_scoped(self):
        a, _ = self._seed(self.kid)
        b, b_ids = self._seed(self.other)
        # A tries to delete B's drafts — must not work.
        self.assertEqual(delete_drafts(a, b_ids), 0)
        self.assertEqual(len(get_drafts(b)), 3)
        a.close(); b.close()

    def test_delete_rejected_route(self):
        from app.web.app import create_app
        ws, ids = self._seed(self.kid)
        update_draft_status(ws, ids[0], "rejected")
        update_draft_status(ws, ids[1], "rejected")
        ws.close()
        app = create_app()
        app.config["TESTING"] = True
        c = app.test_client()
        with c.session_transaction() as s:
            s.update({"authenticated": True, "key_id": self.kid, "key_label": "A", "role": "user"})
        r = c.post("/drafts/bulk-delete", data={"scope": "rejected"})
        self.assertEqual(r.status_code, 302)
        ws = get_connection(self.db, workspace_id=self.kid)
        try:
            remaining = get_drafts(ws)
            self.assertEqual(len(remaining), 1)          # only the non-rejected one left
            self.assertEqual(remaining[0].status, "generated")
        finally:
            ws.close()


if __name__ == "__main__":
    unittest.main()
