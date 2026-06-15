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

        with mock.patch("app.summarizer.chat_with_tools",
                        return_value={"content": "Try Search; pick 3 plausible labs.", "tool_calls": []}):
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


    def test_agentic_write_tool_marks_reply(self):
        # The agent can call the reversible mark_reply write tool.
        from app.database import (create_session, get_draft, get_connection as gc,
                                  insert_draft, insert_sender_profile, set_settings_bulk,
                                  upsert_professor)
        from app.models import Draft, Professor, SenderProfile
        ws = gc(self.db, workspace_id=self.kid)
        pid = upsert_professor(ws, Professor(name="P", email="p@x.edu", university="U", field="ML"))
        sp = insert_sender_profile(ws, SenderProfile(name="Me", school="HS", grade="12", email="me@x.com"))
        sid = create_session(ws, sp, notes="t")
        did = insert_draft(ws, Draft(professor_id=pid, sender_profile_id=sp, session_id=sid,
                                     subject_lines='["Hi"]', body="b", status="sent"))
        set_settings_bulk(ws, {"llm_provider": "openrouter"})
        ws.close()
        os.environ["LLM_API_KEY"] = "k"
        self.addCleanup(lambda: os.environ.pop("LLM_API_KEY", None))

        turns = [
            {"content": "", "tool_calls": [{"id": "c1", "function": {
                "name": "mark_reply", "arguments": '{"draft_id": %d, "outcome": "replied"}' % did}}]},
            {"content": "Marked it replied — they'll be skipped for follow-ups.", "tool_calls": []},
        ]
        with mock.patch("app.summarizer.chat_with_tools", side_effect=turns):
            r = self.client.post("/api/chat", json={"message": "mark draft %d as replied" % did})
        self.assertTrue(r.get_json().get("success"))
        # The write actually happened.
        ws = gc(self.db, workspace_id=self.kid)
        try:
            self.assertEqual(get_draft(ws, did).outcome, "replied")
        finally:
            ws.close()

    def _seed_faculty_and_sender(self):
        """Save one professor + sender profile and configure the LLM."""
        from app.database import (get_connection as gc, insert_sender_profile,
                                  set_settings_bulk, upsert_professor)
        from app.models import Professor, SenderProfile
        ws = gc(self.db, workspace_id=self.kid)
        pid = upsert_professor(ws, Professor(name="Jane Doe", email="j@x.edu",
                                             university="MIT", field="ML"))
        sp = insert_sender_profile(ws, SenderProfile(name="Me", school="HS", grade="12",
                                                     email="me@x.com"))
        set_settings_bulk(ws, {"llm_provider": "openrouter"})
        ws.close()
        os.environ["LLM_API_KEY"] = "k"
        self.addCleanup(lambda: os.environ.pop("LLM_API_KEY", None))
        return pid, sp

    def test_generate_draft_pauses_for_confirmation(self):
        # The agent calling generate_draft must NOT generate inline — it returns a
        # confirm payload and waits for the user.
        pid, _sp = self._seed_faculty_and_sender()
        turn = {"content": "", "tool_calls": [{"id": "c1", "function": {
            "name": "generate_draft", "arguments": '{"professor_id": %d}' % pid}}]}
        with mock.patch("app.summarizer.chat_with_tools", return_value=turn), \
                mock.patch("app.web.app.run_generation_pipeline") as gen:
            r = self.client.post("/api/chat", json={"message": "draft Jane an email"})
        data = r.get_json()
        self.assertTrue(data.get("success"), data)
        self.assertEqual(data["confirm"]["action"], "generate_draft")
        self.assertEqual(data["confirm"]["args"]["professor_id"], pid)
        gen.assert_not_called()  # nothing generated until the user confirms

    def test_confirm_endpoint_generates_draft(self):
        from app.database import (create_session, get_connection as gc, insert_draft)
        from app.generation_service import GenerationSummary
        from app.models import Draft
        pid, sp = self._seed_faculty_and_sender()

        def fake_pipeline(**kw):
            c = gc(kw["db_path"], workspace_id=kw["workspace_id"])
            sid = create_session(c, kw["sender_profile_id"], notes="t")
            insert_draft(c, Draft(professor_id=kw["professor_ids"][0],
                                  sender_profile_id=kw["sender_profile_id"], session_id=sid,
                                  subject_lines='["Hi"]', body="b", status="generated",
                                  overall_score=8.0))
            c.close()
            return GenerationSummary(session_id=sid, created=1)

        with mock.patch("app.web.app.run_generation_pipeline", side_effect=fake_pipeline):
            r = self.client.post("/api/chat/confirm",
                                 json={"action": "generate_draft", "args": {"professor_id": pid}})
        data = r.get_json()
        self.assertTrue(data.get("success"), data)
        self.assertIn("Jane Doe", data["reply"])
        self.assertIn("draft_url", data)
        # A draft now exists for that professor.
        ws = gc(self.db, workspace_id=self.kid)
        try:
            n = ws.execute("SELECT COUNT(*) c FROM drafts WHERE professor_id = ?", (pid,)).fetchone()["c"]
        finally:
            ws.close()
        self.assertEqual(n, 1)

    def test_confirm_rejects_unknown_action(self):
        r = self.client.post("/api/chat/confirm", json={"action": "send_email", "args": {}})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["success"])

    def test_confirm_malformed_professor_id_degrades_gracefully(self):
        # A non-numeric professor_id must not 500 — it should report "not found".
        self._seed_faculty_and_sender()
        r = self.client.post("/api/chat/confirm",
                             json={"action": "generate_draft", "args": {"professor_id": "not-a-number"}})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertFalse(data["success"])
        self.assertIn("not found", data["error"].lower())

    def test_agentic_tool_loop(self):
        # Model asks for a tool, we run it, model answers from the result.
        from app.database import set_settings_bulk
        ws = get_connection(self.db, workspace_id=self.kid)
        set_settings_bulk(ws, {"llm_provider": "openrouter"})
        ws.close()
        os.environ["LLM_API_KEY"] = "k"
        self.addCleanup(lambda: os.environ.pop("LLM_API_KEY", None))

        turns = [
            {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "outreach_stats", "arguments": "{}"}}]},
            {"content": "You've sent 0 so far — nothing to report yet.", "tool_calls": []},
        ]
        with mock.patch("app.summarizer.chat_with_tools", side_effect=turns) as m:
            r = self.client.post("/api/chat", json={"message": "how's my reply rate?"})
        data = r.get_json()
        self.assertTrue(data.get("success"), data)
        self.assertIn("sent 0", data["reply"])
        # Two model turns: one to request the tool, one to answer from the result.
        self.assertEqual(m.call_count, 2)


if __name__ == "__main__":
    unittest.main()
