"""AI assistant endpoint contract: gated, validates input, and signals the
client to use its built-in canned help when no model is configured.
"""
import os
import tempfile
import unittest
from unittest import mock

from app.database import create_access_key, get_connection, init_db


class ChatApiTests(unittest.TestCase):
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
        self.kid = create_access_key(conn, "ao_chat_key", "T", "user", "t")
        conn.close()
        from app.web.app import create_app
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        with self.client.session_transaction() as s:
            s.update({"authenticated": True, "key_id": self.kid, "key_label": "T", "role": "user"})

    def test_requires_login(self):
        anon = self.app.test_client()
        r = anon.post("/api/chat", json={"message": "hi"})
        self.assertIn(r.status_code, (302, 401))

    def test_empty_message_rejected(self):
        r = self.client.post("/api/chat", json={"message": "   "})
        self.assertFalse(r.get_json()["success"])

    def test_no_model_signals_canned_fallback(self):
        # No LLM configured -> client should fall back to built-in help.
        r = self.client.post("/api/chat", json={"message": "how do I find professors?"})
        self.assertEqual(r.get_json().get("error"), "no_ai")

    def test_reply_when_model_configured(self):
        # Configure the workspace for OpenRouter + a key, mock the LLM call.
        from app.database import set_settings_bulk
        ws = get_connection(self.db, workspace_id=self.kid)
        set_settings_bulk(ws, {"llm_provider": "openrouter"})
        ws.close()
        os.environ["LLM_API_KEY"] = "k"
        self.addCleanup(lambda: os.environ.pop("LLM_API_KEY", None))

        with mock.patch("app.summarizer.chat_openrouter", return_value="Try Search; pick 3 plausible labs."):
            r = self.client.post("/api/chat", json={"message": "who should I contact?", "history": []})
        data = r.get_json()
        self.assertTrue(data.get("success"), data)
        self.assertIn("Search", data["reply"])
        # logged to chat_logs with the 'ai' marker
        conn = get_connection(self.db)
        try:
            row = conn.execute("SELECT prompt_key FROM chat_logs ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["prompt_key"], "ai")


if __name__ == "__main__":
    unittest.main()
