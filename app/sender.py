"""Email sending with Gmail API and SMTP (Gmail/Outlook/Hotmail) support.

Provides three classes:
- ``GmailAPISender`` -- create/send drafts via Gmail API with OAuth2.
- ``SMTPSender`` -- send via SMTP with STARTTLS (Gmail, Outlook, Hotmail).
- ``SafeSender`` -- wrapper with rate limiting, dedup, suppression, dry-run.
"""

from __future__ import annotations

import base64
import json
import os
import random
import sqlite3
import smtplib
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Optional

from app.config import Config
from app.database import (
    add_suppression,
    get_connection,
    get_draft,
    get_drafts,
    get_professor,
    get_sender_profile,
    is_duplicate_send,
    is_suppressed,
    record_send,
    update_draft_status,
)
from app.logger import audit_log, get_logger
from app.models import Draft, Professor, SendRecord, SenderProfile

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(tz=timezone.utc).isoformat()


def _is_placeholder_email(value: str | None) -> bool:
    email = (value or "").strip().lower()
    return not email or email.endswith(".placeholder")


def _build_mime_message(
    draft: Draft,
    professor: Professor,
    sender: SenderProfile,
    config: Config,
) -> MIMEMultipart:
    """Build a properly structured MIME message."""
    subject_lines: list[str] = draft.subject_lines_list
    subject: str = subject_lines[0] if subject_lines else "(No Subject)"

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{sender.name} <{config.sender_email}>"
    msg["To"] = f"{professor.name} <{professor.email}>"
    msg["Subject"] = subject
    msg["Reply-To"] = config.sender_email

    # Plain text body
    msg.attach(MIMEText(draft.body, "plain", "utf-8"))

    return msg


# ---------------------------------------------------------------------------
# Token bucket rate limiter
# ---------------------------------------------------------------------------

class _TokenBucket:
    """Simple token-bucket rate limiter."""

    def __init__(self, rate_per_hour: int) -> None:
        self._rate: float = rate_per_hour / 3600.0  # tokens per second
        self._capacity: float = float(rate_per_hour)
        self._tokens: float = float(rate_per_hour)
        self._last_refill: float = time.monotonic()

    def acquire(self) -> bool:
        """Try to consume one token.  Returns ``True`` if allowed."""
        now: float = time.monotonic()
        elapsed: float = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now

        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    def wait_time(self) -> float:
        """Seconds until the next token is available."""
        if self._tokens >= 1.0:
            return 0.0
        deficit: float = 1.0 - self._tokens
        return deficit / self._rate if self._rate > 0 else 0.0


# ---------------------------------------------------------------------------
# GmailAPISender
# ---------------------------------------------------------------------------

