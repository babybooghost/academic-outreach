import dataclasses
import os
import tempfile
import unittest
from unittest.mock import patch

from app.config import load_config
from app.database import get_connection, init_db
from app.enricher import (
    _find_contact_link,
    extract_email_from_arxiv,
    extract_email_from_html,
    extract_email_from_text,
)
from app.models import Draft, Professor, SenderProfile
from app.sender import SafeSender


class EmailExtractionTests(unittest.TestCase):
    def test_prefers_mailto_and_name_match(self) -> None:
        html = (
            '<a href="mailto:jsmith@stanford.edu">email</a> '
            "general info@stanford.edu"
        )
        self.assertEqual(
            extract_email_from_html(html, "John Smith", "Stanford University"),
            "jsmith@stanford.edu",
        )

    def test_text_extractor_prefers_name_match(self) -> None:
        text = "General: info@mit.edu. Corresponding author: jdoe@mit.edu (J. Doe)."
        self.assertEqual(
            extract_email_from_text(text, "Jane Doe", "MIT"),
            "jdoe@mit.edu",
        )

    def test_arxiv_extractor_ignores_non_arxiv_url(self) -> None:
        # No network: a non-arXiv URL must short-circuit to None.
        self.assertIsNone(extract_email_from_arxiv("https://example.edu/~prof"))

    def test_finds_contact_link_absolute(self) -> None:
        html = '<a href="research.html">Research</a> <a href="/people/contact">Contact</a>'
        self.assertEqual(
            _find_contact_link(html, "https://lab.edu/~prof/index.html"),
            "https://lab.edu/people/contact",
        )

    def test_no_contact_link_returns_none(self) -> None:
        html = '<a href="/research">Research</a> <a href="mailto:x@y.edu">mail</a>'
        self.assertIsNone(_find_contact_link(html, "https://lab.edu/"))

    def test_deobfuscates_text(self) -> None:
        html = "<p>jane [at] mit [dot] edu</p>"
        self.assertEqual(
            extract_email_from_html(html, "Jane Doe", "MIT"), "jane@mit.edu"
        )

    def test_rejects_generic_only(self) -> None:
        html = "<p>webmaster@some-college.com</p>"
        self.assertIsNone(
            extract_email_from_html(html, "Alan Turing", "Some College")
        )

    def test_ignores_image_false_positive(self) -> None:
        self.assertIsNone(extract_email_from_html('<img src="logo@2x.png">', "X Y", "Z"))

    def test_picks_name_match_over_unrelated_edu(self) -> None:
        html = "random@harvard.edu and aturing@harvard.edu"
        self.assertEqual(
            extract_email_from_html(html, "Alan Turing", "Harvard University"),
            "aturing@harvard.edu",
        )


class PlaceholderSendGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = self.temp_dir.name
        self.db_path = os.path.join(root, "guard.db")
        env = {
            "DB_PATH": self.db_path,
            "LOG_DIR": os.path.join(root, "logs"),
            "OUTPUT_DIR": os.path.join(root, "outputs"),
            "SENDER_EMAIL": "me@example.com",
            "SMTP_USER": "me@example.com",
            "SMTP_PASSWORD": "secret",
            "EMAIL_PROVIDER": "gmail",
            "FLASK_SECRET_KEY": "guard-secret",
        }
        self.env_patch = patch.dict(os.environ, env, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        init_db(self.db_path)
        self.cfg = load_config()

    def test_send_refuses_placeholder_recipient(self) -> None:
        conn = get_connection(self.db_path, workspace_id=1)
        try:
            prof = Professor(
                id=1, name="Ghost", email="ghost@uni.placeholder",
                university="Uni", department="CS", field="AI",
            )
            draft = Draft(
                id=1, professor_id=1, sender_profile_id=0, session_id=0,
                subject_lines='["s"]', body="b", status="approved",
            )
            sender_profile = SenderProfile(
                id=0, name="Me", school="", grade="", email="me@example.com",
                interests="", background="",
            )
            with self.assertRaises(RuntimeError) as ctx:
                SafeSender(self.cfg, method="smtp").send(
                    draft, "smtp", conn=conn, professor=prof,
                    sender_profile=sender_profile, dry_run=False,
                )
            self.assertIn("needs email", str(ctx.exception).lower())
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
