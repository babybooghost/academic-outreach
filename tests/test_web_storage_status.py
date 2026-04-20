import os
import tempfile
import unittest
from unittest.mock import patch

from app.web.app import create_app


class WebStorageStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = self.temp_dir.name

        env = {
            "DB_PATH": os.path.join(root, "outreach.db"),
            "LOG_DIR": os.path.join(root, "logs"),
            "OUTPUT_DIR": os.path.join(root, "outputs"),
            "FLASK_SECRET_KEY": "storage-test-secret",
            "VERCEL": "1",
        }
        self.env_patch = patch.dict(os.environ, env, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_health_reports_ephemeral_hosted_storage(self) -> None:
        response = self.client.get("/health")
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["storage"]["mode"], "ephemeral-instance")
        self.assertFalse(data["storage"]["persistent"])
        self.assertTrue(data["storage"]["workspace_isolated"])


if __name__ == "__main__":
    unittest.main()
