"""Source failures surface as human-readable warnings, not raw exception reprs."""
import unittest
import types

import requests

from app.finder import _friendly_source_error


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


if __name__ == "__main__":
    unittest.main()
