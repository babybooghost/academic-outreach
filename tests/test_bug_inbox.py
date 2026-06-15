"""Bug reports submitted from the widget land in an admin inbox and can be triaged."""
import os
import tempfile
import unittest

from app.database import create_access_key, get_connection, init_db


class BugInboxTests(unittest.TestCase):
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
        self.kid = create_access_key(conn, "ao_bug_key", "Taudy", "user", "t")
        conn.close()
        from app.web.app import create_app  # also creates the bug_reports table
        self.app = create_app()
        self.app.config["TESTING"] = True

    def _user(self):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s.update({"authenticated": True, "key_id": self.kid, "key_label": "Taudy", "role": "user"})
        return c

    def _admin(self):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["admin_authenticated"] = True
        return c

    def test_submit_then_triage_in_admin_inbox(self):
        # User submits a bug from the widget.
        r = self._user().post("/api/bug-report",
                              json={"title": "Send button broken", "details": "clicking does nothing",
                                    "severity": "high"})
        self.assertTrue(r.get_json()["success"])
        rid = r.get_json()["report_id"]

        admin = self._admin()
        # Admin inbox lists it with the full details.
        page = admin.get("/admin/bugs")
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        self.assertIn("Send button broken", body)
        self.assertIn("clicking does nothing", body)

        # Mark it resolved.
        upd = admin.post(f"/admin/bugs/{rid}/status", data={"status": "resolved"})
        self.assertEqual(upd.status_code, 302)
        from app.database import get_bug_reports
        conn = get_connection(self.db)
        try:
            reports = get_bug_reports(conn, status="resolved")
        finally:
            conn.close()
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["id"], rid)

    def test_invalid_status_rejected(self):
        from app.database import set_bug_report_status
        conn = get_connection(self.db)
        self.addCleanup(conn.close)
        with self.assertRaises(ValueError):
            set_bug_report_status(conn, 1, "garbage")

    def test_inbox_requires_admin(self):
        # A logged-in non-admin user cannot reach the admin inbox.
        r = self._user().get("/admin/bugs")
        self.assertIn(r.status_code, (302, 401, 403))
        # Status update is gated too.
        r2 = self._user().post("/admin/bugs/1/status", data={"status": "resolved"})
        self.assertIn(r2.status_code, (302, 401, 403))


if __name__ == "__main__":
    unittest.main()
