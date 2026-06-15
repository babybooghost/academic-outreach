"""The richer sender-profile fields (awards/skills/goal) must surface in drafts."""
import unittest

from app.config import load_config
from app.models import Professor, SenderProfile
from app.template_engine import generate_subject_lines, render_email


class ProfileEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.prof = Professor(
            name="Jane VanderPlas", field="Machine Learning", university="UW",
            keywords='["graph neural networks"]',
            summary="Scalable graph neural networks.",
            research_summary="GNNs for scientific computing.",
        )

    def _sender(self, **kw):
        base = dict(name="Abhay Shankar", school="Jordan HS", grade="11th grade",
                    email="a@x.com", interests="machine learning")
        base.update(kw)
        return SenderProfile(**base)

    def test_awards_skills_and_goal_appear(self):
        s = self._sender(
            awards="USACO Gold and a regional science-fair win",
            skills="Python and PyTorch",
            goal="a 15-minute chat about your GNN work",
        )
        for variant in ("formal", "concise", "enthusiastic", "research_focused"):
            body = render_email(self.prof, s, self.cfg, session_id=1, variant=variant).body
            self.assertIn("USACO Gold", body, variant)
            self.assertIn("PyTorch", body, variant)
            self.assertIn("15-minute chat about your GNN work", body, variant)

    def test_empty_extras_omit_credentials_cleanly(self):
        # No awards/skills/goal -> no dangling 'A bit about my background' line.
        body = render_email(self.prof, self._sender(), self.cfg, session_id=1, variant="formal").body
        self.assertNotIn("A bit about my background", body)
        self.assertNotIn("On the technical side", body)

    def _affiliation_is_grammatical(self, body: str, variant: str):
        # Never the broken forms a blank grade/school used to produce.
        self.assertNotIn("a  student", body, variant)      # blank grade
        self.assertNotIn("student at .", body, variant)    # blank school
        self.assertNotIn("student at  ", body, variant)
        self.assertNotIn(" at .", body, variant)

    def test_blank_grade_reads_cleanly(self):
        s = self._sender(grade="")
        for variant in ("formal", "concise", "enthusiastic", "research_focused"):
            body = render_email(self.prof, s, self.cfg, session_id=1, variant=variant).body
            self._affiliation_is_grammatical(body, variant)
            self.assertIn("a student at Jordan HS", body, variant)

    def test_blank_school_reads_cleanly(self):
        s = self._sender(school="")
        for variant in ("formal", "concise", "enthusiastic", "research_focused"):
            body = render_email(self.prof, s, self.cfg, session_id=1, variant=variant).body
            self._affiliation_is_grammatical(body, variant)
            self.assertIn("a 11th grade student", body, variant)
            self.assertNotIn("11th grade student at", body, variant)  # no dangling 'at'

    def test_both_blank_reads_cleanly(self):
        s = self._sender(grade="", school="")
        for variant in ("formal", "concise", "enthusiastic", "research_focused"):
            body = render_email(self.prof, s, self.cfg, session_id=1, variant=variant).body
            self._affiliation_is_grammatical(body, variant)
            self.assertIn("a student", body, variant)

    def test_subject_lines_grade_aware(self):
        # Blank grade must never leave a dangling "- student" or double space,
        # and a non-high-school grade must not be mislabeled "high school".
        for seed in range(12):
            blanks = generate_subject_lines(self.prof, self._sender(grade=""), self.cfg, seed=seed)
            for s in blanks:
                self.assertNotIn(" - student", s, s)
                self.assertNotIn("-  student", s, s)
                self.assertNotIn("  ", s, s)
            ug = generate_subject_lines(self.prof, self._sender(grade="undergraduate"), self.cfg, seed=seed)
            for s in ug:
                self.assertNotIn("high school", s.lower(), s)


if __name__ == "__main__":
    unittest.main()
