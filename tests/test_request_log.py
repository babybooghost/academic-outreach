"""The detailed request log captures requests and redacts secrets."""
import os
import tempfile
import unittest
from unittest.mock import patch

from app.database import create_access_key, get_connection, init_db


class RequestLogTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = self.temp_dir.name
        self.db_path = os.path.join(root, "outreach.db")
        env = {
            "DB_PATH": self.db_path,
            "LOG_DIR": os.path.join(root, "logs"),
            "OUTPUT_DIR": os.path.join(root, "out"),
            "FLASK_SECRET_KEY": "test-secret",
        }
        self.env_patch = patch.dict(os.environ, env, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        init_db(self.db_path)
        from app.web.app import create_app
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def _latest(self, path):
        conn = get_connection(self.db_path)
        try:
            return conn.execute(
                "SELECT * FROM request_log WHERE path = ? ORDER BY id DESC LIMIT 1",
                (path,),
            ).fetchone()
        finally:
            conn.close()

    def test_request_is_logged_with_secret_redacted(self):
        self.client.post("/login", data={"access_key": "ao_supersecretvalue"})
        row = self._latest("/login")
        self.assertIsNotNone(row)
        self.assertEqual(row["method"], "POST")
        self.assertTrue(row["status"])
        # The secret must be masked, never stored in the clear.
        self.assertNotIn("ao_supersecretvalue", row["body"])
        self.assertIn("***", row["body"])

    def test_static_and_health_are_not_logged(self):
        self.client.get("/health")
        self.assertIsNone(self._latest("/health"))

    def test_admin_can_view_logs(self):
        conn = get_connection(self.db_path)
        try:
            create_access_key(conn, "ao_admin_test", "Admin", "admin", "test")
        finally:
            conn.close()
        self.client.post("/admin/login", data={"access_key": "ao_admin_test"})
        resp = self.client.get("/admin/logs")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Request Logs", resp.data)


if __name__ == "__main__":
    unittest.main()
