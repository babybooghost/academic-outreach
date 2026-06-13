"""The outgoing CV/resume attachment is wired into the MIME message."""
import base64
import os
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from app.config import load_config
from app.models import Draft, Professor, SenderProfile
from app.sender import _build_mime_message


def _base_config():
    with tempfile.TemporaryDirectory() as d:
        env = {
            "DB_PATH": os.path.join(d, "x.db"),
            "LOG_DIR": os.path.join(d, "logs"),
            "OUTPUT_DIR": os.path.join(d, "out"),
            "SENDER_EMAIL": "me@example.com",
        }
        with patch.dict(os.environ, env, clear=False):
            return load_config()


_DRAFT = Draft(professor_id=1, sender_profile_id=1, session_id=1,
               subject_lines='["Hi there"]', body="Hello professor.", status="approved")
_PROF = Professor(name="Prof X", email="x@example.edu", university="U",
                  department="CS", field="ML")
_SENDER = SenderProfile(name="Student", school="High", grade="11",
                        email="me@example.com", interests="", background="")


class AttachmentMimeTests(unittest.TestCase):
    def test_no_attachment_is_plain_alternative(self):
        cfg = _base_config()
        msg = _build_mime_message(_DRAFT, _PROF, _SENDER, cfg)
        self.assertEqual(msg.get_content_subtype(), "alternative")
        filenames = [p.get_filename() for p in msg.walk()]
        self.assertNotIn("cv.pdf", filenames)

    def test_attachment_is_included_as_mixed(self):
        cfg = replace(
            _base_config(),
            attachment_filename="cv.pdf",
            attachment_mimetype="application/pdf",
            attachment_b64=base64.b64encode(b"%PDF-1.4 data").decode("ascii"),
        )
        msg = _build_mime_message(_DRAFT, _PROF, _SENDER, cfg)
        self.assertEqual(msg.get_content_subtype(), "mixed")

        parts = {p.get_filename(): p for p in msg.walk() if p.get_filename()}
        self.assertIn("cv.pdf", parts)
        self.assertEqual(parts["cv.pdf"].get_payload(decode=True), b"%PDF-1.4 data")

    def test_corrupt_attachment_is_skipped_not_fatal(self):
        cfg = replace(
            _base_config(),
            attachment_filename="cv.pdf",
            attachment_mimetype="application/pdf",
            attachment_b64="!!!not-valid-base64!!!",
        )
        # Should not raise; falls back to a normal message.
        msg = _build_mime_message(_DRAFT, _PROF, _SENDER, cfg)
        self.assertIsNotNone(msg["To"])


if __name__ == "__main__":
    unittest.main()
