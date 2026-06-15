"""Full-database backup: dumps content tables and redacts password hashes."""
import json
import os
import tempfile
import unittest

from app.database import (
    create_access_key,
    dump_database,
    get_connection,
    init_db,
    upsert_professor,
)
from app.models import Professor


class BackupTests(unittest.TestCase):
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
        self.kid = create_access_key(conn, "ao_backup_key", "T", "user", "secretpw")
        conn.close()
        ws = get_connection(self.db, workspace_id=self.kid)
        upsert_professor(ws, Professor(name="P", email="p@x.edu", university="U", field="ML"))
        ws.close()

    def test_dump_includes_content_and_redacts_hashes(self):
        conn = get_connection(self.db)
        try:
            dump = dump_database(conn)
        finally:
            conn.close()
        self.assertIn("professors", dump["data"])
        self.assertEqual(dump["meta"]["row_counts"]["professors"], 1)
        # The login credential (key_value) must never appear in the dump.
        self.assertTrue(dump["data"]["access_keys"])
        for key in dump["data"]["access_keys"]:
            self.assertEqual(key.get("key_value"), "***redacted***")
        # And the whole thing must be JSON-serializable.
        json.dumps(dump, default=str)

    def test_admin_route_returns_json_attachment(self):
        from app.web.app import create_app
        app = create_app()
        app.config["TESTING"] = True
        c = app.test_client()
        with c.session_transaction() as s:
            s["admin_authenticated"] = True
        r = c.get("/admin/backup")
        self.assertEqual(r.status_code, 200)
        self.assertIn("application/json", r.content_type)
        self.assertIn("attachment", r.headers.get("Content-Disposition", ""))
        body = json.loads(r.get_data(as_text=True))
        self.assertIn("professors", body["data"])

    def test_admin_route_requires_admin(self):
        from app.web.app import create_app
        app = create_app()
        app.config["TESTING"] = True
        r = app.test_client().get("/admin/backup")
        self.assertIn(r.status_code, (302, 401))


if __name__ == "__main__":
    unittest.main()
