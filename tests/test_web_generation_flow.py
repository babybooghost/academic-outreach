import os
import tempfile
import unittest
from unittest.mock import patch

from app.database import get_connection, get_drafts, get_sender_profiles, init_db, upsert_professor
from app.models import Professor
from app.web.app import create_app


class WebGenerationFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = self.temp_dir.name
        self.db_path = os.path.join(root, "workspace.db")

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
            "FLASK_SECRET_KEY": "generation-test-secret",
            "LLM_PROVIDER": "",
            "LLM_API_KEY": "",
        }
        self.env_patch = patch.dict(os.environ, env, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        init_db(self.db_path)
        conn = get_connection(self.db_path)
        try:
            upsert_professor(
                conn,
                Professor(
                    name="Prof Generator",
                    email="generator@example.edu",
                    university="Example University",
                    department="Computer Science",
                    field="AI",
                    research_summary="Builds trustworthy AI systems for scientific research.",
                    recent_work="Recent papers focus on interpretable agents and verification.",
                    status="new",
                ),
            )
        finally:
            conn.close()

        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def _login(self) -> None:
        with self.client.session_transaction() as session:
            session["authenticated"] = True
            session["key_id"] = 1
            session["key_label"] = "Generator User"
            session["role"] = "user"
            session["workspace_db_path"] = self.db_path

    def test_user_can_create_profile_and_generate_drafts_from_web(self) -> None:
        self._login()

        create_profile = self.client.post(
            "/settings/profiles",
            data={
                "name": "Abhay Student",
                "school": "Example High",
                "grade": "11",
                "email": "abhay@example.com",
                "interests": "AI, systems",
                "background": "Built Python projects and research notes.",
                "graduation_year": "2027",
            },
            follow_redirects=True,
        )
        self.assertEqual(create_profile.status_code, 200)
        self.assertIn("Sender profile saved to this workspace.", create_profile.get_data(as_text=True))

        conn = get_connection(self.db_path)
        try:
            profiles = get_sender_profiles(conn)
        finally:
            conn.close()
        self.assertEqual(len(profiles), 1)

        generate = self.client.post(
            "/drafts/generate",
            data={"sender_profile_id": profiles[0].id, "variant": ""},
            follow_redirects=False,
        )
        self.assertEqual(generate.status_code, 302)
        self.assertIn("/drafts?session=", generate.headers["Location"])

        conn = get_connection(self.db_path)
        try:
            drafts = get_drafts(conn)
            professor_status = conn.execute(
                "SELECT status FROM professors WHERE email = ?",
                ("generator@example.edu",),
            ).fetchone()["status"]
        finally:
            conn.close()

        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].status, "generated")
        self.assertTrue(drafts[0].body)
        self.assertTrue(drafts[0].subject_lines_list)
        self.assertEqual(professor_status, "ready")


if __name__ == "__main__":
    unittest.main()
