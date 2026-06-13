"""Tests for signup email verification and Google sign-in."""
import base64
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from app.database import (
    create_access_key,
    get_connection,
    init_db,
    set_settings_bulk,
)


def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def _fake_id_token(claims: dict) -> str:
    return f"{_b64({'alg': 'none'})}.{_b64(claims)}.sig"


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


BASE_ENV = {
    "FLASK_SECRET_KEY": "test-secret",
    "SIGNUP_INVITE_CODE": "letmein",
    "GOOGLE_CLIENT_ID": "client-123.apps.googleusercontent.com",
    "GOOGLE_CLIENT_SECRET": "secret-xyz",
}


class AuthFlowTestBase(unittest.TestCase):
    extra_env: dict = {}

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = self.temp_dir.name
        self.db_path = os.path.join(root, "outreach.db")
        env = {
            "DB_PATH": self.db_path,
            "LOG_DIR": os.path.join(root, "logs"),
            "OUTPUT_DIR": os.path.join(root, "out"),
            **BASE_ENV,
            **self.extra_env,
        }
        self.env_patch = patch.dict(os.environ, env, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        init_db(self.db_path)
        from app.web.app import create_app
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()


class EmailVerificationTests(AuthFlowTestBase):
    # System mailbox configured -> signup requires a code.
    extra_env = {
        "SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587",
        "SMTP_USER": "system@example.com", "SMTP_PASSWORD": "pw",
        "SENDER_EMAIL": "system@example.com",
        "SIGNUP_EMAIL_VERIFICATION": "1",
    }

    def test_signup_sends_code_then_verifies(self):
        captured = {}

        def fake_send(cfg, recipient, code):
            captured["recipient"] = recipient
            captured["code"] = code
            return True

        with patch("app.web.app.send_verification_email", new=fake_send):
            resp = self.client.post("/signup", data={
                "invite_code": "letmein", "email": "new@school.edu",
                "display_name": "New Person", "password": "secret1",
                "password_confirm": "secret1",
            })
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Verification code", resp.data)
        self.assertEqual(captured["recipient"], "new@school.edu")

        # No account yet, but a pending verification exists.
        conn = get_connection(self.db_path)
        try:
            self.assertIsNone(conn.execute(
                "SELECT id FROM user_signups WHERE email = ?", ("new@school.edu",)).fetchone())
            self.assertIsNotNone(conn.execute(
                "SELECT id FROM email_verifications WHERE email = ?", ("new@school.edu",)).fetchone())
        finally:
            conn.close()

        # Submit the correct code -> account created.
        resp2 = self.client.post("/signup/verify", data={
            "email": "new@school.edu", "code": captured["code"],
        })
        self.assertEqual(resp2.status_code, 200)
        conn = get_connection(self.db_path)
        try:
            self.assertIsNotNone(conn.execute(
                "SELECT id FROM user_signups WHERE email = ?", ("new@school.edu",)).fetchone())
        finally:
            conn.close()

    def test_wrong_code_does_not_create_account(self):
        with patch("app.web.app.send_verification_email", new=lambda *a: True):
            self.client.post("/signup", data={
                "invite_code": "letmein", "email": "x@school.edu",
                "display_name": "X", "password": "secret1", "password_confirm": "secret1",
            })
        resp = self.client.post("/signup/verify", data={"email": "x@school.edu", "code": "000000"})
        self.assertIn(b"Incorrect code", resp.data)
        conn = get_connection(self.db_path)
        try:
            self.assertIsNone(conn.execute(
                "SELECT id FROM user_signups WHERE email = ?", ("x@school.edu",)).fetchone())
        finally:
            conn.close()


class NoMailboxFallbackTests(AuthFlowTestBase):
    # No system mailbox -> signup creates the account directly (no code step).
    def test_signup_without_mailbox_creates_directly(self):
        resp = self.client.post("/signup", data={
            "invite_code": "letmein", "email": "direct@school.edu",
            "display_name": "Direct", "password": "secret1", "password_confirm": "secret1",
        })
        self.assertEqual(resp.status_code, 200)
        conn = get_connection(self.db_path)
        try:
            self.assertIsNotNone(conn.execute(
                "SELECT id FROM user_signups WHERE email = ?", ("direct@school.edu",)).fetchone())
        finally:
            conn.close()


class GoogleSignInTests(AuthFlowTestBase):
    def _state(self) -> str:
        resp = self.client.get("/auth/google/login")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("accounts.google.com", resp.headers["Location"])
        from urllib.parse import urlparse, parse_qs
        return parse_qs(urlparse(resp.headers["Location"]).query)["state"][0]

    def _callback(self, email, verified=True):
        token = _fake_id_token({
            "aud": BASE_ENV["GOOGLE_CLIENT_ID"], "email": email,
            "email_verified": verified, "name": "G User",
        })
        state = self._state()
        with patch("app.web.app.requests.post", return_value=_FakeResp({"id_token": token})):
            return self.client.get(f"/auth/google/callback?code=abc&state={state}")

    def test_existing_user_logs_in(self):
        # Seed a workspace owned by the Google email.
        conn = get_connection(self.db_path)
        try:
            key_id = create_access_key(conn, "ao_user_g", "Member", "user", "test")
        finally:
            conn.close()
        ws = get_connection(self.db_path, workspace_id=key_id)
        try:
            set_settings_bulk(ws, {"workspace_owner_email": "member@gmail.com"})
        finally:
            ws.close()

        resp = self._callback("member@gmail.com")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/dashboard"))
        with self.client.session_transaction() as sess:
            self.assertTrue(sess.get("authenticated"))
            self.assertEqual(sess.get("key_id"), key_id)

    def test_new_user_routed_to_signup(self):
        resp = self._callback("stranger@gmail.com")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/signup"))
        with self.client.session_transaction() as sess:
            self.assertFalse(sess.get("authenticated"))
            self.assertEqual(sess.get("pending_google", {}).get("email"), "stranger@gmail.com")

    def test_unverified_google_email_rejected(self):
        resp = self._callback("unverified@gmail.com", verified=False)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/login"))


if __name__ == "__main__":
    unittest.main()
