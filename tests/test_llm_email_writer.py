"""render_email uses the LLM writer when a model is configured, and falls back
to the templates otherwise / on failure."""
import unittest
from unittest import mock

from app.config import load_config
from app.models import Professor, SenderProfile
from app.template_engine import render_email


class LLMEmailWriterTests(unittest.TestCase):
    def setUp(self):
        self.prof = Professor(
            name="Jane VanderPlas", field="Machine Learning", university="UW",
            keywords='["graph neural networks"]', summary="Scalable GNNs.",
            research_summary="GNNs for scientific computing.",
        )
        self.sender = SenderProfile(name="Abhay", school="Jordan HS", grade="11th grade",
                                    email="a@x.com", interests="machine learning")

    def _cfg(self, provider=None, model="anthropic/claude-sonnet-4.6"):
        cfg = load_config()
        # Config is frozen; rebuild via dataclasses.replace.
        import dataclasses
        return dataclasses.replace(cfg, llm_provider=provider, llm_api_key=("k" if provider else ""),
                                   llm_model=model)

    def test_no_model_uses_template(self):
        # No provider -> template body, template variant.
        with mock.patch("app.summarizer.write_outreach_email") as w:
            draft = render_email(self.prof, self.sender, self._cfg(provider=None),
                                 session_id=1, variant="formal")
        w.assert_not_called()
        self.assertEqual(draft.template_variant, "formal")
        self.assertIn("Jordan HS", draft.body)  # template content

    def test_model_configured_uses_llm_body(self):
        ai = ("Dear Professor VanderPlas,\n\nI read your work on graph neural networks for "
              "scientific computing and had a specific question about how it scales. I'm an "
              "11th grader teaching myself ML and could help with small coding tasks. Would "
              "you have 15 minutes sometime to talk? Either way, thank you for your time.\n\n"
              "Best,\nAbhay")
        with mock.patch("app.summarizer.write_outreach_email", return_value=ai) as w:
            draft = render_email(self.prof, self.sender, self._cfg(provider="openrouter"),
                                 session_id=1, variant="formal")
        w.assert_called_once()
        self.assertEqual(draft.body, ai)
        self.assertEqual(draft.template_variant, "ai")
        # subjects still generated
        self.assertTrue(draft.subject_lines_list)

    def test_llm_failure_falls_back_to_template(self):
        # Writer returns "" (its own error handling) -> template body is kept.
        with mock.patch("app.summarizer.write_outreach_email", return_value=""):
            draft = render_email(self.prof, self.sender, self._cfg(provider="openrouter"),
                                 session_id=1, variant="concise")
        self.assertEqual(draft.template_variant, "concise")
        self.assertIn("Jordan HS", draft.body)


if __name__ == "__main__":
    unittest.main()
