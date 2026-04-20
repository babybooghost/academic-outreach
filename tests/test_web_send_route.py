import os
import tempfile
import unittest
from unittest.mock import patch

from app.database import (
    create_session,
    get_connection,
    init_db,
    insert_draft,
    insert_sender_profile,
    upsert_professor,
)
from app.models import Draft, Professor, SenderProfile
from app.web.app import create_app


class WebSendRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = self.temp_dir.name
        self.db_path = os.path.join(root, "outreach.db")

        env = {
            "DB_PATH": self.db_path,
            "LOG_DIR": os.path.join(root, "logs"),
            "OUTPUT_DIR": os.path.join(root, "outputs"),
            "SENDER_EMAIL": "sender@example.com",
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "587",
            "SMTP_USER": "sender@example.com",
            "SMTP_PASSWORD": "topsecret",
            "EMAIL_PROVIDER": "gmail",
            "FLASK_SECRET_KEY": "test-secret",
        }
        self.env_patch = patch.dict(os.environ, env, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        init_db(self.db_path)
        conn = get_connection(self.db_path)
        try:
            sender_profile = SenderProfile(
                name="Web Sender",
                school="Example High",
                grade="11",
                email="sender@example.com",
                interests="ML",
                background="Python",
            )
            sender_profile_id = insert_sender_profile(conn, sender_profile)
            session_id = create_session(conn, sender_profile_id, notes="web test")

            professor = Professor(
                name="Prof Route",
                email="route@example.edu",
                university="Example U",
                department="CS",
                field="ML",
                status="ready",
            )
            upsert_professor(conn, professor)
            professor_id = conn.execute(
                "SELECT id FROM professors WHERE email = ?",
                ("route@example.edu",),
            ).fetchone()["id"]

            draft = Draft(
                professor_id=professor_id,
                sender_profile_id=sender_profile_id,
                session_id=session_id,
                subject_lines='["Subject"]',
                body="Body",
                status="approved",
            )
            insert_draft(conn, draft)
        finally:
            conn.close()

        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def _login(self) -> None:
        with self.client.session_transaction() as session:
            session["authenticated"] = True
            session["key_id"] = 1
            session["key_label"] = "Test User"
            session["role"] = "user"
            session["workspace_db_path"] = self.db_path

    def test_send_route_returns_summary_from_service(self) -> None:
        self._login()
        fake_results = [
            {
                "draft_id": 1,
                "professor": "Prof Route",
                "email": "route@example.edu",
                "method": "smtp",
                "status": "sent",
            }
        ]

        with patch("app.sender.SafeSender.send_many", return_value=fake_results):
            response = self.client.post(
                "/send",
                json={"dry_run": False, "method": "smtp", "limit": 10},
            )

        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["success"])
        self.assertEqual(data["sent"], 1)
        self.assertEqual(data["failed"], 0)
        self.assertEqual(data["results"][0]["status"], "sent")


if __name__ == "__main__":
    unittest.main()
