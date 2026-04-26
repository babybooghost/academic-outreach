import os
import tempfile
import unittest
from unittest.mock import patch

from app.database import (
    create_access_key,
    create_session,
    get_connection,
    init_db,
    insert_draft,
    insert_sender_profile,
    set_settings_bulk,
    upsert_professor,
)
from app.delivery import workspace_db_path
from app.models import Draft, Professor, SendRecord, SenderProfile
from app.web.app import create_app


class AutoDeliveryCronTests(unittest.TestCase):
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
            "CRON_SECRET": "test-cron-secret",
        }
        self.env_patch = patch.dict(os.environ, env, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        init_db(self.db_path)
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        auth_conn = get_connection(self.db_path)
        try:
            self.key_id = create_access_key(
                auth_conn,
                "ao_user_auto",
                "Auto User",
                "user",
                "test",
            )
        finally:
            auth_conn.close()

        self.workspace_path = workspace_db_path(self.app.config["APP_CFG"], self.key_id)
        init_db(self.workspace_path)

    def _seed_workspace(self, *, enabled: bool = True) -> None:
        conn = get_connection(self.workspace_path)
        try:
            set_settings_bulk(conn, {
                "auto_send_enabled": "1" if enabled else "0",
                "auto_send_method": "smtp",
                "auto_send_limit": "5",
                "sender_email": "sender@example.com",
                "email_provider": "gmail",
                "smtp_user": "sender@example.com",
                "smtp_password": "topsecret",
            })
            sender_profile = SenderProfile(
                name="Auto Sender",
                school="Example High",
                grade="11",
                email="sender@example.com",
                interests="ML",
                background="Python",
            )
            sender_profile_id = insert_sender_profile(conn, sender_profile)
            session_id = create_session(conn, sender_profile_id, notes="auto test")

            professor = Professor(
                name="Prof Auto",
                email="auto@example.edu",
                university="Example U",
                department="CS",
                field="ML",
                status="ready",
            )
            upsert_professor(conn, professor)
            professor_id = conn.execute(
                "SELECT id FROM professors WHERE email = ?",
                ("auto@example.edu",),
            ).fetchone()["id"]

            draft = Draft(
                professor_id=professor_id,
                sender_profile_id=sender_profile_id,
                session_id=session_id,
                subject_lines='["Auto Subject"]',
                body="Body",
                status="approved",
            )
            self.draft_id = insert_draft(conn, draft)
        finally:
            conn.close()

    def test_cron_requires_authorization(self) -> None:
        response = self.client.get("/api/cron/auto-send")

        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.get_json()["success"])

    def test_cron_sends_enabled_workspace_queue(self) -> None:
        self._seed_workspace(enabled=True)
        captured: dict[str, str] = {}

        def fake_smtp_send(_smtp, draft, professor, sender_profile, config):
            captured["recipient"] = professor.email
            captured["sender"] = config.sender_email
            return SendRecord(
                draft_id=draft.id or 0,
                professor_id=professor.id or 0,
                sent_at="2026-04-26T00:00:00+00:00",
                method="smtp",
                status="success",
            )

        with patch("app.sender.SMTPSender.send", new=fake_smtp_send):
            response = self.client.get(
                "/api/cron/auto-send",
                headers={"Authorization": "Bearer test-cron-secret"},
            )

        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["success"])
        self.assertEqual(data["processed"], 1)
        self.assertEqual(data["sent"], 1)
        self.assertEqual(captured["recipient"], "auto@example.edu")
        self.assertEqual(captured["sender"], "sender@example.com")

        conn = get_connection(self.workspace_path)
        try:
            row = conn.execute(
                "SELECT status FROM drafts WHERE id = ?",
                (self.draft_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "sent")

    def test_cron_skips_disabled_workspace(self) -> None:
        self._seed_workspace(enabled=False)

        with patch("app.sender.SMTPSender.send") as mocked_send:
            response = self.client.get(
                "/api/cron/auto-send",
                headers={"Authorization": "Bearer test-cron-secret"},
            )

        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["success"])
        self.assertEqual(data["processed"], 0)
        self.assertEqual(data["sent"], 0)
        mocked_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
