"""Reply tracking: marking a reply must exclude the professor from follow-ups
and update the outreach stats.
"""
import os
import tempfile
import unittest

from app.database import (
    create_access_key,
    create_session,
    get_connection,
    get_outreach_stats,
    insert_draft,
    insert_sender_profile,
    init_db,
    set_draft_outcome,
    update_draft_status,
    upsert_professor,
)
from app.delivery import _eligible_followup_drafts
from app.models import Draft, Professor, SenderProfile


class ReplyTrackingTests(unittest.TestCase):
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
        self.kid = create_access_key(conn, "ao_reply_key", "T", "user", "t")
        conn.close()
        self.ws = get_connection(self.db, workspace_id=self.kid)
        pid = upsert_professor(self.ws, Professor(name="P", email="p@x.edu", university="U", field="ML"))
        sp = insert_sender_profile(self.ws, SenderProfile(name="Me", school="HS", grade="12", email="me@x.com"))
        sid = create_session(self.ws, sp, notes="t")
        self.did = insert_draft(self.ws, Draft(
            professor_id=pid, sender_profile_id=sp, session_id=sid,
            subject_lines='["Hi"]', body="Hello", status="generated"))
        # Mark it sent, then backdate so it's follow-up-eligible.
        update_draft_status(self.ws, self.did, "sent")
        self.ws.execute("UPDATE drafts SET created_at = '2020-01-01T00:00:00', reviewed_at = '2020-01-01T00:00:00' WHERE id = ?", (self.did,))
        self.ws.commit()

    def test_sent_draft_is_followup_eligible_until_reply(self):
        eligible = [d.id for d in _eligible_followup_drafts(self.ws, days_since=7, limit=10)]
        self.assertIn(self.did, eligible)

    def test_marking_reply_excludes_from_followups_and_updates_stats(self):
        set_draft_outcome(self.ws, self.did, "replied")
        eligible = [d.id for d in _eligible_followup_drafts(self.ws, days_since=7, limit=10)]
        self.assertNotIn(self.did, eligible)
        stats = get_outreach_stats(self.ws)
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(stats["replied"], 1)
        self.assertEqual(stats["reply_rate"], 100)

    def test_meeting_counts_as_reply(self):
        set_draft_outcome(self.ws, self.did, "meeting")
        stats = get_outreach_stats(self.ws)
        self.assertEqual(stats["replied"], 1)
        self.assertEqual(stats["meetings"], 1)

    def test_clearing_outcome_re_enables_followup(self):
        set_draft_outcome(self.ws, self.did, "replied")
        set_draft_outcome(self.ws, self.did, "")
        eligible = [d.id for d in _eligible_followup_drafts(self.ws, days_since=7, limit=10)]
        self.assertIn(self.did, eligible)

    def test_outcome_route(self):
        from app.web.app import create_app
        app = create_app()
        app.config["TESTING"] = True
        c = app.test_client()
        with c.session_transaction() as s:
            s.update({"authenticated": True, "key_id": self.kid, "key_label": "T", "role": "user"})
        r = c.post(f"/drafts/{self.did}/outcome", json={"outcome": "replied"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["success"])
        # bad outcome rejected
        r2 = c.post(f"/drafts/{self.did}/outcome", json={"outcome": "garbage"})
        self.assertEqual(r2.status_code, 400)

    def tearDown(self):
        try:
            self.ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
