"""Email sending with Gmail API and SMTP (Gmail/Outlook/Hotmail) support.

Provides three classes:
- ``GmailAPISender`` -- create/send drafts via Gmail API with OAuth2.
- ``SMTPSender`` -- send via SMTP with STARTTLS (Gmail, Outlook, Hotmail).
- ``SafeSender`` -- wrapper with rate limiting, dedup, suppression, dry-run.
"""

from __future__ import annotations

import base64
import json
import random
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
                "Gmail API dependencies not installed. "
                "Install google-api-python-client, google-auth-oauthlib, "
                "and google-auth-httplib2."
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
        draft: Draft,
        professor: Professor,
        sender: SenderProfile,
        draft_only: bool,
    ) -> SendRecord:
        """Dispatch a single send to the appropriate backend."""
        if self._method == "gmail_draft" or (self._method == "gmail_send" and draft_only):
            gmail: GmailAPISender = self._get_gmail_sender()
            record: SendRecord = gmail.create_draft(draft, professor, sender, self._config)
            # If gmail_send and not draft_only, also send it
            if self._method == "gmail_send" and not draft_only and record.status == "success":
                send_record: SendRecord = gmail.send_draft(
                    record.gmail_draft_id or "", self._config
                )
                send_record.draft_id = draft.id or 0
                send_record.professor_id = professor.id or 0
                return send_record
            return record
        elif self._method == "smtp":
            smtp: SMTPSender = self._get_smtp_sender()
            return smtp.send(draft, professor, sender, self._config)
        else:
            return SendRecord(
                draft_id=draft.id or 0,
                professor_id=professor.id or 0,
                sent_at=_now_iso(),
                method=self._method,
                status="failed",
                error_message=f"Unknown send method: {self._method}",
            )

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

            processed: int = 0

            for draft in approved_drafts:
                if processed >= effective_limit:
                    logger.info("Session cap reached (%d)", effective_limit)
                    break

                professor: Professor | None = get_professor(conn, draft.professor_id)
                if professor is None:
                    logger.warning(
                        "Draft %d: professor_id %d not found -- skipping",
                        draft.id,
                        draft.professor_id,
                    )
                    summary["failed"] += 1
                    continue

                # Suppression check
                if is_suppressed(conn, professor.email):
                    logger.info(
                        "Draft %d: %s is suppressed -- skipping",
                        draft.id,
                        professor.email,
                    )
                    summary["skipped_suppressed"] += 1
                    continue

                # Duplicate check
                if is_duplicate_send(conn, professor.id or 0):
                    logger.info(
                        "Draft %d: duplicate send for professor %d -- skipping",
                        draft.id,
                        professor.id,
                    )
                    summary["skipped_duplicate"] += 1
                    continue

                # Rate limiting
                if not self._bucket.acquire():
                    wait: float = self._bucket.wait_time()
                    logger.info("Rate limit hit, waiting %.1f seconds", wait)
                    time.sleep(wait)
                    self._bucket.acquire()

                # Build a sender profile (fetch from DB)
                from app.database import get_sender_profile
                sender: SenderProfile | None = get_sender_profile(conn, draft.sender_profile_id)
                if sender is None:
                    logger.warning(
                        "Draft %d: sender_profile_id %d not found -- skipping",
                        draft.id,
                        draft.sender_profile_id,
                    )
                    summary["failed"] += 1
                    continue

                # Dry run
                if dry_run:
                    logger.info(
                        "[DRY RUN] Would send draft %d to %s (%s)",
                        draft.id,
                        professor.name,
                        professor.email,
                    )
                    audit_log(
                        action="send_dry_run",
                        detail=f"Dry run: draft {draft.id} to {professor.email}",
                        metadata={
                            "draft_id": draft.id,
                            "professor_email": professor.email,
                            "method": self._method,
                        },
                        db_path=db_path,
                    )
                    summary["sent"] += 1
                    processed += 1
                    continue

                # Actual send with retry (1 retry on failure)
                record: SendRecord | None = None
                for attempt in range(2):
                    record = self._send_single(draft, professor, sender, draft_only)
                    if record.status == "success":
                        break
                    if attempt == 0:
                        logger.warning(
                            "Draft %d: send attempt 1 failed, retrying... (%s)",
                            draft.id,
                            record.error_message,
                        )
                        time.sleep(2)

                if record is None:
                    summary["failed"] += 1
                    continue

                # Record to database
                record_send(conn, record)

                if record.status == "success":
                    summary["sent"] += 1
                    update_draft_status(conn, draft.id or 0, "sent")

                    # Add to suppression after successful send
                    add_suppression(conn, professor.email, reason="email_sent")

                    audit_log(
                        action="email_sent",
                        detail=f"Draft {draft.id} sent to {professor.email} via {self._method}",
                        metadata={
                            "draft_id": draft.id,
                            "professor_id": professor.id,
                            "professor_email": professor.email,
                            "method": self._method,
                            "draft_only": draft_only,
                            "gmail_draft_id": record.gmail_draft_id,
                        },
                        db_path=db_path,
                    )
                else:
                    summary["failed"] += 1
                    update_draft_status(conn, draft.id or 0, "failed")

                    audit_log(
                        action="email_failed",
                        detail=f"Draft {draft.id} failed for {professor.email}: {record.error_message}",
                        metadata={
                            "draft_id": draft.id,
                            "professor_email": professor.email,
                            "error": record.error_message,
                        },
                        db_path=db_path,
                    )

                processed += 1

                # Random cooldown between sends
                if processed < effective_limit and processed < len(approved_drafts):
                    cooldown: float = random.uniform(
                        self._config.sending.cooldown_min,
                        self._config.sending.cooldown_max,
                    )
                    logger.debug("Cooldown: %.1f seconds", cooldown)
                    time.sleep(cooldown)

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
