import unittest
from unittest.mock import patch

from app.models import Professor
from app.summarizer import LLMSummarizer


class LLMSummarizerTests(unittest.TestCase):
    def test_prompt_template_escapes_literal_json_braces(self) -> None:
        summarizer = LLMSummarizer(
            provider="openai",
            api_key="test-key",
            model="gpt-test",
        )
        professor = Professor(name="Prof Prompt", field="AI")

        with patch.object(
            LLMSummarizer,
            "_call_llm",
            return_value='{"keywords":["ai","verification"],"summary":"Studies trustworthy AI."}',
        ) as mock_call:
            keywords, summary = summarizer.summarize(
                "Research text about AI verification and robust agents.",
                professor,
            )

        sent_prompt = mock_call.call_args.args[0]
        self.assertIn('{"keywords": [...], "summary": "..."}', sent_prompt)
        self.assertIn("Text: Research text about AI verification", sent_prompt)
        self.assertEqual(keywords, ["ai", "verification"])
        self.assertEqual(summary, "Studies trustworthy AI.")


if __name__ == "__main__":
    unittest.main()
