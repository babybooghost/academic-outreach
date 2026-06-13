#!/usr/bin/env python3
"""End-to-end SMTP self-test for Academic Outreach.

Proves the real send path works without needing live mailbox credentials:
stands up a local SMTP server (STARTTLS + AUTH), points the app's own
``send_mailbox_test`` / ``SMTPSender`` at it, and verifies the message is
actually transmitted with the right envelope, subject, and body.

Run:  python scripts/smtp_selftest.py
Needs aiosmtpd (pip install aiosmtpd) and a self-signed cert pair; the script
generates one with `openssl` if CERT/KEY env vars are not provided.
"""

from __future__ import annotations

import os
import socket
import ssl
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_cert(tmp: Path) -> tuple[str, str]:
    cert, key = str(tmp / "cert.pem"), str(tmp / "key.pem")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key,
         "-out", cert, "-days", "1", "-nodes", "-subj", "/CN=localhost"],
        check=True, capture_output=True,
    )
    return cert, key


def main() -> int:
    try:
        from aiosmtpd.controller import Controller
        from aiosmtpd.smtp import AuthResult
    except ImportError:
        print("FAIL: aiosmtpd not installed (pip install aiosmtpd)")
        return 2

    tmp = Path(tempfile.mkdtemp())
    cert = os.environ.get("AO_SMTP_CERT") or ""
    key = os.environ.get("AO_SMTP_KEY") or ""
    if not (cert and key):
        cert, key = _make_cert(tmp)

    captured: list[dict] = []

    class Handler:
        async def handle_DATA(self, server, session, envelope):  # noqa: N802
            captured.append({
                "from": envelope.mail_from,
                "to": list(envelope.rcpt_tos),
                "data": envelope.content.decode("utf-8", "replace"),
            })
            return "250 Message accepted"

    def authenticator(server, session, envelope, mechanism, auth_data):
        # Accept any credentials — we are proving transport, not auth policy.
        return AuthResult(success=True)

    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.load_cert_chain(cert, key)

    port = _free_port()
    controller = Controller(
        Handler(), hostname="127.0.0.1", port=port,
        authenticator=authenticator, auth_required=True, auth_require_tls=True,
        require_starttls=True, tls_context=tls_context,
    )
    controller.start()

    try:
        # Point the app config at the local server via env, then load it.
        os.environ.update({
            "DB_PATH": str(tmp / "selftest.db"),
            "LOG_DIR": str(tmp / "logs"),
            "OUTPUT_DIR": str(tmp / "out"),
            "SMTP_HOST": "127.0.0.1",
            "SMTP_PORT": str(port),
            "SMTP_USER": "student@example.com",
            "SMTP_PASSWORD": "app-password-test",
            "SENDER_EMAIL": "student@example.com",
            "EMAIL_PROVIDER": "gmail",
        })
        from app.config import load_config
        from app.delivery import send_mailbox_test

        cfg = load_config()

        # Exercise the exact path the "Send test email" button uses.
        result = send_mailbox_test(
            cfg, recipient="prof.curie@university.edu", sender_name="Ada Lovelace",
        )

        ok = True
        if not result.get("success"):
            print(f"FAIL: send_mailbox_test reported failure: {result}")
            ok = False
        if len(captured) != 1:
            print(f"FAIL: expected 1 delivered message, got {len(captured)}")
            ok = False
        else:
            import email as _email
            msg = captured[0]
            parsed = _email.message_from_string(msg["data"])
            subject = str(parsed["Subject"] or "")
            # Decode the (base64/QP-encoded) text/plain payload back to plaintext.
            body = ""
            for part in parsed.walk():
                if part.get_content_type() == "text/plain":
                    body = part.get_payload(decode=True).decode("utf-8", "replace")
                    break
            checks = {
                "recipient": "prof.curie@university.edu" in msg["to"],
                "from envelope": "student@example.com" in (msg["from"] or ""),
                "subject decoded": "Academic Outreach mailbox test" in subject,
                "body decoded": "test email from your Academic Outreach" in body,
            }
            for label, passed in checks.items():
                print(f"  [{'ok' if passed else 'XX'}] {label}")
                ok = ok and passed

        if ok:
            print("\nPASS: real SMTP send (STARTTLS + AUTH) delivered the message end-to-end.")
            return 0
        return 1
    finally:
        controller.stop()


if __name__ == "__main__":
    raise SystemExit(main())
