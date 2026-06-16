"""One-shot hard reset: wipes user data, keeps admin login + global config, runs once."""
import os
import tempfile
import unittest

from app.database import (
    create_access_key,
    get_connection,
    init_db,
    maybe_hard_reset,
    set_settings_bulk,
    upsert_professor,
    validate_access_key,
)
from app.models import Professor


class HardResetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "o.db")
        os.environ.update({"DB_PATH": self.db, "FLASK_SECRET_KEY": "t",
                           "LOG_DIR": self.tmp.name, "OUTPUT_DIR": self.tmp.name})
        init_db(self.db)
        conn = get_connection(self.db)
        self.admin = create_access_key(conn, "ao_admin", "Admin", "admin", "seed")
        self.user = create_access_key(conn, "ao_user", "User", "user", "seed")
        # global config (workspace 0) — must survive
        set_settings_bulk(get_connection(self.db, workspace_id=0), {"llm_api_key": "keep-me"})
        conn.close()
        # user content
        ws = get_connection(self.db, workspace_id=self.user)
        upsert_professor(ws, Professor(name="P", email="p@x.edu", university="U", field="ML"))
        ws.close()

    def test_wipe_keeps_admin_and_global_config(self):
        conn = get_connection(self.db)
        did = maybe_hard_reset(conn, "reset-v1")
        conn.close()
        self.assertTrue(did)
        conn = get_connection(self.db)
        try:
            # user account + content gone; admin kept
            self.assertIsNone(validate_access_key(conn, "ao_user"))
            self.assertIsNotNone(validate_access_key(conn, "ao_admin"))
            n_prof = conn.execute("SELECT COUNT(*) AS c FROM professors").fetchone()["c"]
            self.assertEqual(n_prof, 0)
            # global config (workspace 0) preserved
            gv = conn.execute(
                "SELECT value FROM app_settings WHERE workspace_id = 0 AND key = 'llm_api_key'"
            ).fetchone()
            self.assertEqual(gv["value"], "keep-me")
        finally:
            conn.close()

    def test_same_token_runs_only_once(self):
        conn = get_connection(self.db)
        self.assertTrue(maybe_hard_reset(conn, "reset-v1"))
        self.assertFalse(maybe_hard_reset(conn, "reset-v1"))   # already consumed
        # a new token value triggers another wipe
        self.assertTrue(maybe_hard_reset(conn, "reset-v2"))
        conn.close()

    def test_empty_token_is_noop(self):
        conn = get_connection(self.db)
        self.assertFalse(maybe_hard_reset(conn, ""))
        self.assertFalse(maybe_hard_reset(conn, "   "))
        # nothing wiped
        self.assertIsNotNone(validate_access_key(conn, "ao_user"))
        conn.close()


if __name__ == "__main__":
    unittest.main()