class GmailAPISender:
    """Create and optionally send drafts via the Gmail API (OAuth2)."""

    def __init__(self) -> None:
        self._service: Any = None

    def _get_service(self, config: Config) -> Any:
        """Lazily build and cache the Gmail API service with OAuth2."""
        if self._service is not None:
            return self._service

        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise RuntimeError(
                "Gmail API dependencies are not installed in this environment. "
                "Use SMTP for hosted sends, or install google-api-python-client, "
                "google-auth-oauthlib, and google-auth-httplib2 for local Gmail drafts."
            ) from exc

        scopes: list[str] = [
            "https://www.googleapis.com/auth/gmail.compose",
            "https://www.googleapis.com/auth/gmail.modify",
        ]

        creds: Credentials | None = None
        token_path = Path(config.gmail_token_path)
        credentials_path = Path(config.gmail_credentials_path)

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), scopes)

        if creds is None or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not credentials_path.exists():
                    raise FileNotFoundError(
                        f"Gmail credentials file not found: {credentials_path}"
                    )
                if os.environ.get("VERCEL"):
                    raise RuntimeError(
                        "Hosted Gmail OAuth setup is not supported from the web app. "
                        "Use SMTP for hosted sending, or authenticate Gmail locally first."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(credentials_path), scopes
                )
                creds = flow.run_local_server(port=0)

            token_path.parent.mkdir(parents=True, exist_ok=True)
            with open(token_path, "w", encoding="utf-8") as token_file:
                token_file.write(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)
        return self._service

    def create_draft(
        self,
        draft: Draft,
        professor: Professor,
        sender: SenderProfile,
        config: Config,
    ) -> SendRecord:
        """Create a draft in Gmail via the API. Returns a ``SendRecord``."""
        service = self._get_service(config)

        mime_msg: MIMEMultipart = _build_mime_message(draft, professor, sender, config)
        raw: str = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode("ascii")

        try:
            result: dict[str, Any] = (
                service.users()
                .drafts()
                .create(userId="me", body={"message": {"raw": raw}})
                .execute()
            )
            gmail_draft_id: str = result.get("id", "")
            message_id: str = result.get("message", {}).get("id", "")

            logger.info(
                "Gmail draft created for %s (draft_id=%s)",
                professor.email,
                gmail_draft_id,
            )

            return SendRecord(
                draft_id=draft.id or 0,
                professor_id=professor.id or 0,
                sent_at=_now_iso(),
                method="gmail_draft",
                gmail_draft_id=gmail_draft_id,
                status="success",
                message_id=message_id,
            )
        except Exception as exc:
            logger.error("Failed to create Gmail draft for %s: %s", professor.email, exc)
            return SendRecord(
                draft_id=draft.id or 0,
                professor_id=professor.id or 0,
                sent_at=_now_iso(),
                method="gmail_draft",
                status="failed",
                error_message=str(exc),
            )

    def send_draft(
        self,
        gmail_draft_id: str,
        config: Config,
    ) -> SendRecord:
        """Send an existing Gmail draft by its ID. Returns a ``SendRecord``."""
        service = self._get_service(config)

        try:
            result: dict[str, Any] = (
                service.users()
                .drafts()
                .send(userId="me", body={"id": gmail_draft_id})
                .execute()
            )
            message_id: str = result.get("id", "")

            logger.info("Gmail draft %s sent (message_id=%s)", gmail_draft_id, message_id)

            return SendRecord(
                draft_id=0,
                professor_id=0,
                sent_at=_now_iso(),
                method="gmail_send",
                gmail_draft_id=gmail_draft_id,
                status="success",
                message_id=message_id,
            )
        except Exception as exc:
            logger.error("Failed to send Gmail draft %s: %s", gmail_draft_id, exc)
            return SendRecord(
                draft_id=0,
                professor_id=0,
                sent_at=_now_iso(),
                method="gmail_send",
                gmail_draft_id=gmail_draft_id,
                status="failed",
                error_message=str(exc),
            )


# ---------------------------------------------------------------------------
# SMTPSender
# ---------------------------------------------------------------------------

class SMTPSender:
    """Send emails via SMTP with STARTTLS.

    Works with Gmail (smtp.gmail.com:587), Outlook (smtp-mail.outlook.com:587),
    and Hotmail (same as Outlook).
    """

    def send(
        self,
        draft: Draft,
        professor: Professor,
        sender: SenderProfile,
        config: Config,
    ) -> SendRecord:
        """Send an email via SMTP. Returns a ``SendRecord``."""
        mime_msg: MIMEMultipart = _build_mime_message(draft, professor, sender, config)

        try:
            with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(config.smtp_user, config.smtp_password)
                server.send_message(mime_msg)

            logger.info(
                "SMTP email sent to %s via %s:%d",
                professor.email,
                config.smtp_host,
                config.smtp_port,
            )

            return SendRecord(
                draft_id=draft.id or 0,
                professor_id=professor.id or 0,
                sent_at=_now_iso(),
                method="smtp",
                status="success",
            )
        except smtplib.SMTPAuthenticationError as exc:
            logger.error(
                "SMTP authentication failed for %s:%d -- %s",
                config.smtp_host,
                config.smtp_port,
                exc,
            )
            return SendRecord(
                draft_id=draft.id or 0,
                professor_id=professor.id or 0,
                sent_at=_now_iso(),
                method="smtp",
                status="failed",
                error_message=f"Authentication failed: {exc}",
            )
        except smtplib.SMTPException as exc:
            logger.error("SMTP error sending to %s: %s", professor.email, exc)
            return SendRecord(
                draft_id=draft.id or 0,
                professor_id=professor.id or 0,
                sent_at=_now_iso(),
                method="smtp",
                status="failed",
                error_message=str(exc),
            )
        except OSError as exc:
            logger.error("Network error sending to %s: %s", professor.email, exc)
            return SendRecord(
                draft_id=draft.id or 0,
                professor_id=professor.id or 0,
                sent_at=_now_iso(),
                method="smtp",
                status="failed",
                error_message=str(exc),
            )


