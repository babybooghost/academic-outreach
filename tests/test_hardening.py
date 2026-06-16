"""Hardening regressions: static cache-busting, nested-secret redaction in the
request log, and attachment-filename sanitization.
"""
import io
import os
import re
import tempfile
import unittest

from app.database import create_access_key, get_connection, init_db


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.db = os.path.join(root, "o.db")
        os.environ.update({
            "DB_PATH": self.db, "LOG_DIR": os.path.join(root, "l"),
            "OUTPUT_DIR": os.path.join(root, "o"), "FLASK_SECRET_KEY": "t",
        })
        init_db(self.db)
        conn = get_connection(self.db)
        self.kid = create_access_key(conn, "ao_h_key", "T", "user", "t")
        conn.close()
        from app.web.app import create_app
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def _login(self):
        with self.client.session_transaction() as s:
            s.update({"authenticated": True, "key_id": self.kid, "key_label": "T", "role": "user"})

    def test_static_urls_are_cache_busted(self):
        html = self.client.get("/login").get_data(as_text=True)
        self.assertRegex(html, r"static/[\w.]+\.css\?v=[0-9a-f]+")

    def test_security_headers_present(self):
        # Set by Flask, so they survive the vercel.json schema change.
        r = self.client.get("/login")
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(r.headers.get("Referrer-Policy"), "strict-origin-when-cross-origin")
        self.assertIn("max-age=", r.headers.get("Strict-Transport-Security", ""))

    def test_nested_secret_redacted_in_request_log(self):
        self._login()
        self.client.post("/api/bug-report", json={
            "title": "t", "details": "d",
            "nested": {"password": "SUPERSECRET", "ok": "keepme"},
        })
        conn = get_connection(self.db)
        try:
            row = conn.execute(
                "SELECT body FROM request_log WHERE path='/api/bug-report' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        body = row["body"] if row else ""
        self.assertNotIn("SUPERSECRET", body)
        self.assertIn("***", body)
        self.assertIn("keepme", body)  # non-secret nested values preserved

    def test_attachment_filename_path_is_stripped(self):
        # Path-traversal components must be stripped before the name is stored
        # (and later placed in an email Content-Disposition header).
        self._login()
        data = {"attachment": (io.BytesIO(b"%PDF-1.4 fake"), "../../../etc/evil.pdf")}
        self.client.post("/settings/attachment", data=data, content_type="multipart/form-data")
        conn = get_connection(self.db, workspace_id=self.kid)
        try:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE workspace_id=? AND key='attachment_filename'",
                (self.kid,),
            ).fetchone()
        finally:
            conn.close()
        name = row["value"] if row else ""
        self.assertEqual(name, "evil.pdf")
        self.assertNotIn("/", name)


if __name__ == "__main__":
    unittest.main()
