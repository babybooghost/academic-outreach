import os
import tempfile
import unittest
from unittest.mock import patch

from app.config import load_config
from app.database import (
    create_session,
    get_connection,
    get_draft,
    init_db,
    insert_draft,
    insert_sender_profile,
    upsert_professor,
)
from app.models import Draft, Professor, SendRecord, SenderProfile
from app.sender import SafeSender


class SafeSenderTests(unittest.TestCase):
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
        }
        self.env_patch = patch.dict(os.environ, env, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        self.config = load_config()
        init_db(self.config.db_path)

        conn = get_connection(self.config.db_path)
        try:
            sender_profile = SenderProfile(
                name="Test Sender",
                school="Example High",
                grade="11",
                email="sender@example.com",
                interests="ML",
                background="Python",
            )
            self.sender_profile_id = insert_sender_profile(conn, sender_profile)
            self.session_id = create_session(conn, self.sender_profile_id, notes="test session")

            professor = Professor(
                name="Prof Example",
                email="prof@example.edu",
                university="Example U",
                department="CS",
                field="ML",
                status="ready",
            )
            upsert_professor(conn, professor)
            stored_professor = conn.execute(
                "SELECT id FROM professors WHERE email = ?",
                ("prof@example.edu",),
            ).fetchone()
            self.professor_id = stored_professor["id"]

            draft = Draft(
                professor_id=self.professor_id,
                sender_profile_id=self.sender_profile_id,
                session_id=self.session_id,
                subject_lines='["Hello"]',
                body="Body",
                status="approved",
            )
            self.draft_id = insert_draft(conn, draft)
        finally:
            conn.close()

    def test_send_success_updates_draft_and_suppression(self) -> None:
        sender = SafeSender(self.config, method="smtp")
        record = SendRecord(
            draft_id=self.draft_id,
            professor_id=self.professor_id,
            sent_at="2026-04-19T00:00:00+00:00",
            method="smtp",
            status="success",
        )

        with patch.object(SafeSender, "_send_single", return_value=record):
            conn = get_connection(self.config.db_path)
            try:
                draft = get_draft(conn, self.draft_id)
            finally:
                conn.close()

            sender.send(draft, method="smtp")

        conn = get_connection(self.config.db_path)
        try:
            updated = conn.execute(
                "SELECT status FROM drafts WHERE id = ?",
                (self.draft_id,),
            ).fetchone()
            send_log = conn.execute("SELECT COUNT(*) AS c FROM send_log").fetchone()
            suppression = conn.execute(
                "SELECT COUNT(*) AS c FROM suppression_list WHERE email = ?",
                ("prof@example.edu",),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(updated["status"], "sent")
        self.assertEqual(send_log["c"], 1)
        self.assertEqual(suppression["c"], 1)

    def test_send_failure_marks_draft_failed_and_raises(self) -> None:
        sender = SafeSender(self.config, method="smtp")
        record = SendRecord(
            draft_id=self.draft_id,
            professor_id=self.professor_id,
            sent_at="2026-04-19T00:00:00+00:00",
            method="smtp",
            status="failed",
            error_message="SMTP authentication failed",
        )

        with patch.object(SafeSender, "_send_single", return_value=record):
            conn = get_connection(self.config.db_path)
            try:
                draft = get_draft(conn, self.draft_id)
            finally:
                conn.close()

            with self.assertRaisesRegex(RuntimeError, "SMTP authentication failed"):
                sender.send(draft, method="smtp")

        conn = get_connection(self.config.db_path)
        try:
            updated = conn.execute(
                "SELECT status FROM drafts WHERE id = ?",
                (self.draft_id,),
            ).fetchone()
            send_log = conn.execute("SELECT COUNT(*) AS c FROM send_log").fetchone()
        finally:
            conn.close()

        self.assertEqual(updated["status"], "failed")
        self.assertEqual(send_log["c"], 1)

    def test_send_respects_explicit_method_override(self) -> None:
        sender = SafeSender(self.config, method="gmail_draft")
        captured: dict[str, bool] = {}

        def fake_smtp_send(_smtp, draft, professor, sender_profile, config):
            captured["smtp"] = True
            return SendRecord(
                draft_id=draft.id or 0,
                professor_id=professor.id or 0,
                sent_at="2026-04-19T00:00:00+00:00",
                method="smtp",
                status="success",
            )

        conn = get_connection(self.config.db_path)
        try:
            draft = get_draft(conn, self.draft_id)
            with patch("app.sender.SMTPSender.send", new=fake_smtp_send):
                sender.send(draft, method="smtp", conn=conn)
        finally:
            conn.close()

        self.assertTrue(captured["smtp"])


if __name__ == "__main__":
    unittest.main()
