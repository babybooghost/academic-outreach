import os
import tempfile
import unittest
from unittest.mock import patch

from app.database import init_db
from app.web.app import create_app


class SignupInviteGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = self.temp_dir.name
        self.db_path = os.path.join(root, "auth.db")

        env = {
            "DB_PATH": self.db_path,
            "LOG_DIR": os.path.join(root, "logs"),
            "OUTPUT_DIR": os.path.join(root, "outputs"),
            "FLASK_SECRET_KEY": "invite-test-secret",
            "SIGNUP_INVITE_CODE": "let-me-in",
        }
        self.env_patch = patch.dict(os.environ, env, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        init_db(self.db_path)
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def _signup(self, **overrides):
        data = {
            "email": "invitee@example.com",
            "display_name": "Invitee",
            "password": "secret123",
            "password_confirm": "secret123",
            "invite_code": "let-me-in",
        }
        data.update(overrides)
        return self.client.post("/signup", data=data, follow_redirects=True)

    def test_signup_requires_invite_field(self) -> None:
        body = self.client.get("/signup").get_data(as_text=True)
        self.assertIn("Invite Code", body)

    def test_wrong_invite_is_rejected(self) -> None:
        body = self._signup(invite_code="nope").get_data(as_text=True)
        self.assertIn("invite code is not valid", body)
        self.assertNotIn("Save this key now", body)

    def test_correct_invite_creates_workspace(self) -> None:
        body = self._signup().get_data(as_text=True)
        self.assertIn("Save this key now", body)


if __name__ == "__main__":
    unittest.main()
