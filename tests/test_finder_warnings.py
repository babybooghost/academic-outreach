"""Source failures surface as human-readable warnings, not raw exception reprs."""
import unittest
import types

import requests

from app.finder import _friendly_source_error, _visible_warnings


class FriendlySourceErrorTests(unittest.TestCase):
    def test_connection_reset_is_humanized(self):
        # The exact shape that leaked before: a wrapped ConnectionResetError.
        exc = requests.exceptions.ConnectionError(
            "('Connection aborted.', ConnectionResetError(104, 'Connection reset by peer'))")
        msg = _friendly_source_error("DBLP", exc)
        self.assertIn("DBLP", msg)
        self.assertIn("couldn't be reached", msg)
        self.assertIn("other sources still ran", msg)
        # No raw exception internals leak through.
        self.assertNotIn("ConnectionResetError", msg)
        self.assertNotIn("Connection aborted", msg)
        self.assertNotIn("104", msg)

    def test_timeout(self):
        msg = _friendly_source_error("OpenAlex", requests.exceptions.Timeout())
        self.assertIn("timed out", msg)

    def test_rate_limit_429(self):
        exc = requests.exceptions.HTTPError()
        exc.response = types.SimpleNamespace(status_code=429)
        msg = _friendly_source_error("Semantic Scholar", exc)
        self.assertIn("rate-limiting", msg)

    def test_generic_error(self):
        msg = _friendly_source_error("Crossref", requests.exceptions.RequestException("boom"))
        self.assertIn("returned an error", msg)
        self.assertNotIn("boom", msg)


class VisibleWarningTests(unittest.TestCase):
    TRANSIENT = [
        "Semantic Scholar is rate-limiting right now — the other sources still ran. "
        "Set a SEMANTIC_SCHOLAR_API_KEY for higher limits.",
        "DBLP couldn't be reached and was skipped — the other sources still ran.",
    ]
    SUBSTANTIVE = "Could not resolve 'Hogwarts' — showing unfiltered results."

    def test_results_suppress_transient_warnings(self):
        out = _visible_warnings(self.TRANSIENT + [self.SUBSTANTIVE], has_results=True)
        self.assertEqual(out, [self.SUBSTANTIVE])  # only the actionable one remains

    def test_no_results_keeps_warnings(self):
        out = _visible_warnings(self.TRANSIENT, has_results=False)
        self.assertEqual(out, self.TRANSIENT)  # explains why it's empty

    def test_dedupes(self):
        dupe = ["DBLP couldn't be reached and was skipped — the other sources still ran."] * 3
        self.assertEqual(_visible_warnings(dupe, has_results=False), dupe[:1])

    def test_substantive_kept_with_results(self):
        self.assertEqual(_visible_warnings([self.SUBSTANTIVE], has_results=True), [self.SUBSTANTIVE])


if __name__ == "__main__":
    unittest.main()
