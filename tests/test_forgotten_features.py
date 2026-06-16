"""Wiring up previously-dead features: draft editing UI endpoint, sender-profile
edit/delete, and the activity/tracking CSV exports."""
import os
import tempfile
import unittest

from app.database import (
    create_access_key, create_session, delete_sender_profile, get_connection,
    get_drafts, get_sender_profile, init_db, insert_draft, insert_sender_profile,
    update_sender_profile, upsert_professor,
)
from app.models import Draft, Professor, SenderProfile


class ForgottenFeatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "o.db")
        os.environ.update({"DB_PATH": self.db, "FLASK_SECRET_KEY": "t",
                           "LOG_DIR": self.tmp.name, "OUTPUT_DIR": self.tmp.name})
        init_db(self.db)
        conn = get_connection(self.db)
        self.kid = create_access_key(conn, "ao_ff", "A", "user", "t")
        conn.close()
        self.ws = get_connection(self.db, workspace_id=self.kid)
        self.pid = upsert_professor(self.ws, Professor(name="P", email="p@x.edu", university="U", field="ML"))
        self.sp = insert_sender_profile(self.ws, SenderProfile(name="Me", school="HS", grade="12", email="me@x.com"))

    def tearDown(self):
        try: self.ws.close()
        except Exception: pass

    def _client(self):
        from app.web.app import create_app
        app = create_app()
        app.config["TESTING"] = True
        c = app.test_client()
        with c.session_transaction() as s:
            s.update({"authenticated": True, "key_id": self.kid, "key_label": "A", "role": "user"})
        return c

    # --- sender profile edit/delete ---
    def test_update_sender_profile(self):
        ok = update_sender_profile(self.ws, self.sp,
                                   SenderProfile(name="New Name", school="MIT", grade="freshman",
                                                 email="new@x.com", goal="a 15-min chat"))
        self.assertTrue(ok)
        p = get_sender_profile(self.ws, self.sp)
        self.assertEqual(p.name, "New Name")
        self.assertEqual(p.school, "MIT")
        self.assertEqual(p.goal, "a 15-min chat")

    def test_delete_unused_profile(self):
        unused = insert_sender_profile(self.ws, SenderProfile(name="Temp", school="S", grade="12", email="t@x.com"))
        self.assertEqual(delete_sender_profile(self.ws, unused), "deleted")
        self.assertIsNone(get_sender_profile(self.ws, unused))

    def test_delete_in_use_profile_refused(self):
        sid = create_session(self.ws, self.sp, notes="t")
        insert_draft(self.ws, Draft(professor_id=self.pid, sender_profile_id=self.sp, session_id=sid,
                                    subject_lines='["Hi"]', body="b", status="generated"))
        self.assertEqual(delete_sender_profile(self.ws, self.sp), "in_use")
        self.assertIsNotNone(get_sender_profile(self.ws, self.sp))  # still there

    def test_profile_update_via_route(self):
        c = self._client()
        r = c.post("/settings/profiles", data={
            "profile_id": self.sp, "name": "Routed", "school": "HS", "grade": "12", "email": "me@x.com"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(get_sender_profile(self.ws, self.sp).name, "Routed")

    # --- draft editing endpoint (now reachable from the UI) ---
    def test_draft_edit_updates_body_and_marks_edited(self):
        sid = create_session(self.ws, self.sp, notes="t")
        did = insert_draft(self.ws, Draft(professor_id=self.pid, sender_profile_id=self.sp, session_id=sid,
                                          subject_lines='["Old subject"]', body="old body", status="generated"))
        c = self._client()
        r = c.post(f"/drafts/{did}/edit", json={"body": "new body text", "subject": "New subject"})
        self.assertTrue(r.get_json()["success"])
        d = [x for x in get_drafts(self.ws) if x.id == did][0]
        self.assertEqual(d.body, "new body text")
        self.assertEqual(d.subject_lines_list[0], "New subject")
        self.assertEqual(d.status, "edited")

    # --- exports that used to say "Coming soon" ---
    def test_tracking_and_activity_exports(self):
        sid = create_session(self.ws, self.sp, notes="t")
        insert_draft(self.ws, Draft(professor_id=self.pid, sender_profile_id=self.sp, session_id=sid,
                                    subject_lines='["Hi"]', body="b", status="generated"))
        c = self._client()
        for path in ("/export/tracking", "/export/activity"):
            r = c.post(path, json={})
            self.assertEqual(r.status_code, 200, path)
            data = r.get_json()
            self.assertTrue(data["success"], (path, data))
            self.assertTrue(data["filename"].endswith(".csv"))
            # the file is downloadable
            dl = c.get(data["download_url"])
            self.assertEqual(dl.status_code, 200, path)


if __name__ == "__main__":
    unittest.main()
