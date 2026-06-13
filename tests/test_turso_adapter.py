"""Tests for the Turso HTTP adapter's keep-alive connection reuse.

The rest of the suite runs against local SQLite, so the Turso HTTP path is
never otherwise exercised. These tests mock the socket to confirm the two
behaviours that matter for performance and robustness: a single connection is
reused across many queries, and a stale socket triggers exactly one reconnect.
"""
import json
import unittest
from unittest import mock

from app import database


def _body(rows):
    """Build a Turso pipeline response body (execute result + close result)."""
    return json.dumps({
        "results": [
            {"type": "ok", "response": {"result": {
                "cols": [{"name": "id"}],
                "rows": rows,
                "affected_row_count": 0,
                "last_insert_rowid": None,
            }}},
            {"type": "ok", "response": {"result": {
                "cols": [], "rows": [], "affected_row_count": 0,
                "last_insert_rowid": None,
            }}},
        ]
    }).encode()


_ONE_ROW = [[{"type": "integer", "value": "1"}]]


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body


class _FakeHTTPS:
    """Stand-in for http.client.HTTPSConnection driven by a FIFO script."""

    behaviors: list = []      # ("ok", body) or ("raise",), consumed per request()
    instances: list = []      # every connection object constructed

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.closed = False
        self._pending = None
        _FakeHTTPS.instances.append(self)

    def request(self, method, path, body=None, headers=None):
        behavior = _FakeHTTPS.behaviors.pop(0)
        if behavior[0] == "raise":
            raise OSError("stale keep-alive socket")
        self._pending = behavior[1]

    def getresponse(self):
        return _FakeResponse(200, self._pending)

    def close(self):
        self.closed = True


class TursoKeepAliveTests(unittest.TestCase):
    def setUp(self):
        _FakeHTTPS.behaviors = []
        _FakeHTTPS.instances = []
        self._patches = [
            mock.patch.object(database, "_TURSO_URL", "libsql://demo.turso.io"),
            mock.patch.object(database, "_TURSO_TOKEN", "tok"),
            mock.patch.object(database._http_client, "HTTPSConnection", _FakeHTTPS),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_connection_is_reused_across_queries(self):
        _FakeHTTPS.behaviors = [("ok", _body(_ONE_ROW)), ("ok", _body(_ONE_ROW))]
        conn = database._TursoHTTPConnection()

        self.assertEqual(conn.execute("SELECT id FROM t").fetchone()["id"], 1)
        self.assertEqual(conn.execute("SELECT id FROM t").fetchone()["id"], 1)

        # Two queries, but only one socket ever opened.
        self.assertEqual(len(_FakeHTTPS.instances), 1)

    def test_stale_socket_reconnects_once(self):
        _FakeHTTPS.behaviors = [("raise",), ("ok", _body(_ONE_ROW))]
        conn = database._TursoHTTPConnection()

        self.assertEqual(conn.execute("SELECT id FROM t").fetchone()["id"], 1)

        # First socket failed and was dropped; a second was opened for the retry.
        self.assertEqual(len(_FakeHTTPS.instances), 2)
        self.assertTrue(_FakeHTTPS.instances[0].closed)

    def test_persistent_failure_raises(self):
        _FakeHTTPS.behaviors = [("raise",), ("raise",)]
        conn = database._TursoHTTPConnection()
        with self.assertRaises(Exception):
            conn.execute("SELECT id FROM t")


if __name__ == "__main__":
    unittest.main()
