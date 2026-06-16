"""Batch generation is bounded (serverless-safe) and resumable (skips drafted)."""
import os
import tempfile
import unittest

from app.config import load_config
from app.database import (create_access_key, get_connection, get_drafts,
                          init_db, insert_sender_profile, upsert_professor)
from app.generation_service import run_generation_pipeline
from app.models import Professor, SenderProfile


class BatchGenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "o.db")
        os.environ.update({"DB_PATH": self.db, "FLASK_SECRET_KEY": "t",
                           "LOG_DIR": self.tmp.name, "OUTPUT_DIR": self.tmp.name})
        init_db(self.db)
        conn = get_connection(self.db)
        self.kid = create_access_key(conn, "ao_bg", "A", "user", "t")
        conn.close()
        self.cfg = load_config()  # no LLM provider -> template path, no network
        self.ws = get_connection(self.db, workspace_id=self.kid)
        self.sp = insert_sender_profile(self.ws, SenderProfile(
            name="Me", school="HS", grade="12", email="me@x.com", interests="ML"))
        for i in range(5):
            upsert_professor(self.ws, Professor(
                name=f"Prof {i}", email=f"p{i}@x.edu", university="MIT", field="ML",
                research_summary=f"Works on topic {i} in ML.", summary=f"Topic {i}.",
                keywords='["machine learning"]'))

    def tearDown(self):
        try: self.ws.close()
        except Exception: pass

    def _run(self, **kw):
        return run_generation_pipeline(db_path=self.db, config=self.cfg,
                                       sender_profile_id=self.sp, workspace_id=self.kid, **kw)

    def test_cap_limits_batch_and_reports_remaining(self):
        s = self._run(skip_existing_drafts=True, max_professors=2)
        self.assertEqual(s.created, 2)
        self.assertEqual(s.remaining, 3)
        self.assertEqual(len(get_drafts(self.ws)), 2)

    def test_resumable_skips_already_drafted(self):
        self._run(skip_existing_drafts=True, max_professors=2)   # 2 of 5
        self._run(skip_existing_drafts=True, max_professors=2)   # next 2
        s3 = self._run(skip_existing_drafts=True, max_professors=2)  # last 1
        self.assertEqual(s3.created, 1)
        self.assertEqual(s3.remaining, 0)
        # 5 distinct professors, one draft each — no duplicates.
        drafts = get_drafts(self.ws)
        self.assertEqual(len(drafts), 5)
        self.assertEqual(len({d.professor_id for d in drafts}), 5)

    def test_all_drafted_is_graceful_not_error(self):
        self._run(skip_existing_drafts=True)  # drafts all 5
        s = self._run(skip_existing_drafts=True)  # nothing left
        self.assertEqual(s.created, 0)
        self.assertTrue(s.warnings)
        self.assertIn("already has a draft", s.warnings[0])

    def test_no_professors_at_all_still_raises(self):
        empty_ws = get_connection(self.db, workspace_id=999999)
        # a workspace with no professors
        with self.assertRaises(ValueError):
            run_generation_pipeline(db_path=self.db, config=self.cfg,
                                    sender_profile_id=self.sp, workspace_id=999999,
                                    skip_existing_drafts=True)
        empty_ws.close()


if __name__ == "__main__":
    unittest.main()
