"""Access-control regressions: cross-workspace isolation, privilege escalation,
auth gating, and credential checks must all hold.
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
    upsert_professor,
)
from app.models import Draft, Professor, SenderProfile


class AccessControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.db = os.path.join(root, "o.db")
        os.environ.update({
            "DB_PATH": self.db, "LOG_DIR": os.path.join(root, "l"),
            "OUTPUT_DIR": os.path.join(root, "o"), "FLASK_SECRET_KEY": "t",
            "SIGNUP_INVITE_CODE": "letmein",
        })
        # Don't let the invite-code env leak into other tests (open-signup ones).
        self.addCleanup(lambda: os.environ.pop("SIGNUP_INVITE_CODE", None))
        init_db(self.db)
        conn = get_connection(self.db)
        self.B = create_access_key(conn, "ao_key_B", "Bob", "user", "t")
        self.A = create_access_key(conn, "ao_key_A", "Alice", "user", "t")
        conn.close()
        # Bob's private draft.
        wsB = get_connection(self.db, workspace_id=self.B)
        pid = upsert_professor(wsB, Professor(name="B Prof", email="b@x.edu", university="U", field="ML"))
        sp = insert_sender_profile(wsB, SenderProfile(name="Bob", school="HS", grade="12", email="b@x.com"))
        sid = create_session(wsB, sp, notes="b")
        self.draftB = insert_draft(wsB, Draft(
            professor_id=pid, sender_profile_id=sp, session_id=sid,
            subject_lines='["x"]', body="secret B", status="generated"))
        self.profB = pid
        wsB.close()

        from app.web.app import create_app
        self.app = create_app()
        self.app.config["TESTING"] = True

    def _user(self, kid):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s.update({"authenticated": True, "key_id": kid, "key_label": "x", "role": "user"})
        return c

    # --- Cross-workspace isolation (IDOR) ---
    def test_user_cannot_read_other_workspace_draft(self):
        # Alice must not see Bob's draft body; route 302-redirects or 404s, never 200 with content.
        r = self._user(self.A).get(f"/drafts/{self.draftB}")
        self.assertIn(r.status_code, (302, 404))
        if r.status_code == 200:
            self.assertNotIn(b"secret B", r.data)

    def test_user_cannot_mutate_other_workspace_draft(self):
        r = self._user(self.A).post(f"/drafts/{self.draftB}/approve",
                                    content_type="application/json", data="{}")
        self.assertEqual(r.status_code, 404)

    def test_user_cannot_read_other_workspace_professor(self):
        r = self._user(self.A).get(f"/professors/{self.profB}")
        self.assertIn(r.status_code, (302, 404))

    # --- Privilege escalation ---
    def test_user_cannot_open_admin(self):
        self.assertEqual(self._user(self.A).get("/admin").status_code, 302)

    def test_user_cannot_create_admin_key(self):
        self.assertEqual(
            self._user(self.A).post("/admin/keys/create", data={"label": "x", "role": "admin"}).status_code,
            302,
        )

    def test_user_key_rejected_at_admin_login(self):
        c = self.app.test_client()
        c.post("/admin/login", data={"access_key": "ao_key_A"})
        with c.session_transaction() as s:
            self.assertFalse(s.get("admin_authenticated"))

    # --- Auth gating + credentials ---
    def test_anonymous_is_redirected(self):
        anon = self.app.test_client()
        self.assertEqual(anon.get("/desk").status_code, 302)
        self.assertEqual(anon.get("/admin").status_code, 302)

    def test_bad_access_key_does_not_authenticate(self):
        c = self.app.test_client()
        c.post("/login", data={"access_key": "ao_not_real"})
        with c.session_transaction() as s:
            self.assertFalse(s.get("authenticated"))

    def test_signup_requires_correct_invite(self):
        body = self.app.test_client().post("/signup", data={
            "invite_code": "wrong", "email": "n@x.edu", "display_name": "N",
            "password": "secret1", "password_confirm": "secret1",
        }).get_data(as_text=True).lower()
        self.assertIn("invite", body)
        # No account created for the bad attempt.
        conn = get_connection(self.db)
        try:
            self.assertIsNone(conn.execute(
                "SELECT id FROM user_signups WHERE email = ?", ("n@x.edu",)).fetchone())
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
