"""Batch generation runs the per-professor prep in parallel; verify it still
produces exactly one correct draft per eligible professor (nothing lost, no dupes).
"""
import os
import tempfile
import unittest

from app.config import load_config
from app.database import (create_access_key, create_session, get_connection,
                          get_drafts, init_db, insert_sender_profile, upsert_professor)
from app.generation_service import run_generation_pipeline
from app.models import Professor, SenderProfile


class GenerationParallelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "o.db")
        os.environ.update({"DB_PATH": self.db, "FLASK_SECRET_KEY": "t",
                           "LOG_DIR": self.tmp.name, "OUTPUT_DIR": self.tmp.name,
                           "LLM_API_KEY": ""})  # no model -> template path (fast, deterministic)
        init_db(self.db)
        conn = get_connection(self.db)
        self.kid = create_access_key(conn, "ao_gp", "T", "user", "t")
        conn.close()
        self.ws = get_connection(self.db, workspace_id=self.kid)
        self.cfg = load_config()
        self.sp = insert_sender_profile(self.ws, SenderProfile(
            name="Alice", school="HS", grade="12th", email="a@x.com", interests="ML"))
        self.pids = []
        for i in range(5):
            self.pids.append(upsert_professor(self.ws, Professor(
                name=f"Prof Number {i}", email=f"p{i}@x.edu", university="MIT", field="ML",
                research_summary=f"Researches topic {i} in machine learning, with concrete results.",
                summary=f"Works on topic {i}.", keywords='["machine learning"]',
                talking_points='["a specific anchor"]', status="new")))

    def tearDown(self):
        try: self.ws.close()
        except Exception: pass

    def test_parallel_generation_one_draft_per_professor(self):
        summary = run_generation_pipeline(
            db_path=self.db, config=self.cfg, sender_profile_id=self.sp,
            workspace_id=self.kid,
        )
        self.assertEqual(summary.created, 5)
        self.assertEqual(summary.failed, 0)
        drafts = get_drafts(self.ws)
        self.assertEqual(len(drafts), 5)
        # Exactly one draft per professor, all non-empty bodies.
        prof_ids = sorted(d.professor_id for d in drafts)
        self.assertEqual(prof_ids, sorted(self.pids))
        self.assertTrue(all(d.body.strip() for d in drafts))

    def test_professor_missing_context_is_skipped_not_failed(self):
        # A professor with no research context should be skipped cleanly even
        # in the parallel path.
        bare = upsert_professor(self.ws, Professor(
            name="Bare Prof", email="bare@x.edu", university="MIT", field="ML", status="new"))
        summary = run_generation_pipeline(
            db_path=self.db, config=self.cfg, sender_profile_id=self.sp,
            professor_ids=[bare], workspace_id=self.kid,
        )
        self.assertEqual(summary.created, 0)
        self.assertEqual(summary.skipped, 1)
        self.assertEqual(summary.failed, 0)


if __name__ == "__main__":
    unittest.main()