# ---------------------------------------------------------------------------
# SafeSender
# ---------------------------------------------------------------------------

class SafeSender:
    """Wrapper with rate limiting, dedup, suppression, and dry-run support.

    Parameters
    ----------
    config : Config
        Application configuration.
    method : str
        One of ``"gmail_draft"``, ``"gmail_send"``, ``"smtp"``.
        Default is ``"gmail_draft"`` (creates Gmail drafts without sending).
    """

    def __init__(self, config: Config, method: str = "gmail_draft") -> None:
        self._config: Config = config
        self._method: str = method
        self._bucket: _TokenBucket = _TokenBucket(config.sending.rate_limit_per_hour)
        self._gmail_sender: GmailAPISender | None = None
        self._smtp_sender: SMTPSender | None = None

    def _resolve_method(self, method: str | None) -> str:
        return method or self._method

    def _resolve_draft_only(
        self,
        method: str,
        draft_only: bool | None,
    ) -> bool:
        if draft_only is not None:
            return draft_only
        return method == "gmail_draft"

    def validate_configuration(self, method: str | None = None) -> list[str]:
        """Return user-facing setup errors before attempting a live send."""
        effective_method = self._resolve_method(method)
        errors: list[str] = []

        if effective_method not in {"gmail_draft", "gmail_send", "smtp"}:
            return [f"Unknown send method: {effective_method}"]

        if effective_method == "smtp":
            if not self._config.smtp_host:
                errors.append("SMTP host is not configured.")
            if not self._config.smtp_port:
                errors.append("SMTP port is not configured.")
            if not self._config.smtp_user:
                errors.append("SMTP username is required.")
            if not self._config.smtp_password:
                errors.append("SMTP app password is required.")
            if _is_placeholder_email(self._config.sender_email):
                errors.append("Sender email must be a real inbox, not a placeholder.")
            elif "@" not in self._config.sender_email:
                errors.append("Sender email must be a valid email address.")
            return errors

        credentials_path = Path(self._config.gmail_credentials_path)
        if not credentials_path.exists():
            errors.append(
                "Gmail API credentials file was not found. Use SMTP for hosted delivery or add Gmail OAuth credentials."
            )
        return errors

    def _resolve_context(
        self,
        conn: sqlite3.Connection,
        draft: Draft,
        professor: Professor | None = None,
        sender_profile: SenderProfile | None = None,
    ) -> tuple[Professor, SenderProfile]:
        resolved_professor = professor or get_professor(conn, draft.professor_id)
        if resolved_professor is None:
            raise RuntimeError(
                f"Professor {draft.professor_id} for draft {draft.id} was not found."
            )

        resolved_sender = sender_profile or get_sender_profile(conn, draft.sender_profile_id)
        if resolved_sender is None:
            raise RuntimeError(
                f"Sender profile {draft.sender_profile_id} for draft {draft.id} was not found."
            )

        return resolved_professor, resolved_sender

    def _get_gmail_sender(self) -> GmailAPISender:
        if self._gmail_sender is None:
            self._gmail_sender = GmailAPISender()
        return self._gmail_sender

    def _get_smtp_sender(self) -> SMTPSender:
        if self._smtp_sender is None:
            self._smtp_sender = SMTPSender()
        return self._smtp_sender

    def _send_single(
        self,
        method: str,
        draft: Draft,
        professor: Professor,
        sender: SenderProfile,
        draft_only: bool,
    ) -> SendRecord:
        """Dispatch a single send to the appropriate backend."""
        if method == "gmail_draft" or (method == "gmail_send" and draft_only):
            gmail: GmailAPISender = self._get_gmail_sender()
            record: SendRecord = gmail.create_draft(draft, professor, sender, self._config)
            # If gmail_send and not draft_only, also send it
            if method == "gmail_send" and not draft_only and record.status == "success":
                send_record: SendRecord = gmail.send_draft(
                    record.gmail_draft_id or "", self._config
                )
                send_record.draft_id = draft.id or 0
                send_record.professor_id = professor.id or 0
                return send_record
            return record
        elif method == "smtp":
            smtp: SMTPSender = self._get_smtp_sender()
            return smtp.send(draft, professor, sender, self._config)
        else:
            return SendRecord(
                draft_id=draft.id or 0,
                professor_id=professor.id or 0,
                sent_at=_now_iso(),
                method=method,
                status="failed",
                error_message=f"Unknown send method: {method}",
            )

    def send(
        self,
        draft: Draft,
        method: str | None = None,
        *,
        conn: sqlite3.Connection | None = None,
        professor: Professor | None = None,
        sender_profile: SenderProfile | None = None,
        draft_only: bool | None = None,
        dry_run: bool = False,
    ) -> SendRecord:
        """Send a single draft and persist the outcome.

        Raises
        ------
        RuntimeError
            If the draft cannot be sent or the send backend reports failure.
        """
        owned_conn = conn is None
        effective_method = self._resolve_method(method)
        effective_draft_only = self._resolve_draft_only(effective_method, draft_only)
        active_conn = conn or get_connection(self._config.db_path)

        try:
            if not dry_run:
                config_errors = self.validate_configuration(effective_method)
                if config_errors:
                    raise RuntimeError("; ".join(config_errors))

            resolved_professor, resolved_sender = self._resolve_context(
                active_conn,
                draft,
                professor=professor,
                sender_profile=sender_profile,
            )

            if is_suppressed(active_conn, resolved_professor.email):
                raise RuntimeError(
                    f"{resolved_professor.email} is on the suppression list."
                )

            if is_duplicate_send(active_conn, resolved_professor.id or 0):
                raise RuntimeError(
                    f"{resolved_professor.email} has already been contacted."
                )

            if not self._bucket.acquire():
                wait = self._bucket.wait_time()
                logger.info("Rate limit hit, waiting %.1f seconds", wait)
                time.sleep(wait)
                self._bucket.acquire()

            if dry_run:
                audit_log(
                    action="send_dry_run",
                    detail=f"Dry run: draft {draft.id} to {resolved_professor.email}",
                    metadata={
                        "draft_id": draft.id,
                        "professor_email": resolved_professor.email,
                        "method": effective_method,
                    },
                    db_path=self._config.db_path,
                )
                return SendRecord(
                    draft_id=draft.id or 0,
                    professor_id=resolved_professor.id or 0,
                    sent_at=_now_iso(),
                    method=effective_method,
                    status="success",
                )

            record: SendRecord | None = None
            for attempt in range(2):
                record = self._send_single(
                    effective_method,
                    draft,
                    resolved_professor,
                    resolved_sender,
                    effective_draft_only,
                )
                if record.status == "success":
                    break
                if attempt == 0:
                    logger.warning(
                        "Draft %d: first send attempt failed, retrying... (%s)",
                        draft.id,
                        record.error_message,
                    )
                    time.sleep(2)

            if record is None:
                raise RuntimeError("Sending failed before a result could be recorded.")

            record_send(active_conn, record)

            if record.status != "success":
                update_draft_status(active_conn, draft.id or 0, "failed")
                audit_log(
                    action="email_failed",
                    detail=f"Draft {draft.id} failed for {resolved_professor.email}: {record.error_message}",
                    metadata={
                        "draft_id": draft.id,
                        "professor_email": resolved_professor.email,
                        "error": record.error_message,
                        "method": effective_method,
                    },
                    db_path=self._config.db_path,
                )
                raise RuntimeError(record.error_message or "Sending failed.")

            update_draft_status(active_conn, draft.id or 0, "sent")
            add_suppression(active_conn, resolved_professor.email, reason="email_sent")
            audit_log(
                action="email_sent",
                detail=f"Draft {draft.id} sent to {resolved_professor.email} via {effective_method}",
                metadata={
                    "draft_id": draft.id,
                    "professor_id": resolved_professor.id,
                    "professor_email": resolved_professor.email,
                    "method": effective_method,
                    "draft_only": effective_draft_only,
                    "gmail_draft_id": record.gmail_draft_id,
                },
                db_path=self._config.db_path,
            )
            return record
        finally:
            if owned_conn:
                active_conn.close()

    def send_many(
        self,
        drafts: list[Draft],
        *,
        conn: sqlite3.Connection | None = None,
        method: str | None = None,
        draft_only: bool | None = None,
        dry_run: bool = False,
        cooldown: bool = False,
    ) -> list[dict[str, Any]]:
        """Send multiple drafts and return user-facing result objects."""
        owned_conn = conn is None
        active_conn = conn or get_connection(self._config.db_path)
        results: list[dict[str, Any]] = []
        effective_method = self._resolve_method(method)

        try:
            for index, draft in enumerate(drafts):
                try:
                    professor = get_professor(active_conn, draft.professor_id)
                    record = self.send(
                        draft,
                        method=effective_method,
                        conn=active_conn,
                        professor=professor,
                        draft_only=draft_only,
                        dry_run=dry_run,
                    )
                    results.append({
                        "draft_id": draft.id,
                        "professor": professor.name if professor else "Unknown",
                        "email": professor.email if professor else "",
                        "method": record.method,
                        "status": "dry_run" if dry_run else "sent",
                    })
                except Exception as exc:
                    professor = get_professor(active_conn, draft.professor_id)
                    results.append({
                        "draft_id": draft.id,
                        "professor": professor.name if professor else "Unknown",
                        "email": professor.email if professor else "",
                        "method": effective_method,
                        "status": "failed",
                        "error": str(exc),
                    })

                if cooldown and index < len(drafts) - 1 and not dry_run:
                    pause = random.uniform(
                        self._config.sending.cooldown_min,
                        self._config.sending.cooldown_max,
                    )
                    logger.debug("Cooldown: %.1f seconds", pause)
                    time.sleep(pause)

            return results
        finally:
            if owned_conn:
                active_conn.close()

    def send_batch(
        self,
        db_path: str,
        limit: int | None = None,
        dry_run: bool = False,
        draft_only: bool = True,
    ) -> dict[str, Any]:
        """Send a batch of approved drafts with full safety controls.

        Parameters
        ----------
        db_path : str
            Path to the SQLite database.
        limit : int, optional
            Maximum number of drafts to process. Defaults to the session cap.
        dry_run : bool
            If ``True``, log actions without actually sending.
        draft_only : bool
            If ``True`` (default), create Gmail drafts without sending them.

        Returns
        -------
        dict
            Summary with keys: ``sent``, ``failed``, ``skipped_duplicate``,
            ``skipped_suppressed``, ``dry_run``.
        """
        effective_limit: int = limit if limit is not None else self._config.sending.session_cap

        summary: dict[str, Any] = {
            "sent": 0,
            "failed": 0,
            "skipped_duplicate": 0,
            "skipped_suppressed": 0,
            "dry_run": dry_run,
        }

        conn = get_connection(db_path)
        try:
            approved_drafts: list[Draft] = get_drafts(conn, status="approved")

            if not approved_drafts:
                logger.info("No approved drafts to send")
                return summary
            results = self.send_many(
                approved_drafts[:effective_limit],
                conn=conn,
                method=self._method,
                draft_only=draft_only,
                dry_run=dry_run,
                cooldown=not dry_run,
            )
            summary["sent"] = sum(
                1 for result in results if result["status"] in {"sent", "dry_run"}
            )
            summary["failed"] = sum(
                1 for result in results if result["status"] == "failed"
            )

        finally:
            conn.close()

        logger.info(
            "Batch complete: sent=%d failed=%d dup=%d suppressed=%d dry_run=%s",
            summary["sent"],
            summary["failed"],
            summary["skipped_duplicate"],
            summary["skipped_suppressed"],
            summary["dry_run"],
        )

        audit_log(
            action="send_batch_completed",
            detail=f"Batch send completed: {json.dumps(summary)}",
            metadata=summary,
            db_path=db_path,
        )

        return summary
