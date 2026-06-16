"""Removing faculty files: cascades to their drafts/sends/follow-ups, stays in-workspace."""
import os
import tempfile
import unittest

from app.database import (
    create_access_key,
    create_session,
    delete_professors,
    get_connection,
    get_professors,
    insert_draft,
    insert_sender_profile,
    init_db,
    upsert_professor,
)
from app.models import Draft, Professor, SenderProfile


class ProfessorDeleteTests(unittest.TestCase):
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
        self.kid = create_access_key(conn, "ao_del_key", "A", "user", "t")
        self.other = create_access_key(conn, "ao_other_key", "B", "user", "t")
        conn.close()

    def _ws(self, kid):
        return get_connection(self.db, workspace_id=kid)

    def test_delete_cascades_to_drafts(self):
        ws = self._ws(self.kid)
        p1 = upsert_professor(ws, Professor(name="P1", email="p1@x.edu", university="U", field="ML"))
        p2 = upsert_professor(ws, Professor(name="P2", email="p2@x.edu", university="U", field="ML"))
        sp = insert_sender_profile(ws, SenderProfile(name="Me", school="HS", grade="12", email="me@x.com"))
        sid = create_session(ws, sp, notes="t")
        insert_draft(ws, Draft(professor_id=p1, sender_profile_id=sp, session_id=sid,
                               subject_lines='["Hi"]', body="b", status="generated"))
        removed = delete_professors(ws, [p1])
        self.assertEqual(removed, 1)
        names = {p.name for p in get_professors(ws)}
        self.assertEqual(names, {"P2"})
        # The deleted professor's draft is gone too.
        n = ws.execute("SELECT COUNT(*) AS c FROM drafts WHERE professor_id = ?", (p1,)).fetchone()["c"]
        self.assertEqual(n, 0)
        ws.close()

    def test_delete_is_workspace_scoped(self):
        a = self._ws(self.kid)
        b = self._ws(self.other)
        pa = upsert_professor(a, Professor(name="A1", email="a1@x.edu", university="U", field="ML"))
        pb = upsert_professor(b, Professor(name="B1", email="b1@x.edu", university="U", field="ML"))
        # Workspace A tries to delete B's professor id — must not touch B.
        removed = delete_professors(a, [pb])
        self.assertEqual(removed, 0)
        self.assertEqual({p.name for p in get_professors(b)}, {"B1"})
        a.close(); b.close()

    def test_bulk_delete_route(self):
        from app.web.app import create_app
        ws = self._ws(self.kid)
        p1 = upsert_professor(ws, Professor(name="P1", email="p1@x.edu", university="U", field="ML"))
        p2 = upsert_professor(ws, Professor(name="P2", email="p2@x.edu", university="U", field="ML"))
        ws.close()
        app = create_app()
        app.config["TESTING"] = True
        c = app.test_client()
        with c.session_transaction() as s:
            s.update({"authenticated": True, "key_id": self.kid, "key_label": "A", "role": "user"})
        r = c.post("/professors/bulk-delete", data={"professor_ids": [str(p1), str(p2)]})
        self.assertEqual(r.status_code, 302)
        ws = self._ws(self.kid)
        try:
            self.assertEqual(get_professors(ws), [])
        finally:
            ws.close()


if __name__ == "__main__":
    unittest.main()
