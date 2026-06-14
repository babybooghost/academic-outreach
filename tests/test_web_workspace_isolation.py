import os
import re
import tempfile
import time
import unittest
from unittest.mock import patch

from app.database import init_db
from app.web.app import create_app


class WebWorkspaceIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = self.temp_dir.name
        self.db_path = os.path.join(root, "auth.db")

        env = {
            "DB_PATH": self.db_path,
            "LOG_DIR": os.path.join(root, "logs"),
            "OUTPUT_DIR": os.path.join(root, "outputs"),
            "SENDER_EMAIL": "",
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "587",
            "SMTP_USER": "",
            "SMTP_PASSWORD": "",
            "EMAIL_PROVIDER": "gmail",
            "FLASK_SECRET_KEY": "workspace-test-secret",
        }
        self.env_patch = patch.dict(os.environ, env, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        init_db(self.db_path)
        self.app = create_app()
        self.app.config["TESTING"] = True

    def _signup_and_login(self, client, email: str, display_name: str) -> str:
        signup_response = client.post(
            "/signup",
            data={
                "email": email,
                "display_name": display_name,
                "password": "secret123",
                "password_confirm": "secret123",
            },
            follow_redirects=True,
        )
        body = signup_response.get_data(as_text=True)
        match = re.search(r"ao_[A-Za-z0-9_]+", body)
        self.assertIsNotNone(match, "signup should return an access key")
        access_key = match.group(0)

        login_response = client.post(
            "/login",
            data={"access_key": access_key},
            follow_redirects=True,
        )
        self.assertEqual(login_response.status_code, 200)
        return access_key

    def test_settings_and_professors_are_isolated_per_workspace(self) -> None:
        client_one = self.app.test_client()
        client_two = self.app.test_client()

        self._signup_and_login(
            client_one,
            f"user-one-{int(time.time())}@example.com",
            "User One",
        )
        client_one.post(
            "/settings",
            data={
                "sender_email": "user-one@example.com",
                "llm_provider": "anthropic",
                "llm_model": "anthropic/claude-opus-4-6",
                "email_provider": "gmail",
                "smtp_user": "user-one@example.com",
                "smtp_password": "workspace-one-secret",
            },
            follow_redirects=True,
        )
        save_response = client_one.post(
            "/finder/save",
            json={
                "professors": [
                    {
                        "name": "Prof Workspace One",
                        "email": "workspace.one@example.edu",
                        "university": "Isolation University",
                        "department": "Computer Science",
                        "field": "AI",
                        "title": "Professor",
                        "profile_url": "https://example.edu/workspace-one",
                        "research_summary": "Works on multi-agent systems.",
                        "notes": "Saved by user one.",
                    }
                ]
            },
        )
        self.assertEqual(save_response.status_code, 200)

        settings_one = client_one.get("/setup").get_data(as_text=True)
        professors_one = client_one.get("/faculty").get_data(as_text=True)
        self.assertIn("user-one@example.com", settings_one)
        self.assertIn("Prof Workspace One", professors_one)

        user_two_email = f"user-two-{int(time.time())}@example.com"
        self._signup_and_login(
            client_two,
            user_two_email,
            "User Two",
        )

        settings_two = client_two.get("/setup").get_data(as_text=True)
        professors_two = client_two.get("/faculty").get_data(as_text=True)

        self.assertIn(user_two_email, settings_two)
        self.assertIn("This workspace sends for", settings_two)
        self.assertNotIn("user-one@example.com", settings_two)
        self.assertNotIn("workspace-one-secret", settings_two)
        self.assertNotIn("Prof Workspace One", professors_two)
        self.assertIn("No faculty files match this view yet.", professors_two)

        # Tenant-safety: both workspaces must live in the SAME shared database,
        # separated only by workspace_id — not in per-user database files.
        client_two.post(
            "/finder/save",
            json={
                "professors": [
                    {
                        "name": "Prof Workspace Two",
                        "email": "workspace.two@example.edu",
                        "university": "Isolation University",
                        "field": "Robotics",
                    }
                ]
            },
        )
        from app.database import get_connection

        conn = get_connection(self.db_path)
        try:
            rows = conn.execute(
                "SELECT name, workspace_id FROM professors ORDER BY name"
            ).fetchall()
        finally:
            conn.close()
        names = {r["name"]: r["workspace_id"] for r in rows}
        self.assertIn("Prof Workspace One", names)
        self.assertIn("Prof Workspace Two", names)
        # Distinct workspace ids, both persisted in one database file.
        self.assertNotEqual(
            names["Prof Workspace One"], names["Prof Workspace Two"]
        )


if __name__ == "__main__":
    unittest.main()
