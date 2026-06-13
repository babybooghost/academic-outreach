"""Delivery orchestration shared by manual sends and scheduled auto-send."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import Config, email_provider_smtp_defaults
from app.database import (
    get_all_settings,
    get_connection,
    get_drafts,
    get_professor,
    get_sender_profile,
    get_sender_profiles,
    insert_followup,
    insert_sender_profile,
    record_send,
    set_settings_bulk,
    update_followup_status,
)
from app.models import Draft, Professor, SenderProfile
from app.sender import SafeSender, SMTPSender
from app.template_engine import render_followup

SEND_METHODS: set[str] = {"smtp", "gmail_draft", "gmail_send"}

logger = logging.getLogger(__name__)


def parse_bool(value: object, default: bool = False) -> bool:
    """Parse checkbox/env style values into a bool."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if not normalized:
        return default
    return normalized in {"1", "true", "yes", "on", "enabled"}


def _parse_limit(value: object, default: int = 5, maximum: int = 50) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def auto_send_preferences(settings: dict[str, str]) -> tuple[bool, str, int]:
    """Normalize saved automatic delivery settings."""
    enabled = parse_bool(settings.get("auto_send_enabled"), default=False)
    method = settings.get("auto_send_method", "smtp").strip() or "smtp"
    if method not in SEND_METHODS:
        method = "smtp"
    limit = _parse_limit(settings.get("auto_send_limit"), default=5, maximum=20)
    return enabled, method, limit


def is_placeholder_email(value: str | None) -> bool:
    email = (value or "").strip().lower()
    return not email or email.endswith(".placeholder")


def infer_email_provider(email: str, default: str = "gmail") -> str:
    """Infer a reasonable provider default from the account email domain."""
    domain = email.rsplit("@", 1)[-1].strip().lower() if "@" in email else ""
    if domain in {"outlook.com", "hotmail.com", "live.com", "msn.com"}:
        return "outlook"
    if domain in {"gmail.com", "googlemail.com"}:
        return "gmail"
    return default


def seed_person_workspace_identity(
    conn: Any,
    *,
    email: str,
    display_name: str,
    default_provider: str = "gmail",
) -> None:
    """Seed a new workspace so sending belongs to the signed-up person."""
    user_email = email.strip()
    user_name = display_name.strip()
    if not user_email:
        return

    saved = get_all_settings(conn)
    provider = infer_email_provider(user_email, default=default_provider)
    settings: dict[str, str] = {}
    for key, value in {
        "workspace_owner_email": user_email,
        "workspace_owner_name": user_name,
        "sender_email": user_email,
        "smtp_user": user_email,
        "email_provider": provider,
        "auto_send_enabled": "0",
        "auto_send_method": "smtp",
        "auto_send_limit": "5",
    }.items():
        if not saved.get(key):
            settings[key] = value
    if settings:
        set_settings_bulk(conn, settings)

    existing_profiles = get_sender_profiles(conn)
    if not any(profile.email.lower() == user_email.lower() for profile in existing_profiles):
        insert_sender_profile(
            conn,
            SenderProfile(
                name=user_name or user_email.split("@", 1)[0],
                school="",
                grade="",
                email=user_email,
                interests="",
                background="",
            ),
        )


def workspace_config(base_cfg: Config, conn: Any) -> Config:
    """Apply per-workspace settings to the immutable base config."""
    saved = get_all_settings(conn)
    llm_provider = saved.get("llm_provider", base_cfg.llm_provider or "").strip() or None
    saved_provider = saved.get("email_provider")
    email_provider = (saved_provider or base_cfg.email_provider or "gmail").strip().lower()
    provider_smtp_host, provider_smtp_port = email_provider_smtp_defaults(email_provider)
    smtp_host = provider_smtp_host if saved_provider is not None else (base_cfg.smtp_host or provider_smtp_host)
    smtp_port = provider_smtp_port if saved_provider is not None else (base_cfg.smtp_port or provider_smtp_port)
    smtp_user = saved.get("smtp_user", base_cfg.smtp_user).strip()
    smtp_password = saved.get("smtp_password", base_cfg.smtp_password).strip()
    sender_email = saved.get("sender_email", base_cfg.sender_email).strip()
    workspace_owner_email = saved.get("workspace_owner_email", "").strip()

    if is_placeholder_email(sender_email) and smtp_user:
        sender_email = smtp_user
    if is_placeholder_email(sender_email) and workspace_owner_email:
        sender_email = workspace_owner_email

    return replace(
        base_cfg,
        sender_email=sender_email,
        llm_provider=llm_provider,
        llm_model=saved.get("llm_model", base_cfg.llm_model),
        email_provider=email_provider,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_password=smtp_password,
        attachment_filename=saved.get("attachment_filename", "").strip(),
        attachment_mimetype=saved.get("attachment_mimetype", "").strip(),
        attachment_b64=saved.get("attachment_b64", "").strip(),
    )


def delivery_setup_diagnostics(config: Config, conn: Any) -> list[dict[str, str]]:
    """Return setup checks that explain whether delivery can run."""
    smtp_errors = SafeSender(config, method="smtp").validate_configuration(method="smtp")
    queue_count = len(ready_send_queue(conn, 50))
    settings = get_all_settings(conn)
    auto_enabled, auto_method, auto_limit = auto_send_preferences(settings)

    checks: list[dict[str, str]] = []
    checks.append({
        "label": "Sender identity",
        "status": "ready" if config.sender_email and not is_placeholder_email(config.sender_email) else "blocked",
        "detail": config.sender_email or "No From email is set.",
    })
    checks.append({
        "label": "Mailbox login",
        "status": "ready" if config.smtp_user and config.smtp_password else "blocked",
        "detail": "SMTP credentials are present." if config.smtp_user and config.smtp_password else "SMTP username and app password are required.",
    })
    checks.append({
        "label": "Provider route",
        "status": "ready" if config.smtp_host and config.smtp_port else "blocked",
        "detail": f"{config.email_provider}: {config.smtp_host}:{config.smtp_port}",
    })
    checks.append({
        "label": "Approved queue",
        "status": "ready" if queue_count else "waiting",
        "detail": f"{queue_count} approved/edited draft(s) ready.",
    })
    checks.append({
        "label": "Automatic delivery",
        "status": "ready" if auto_enabled and not smtp_errors else ("blocked" if auto_enabled else "waiting"),
        "detail": (
            f"Enabled via {auto_method}, up to {auto_limit} per run."
            if auto_enabled else "Disabled until you opt in."
        ),
    })
    for error in smtp_errors:
        checks.append({
            "label": "Setup issue",
            "status": "blocked",
            "detail": error,
        })
    return checks


def persist_auto_send_result(conn: Any, result: dict[str, Any]) -> None:
    """Store the latest automatic delivery result for the workspace UI."""
    set_settings_bulk(conn, {
        "auto_send_last_run": datetime.now(tz=timezone.utc).isoformat(),
        "auto_send_last_status": str(result.get("status", "unknown")),
        "auto_send_last_summary": json.dumps({
            "sent": result.get("sent", 0),
            "failed": result.get("failed", 0),
            "count": result.get("count", 0),
            "status": result.get("status", "unknown"),
        }),
    })


def send_mailbox_test(
    config: Config,
    *,
    recipient: str | None = None,
    sender_name: str = "Academic Outreach",
) -> dict[str, Any]:
    """Send a one-off SMTP test email to prove the user's mailbox works."""
    errors = SafeSender(config, method="smtp").validate_configuration(method="smtp")
    if errors:
        return {
            "success": False,
            "status": "config_error",
            "error": "Email setup is incomplete.",
            "errors": errors,
        }

    target = (recipient or config.sender_email).strip()
    if "@" not in target:
        return {
            "success": False,
            "status": "invalid_recipient",
            "error": "Enter a valid test recipient email.",
        }

    now = datetime.now(tz=timezone.utc).isoformat()
    draft = Draft(
        id=0,
        professor_id=0,
        sender_profile_id=0,
        session_id=0,
        subject_lines=json.dumps(["Academic Outreach mailbox test"]),
        body=(
            "This is a test email from your Academic Outreach workspace.\n\n"
            "If this reached your inbox, the website can send through your configured mailbox.\n\n"
            f"Sent at: {now}"
        ),
        status="approved",
    )
    professor = Professor(
        id=0,
        name="Mailbox Test",
        email=target,
        university="Mailbox check",
        department="Delivery",
        field="SMTP",
    )
    sender = SenderProfile(
        id=0,
        name=sender_name or config.sender_email,
        school="",
        grade="",
        email=config.sender_email,
        interests="",
        background="",
    )

    record = SMTPSender().send(draft, professor, sender, config)
    return {
        "success": record.status == "success",
        "status": "sent" if record.status == "success" else "failed",
        "recipient": target,
        "error": record.error_message,
    }


def ready_send_queue(conn: Any, limit: int) -> list[Any]:
    """Return approved/edited drafts in send order."""
    approved = get_drafts(conn, status="approved")
    edited = get_drafts(conn, status="edited")
    return (approved + edited)[:limit]


def send_ready_queue(
    conn: Any,
    config: Config,
    *,
    method: str = "smtp",
    limit: int = 10,
    dry_run: bool = False,
    cooldown: bool = False,
) -> dict[str, Any]:
    """Process a ready queue and return a UI/API friendly summary."""
    if method not in SEND_METHODS:
        return {
            "success": False,
            "dry_run": dry_run,
            "status": "invalid_method",
            "error": f"Unknown send method: {method}",
            "results": [],
            "sent": 0,
            "failed": 0,
            "count": 0,
        }

    send_queue = ready_send_queue(conn, _parse_limit(limit, default=10, maximum=50))
    if not send_queue:
        return {
            "success": False,
            "dry_run": dry_run,
            "status": "empty",
            "error": "No approved or edited drafts are ready to send.",
            "results": [],
            "sent": 0,
            "failed": 0,
            "count": 0,
        }

    if dry_run:
        results: list[dict[str, Any]] = []
        for draft in send_queue:
            professor = get_professor(conn, draft.professor_id)
            results.append({
                "draft_id": draft.id,
                "professor": professor.name if professor else "Unknown",
                "email": professor.email if professor else "Unknown",
                "subject": draft.subject_lines_list[0] if draft.subject_lines_list else "(no subject)",
                "method": method,
                "status": "dry_run",
            })
        return {
            "success": True,
            "dry_run": True,
            "status": "preview",
            "count": len(results),
            "sent": 0,
            "failed": 0,
            "results": results,
        }

    sender = SafeSender(config, method=method)
    config_errors = sender.validate_configuration(method=method)
    if config_errors:
        return {
            "success": False,
            "dry_run": False,
            "status": "config_error",
            "error": "Email setup is incomplete. Fix Settings before running delivery.",
            "errors": config_errors,
            "results": [],
            "sent": 0,
            "failed": 0,
            "count": len(send_queue),
        }

    results = sender.send_many(
        send_queue,
        conn=conn,
        method=method,
        dry_run=False,
        cooldown=cooldown,
    )
    sent_count = sum(1 for result in results if result["status"] == "sent")
    failed_count = sum(1 for result in results if result["status"] == "failed")
    success = failed_count == 0
    return {
        "success": success,
        "dry_run": False,
        "status": "sent" if success else "failed",
        "count": len(results),
        "sent": sent_count,
        "failed": failed_count,
        "error": None if success else f"{failed_count} of {len(results)} emails failed.",
        "results": results,
    }


def auto_send_workspace(
    base_cfg: Config,
    workspace_id: int,
    *,
    label: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run auto-send for one workspace (by id) if that workspace opted in."""
    conn = get_connection(base_cfg.db_path, workspace_id=workspace_id)
    try:
        settings = get_all_settings(conn)
        enabled = parse_bool(settings.get("auto_send_enabled"), default=False)
        if not enabled:
            return {
                "workspace_id": workspace_id,
                "label": label,
                "status": "disabled",
                "success": True,
                "processed": False,
                "sent": 0,
                "failed": 0,
                "count": 0,
            }

        _enabled, method, limit = auto_send_preferences(settings)
        cfg = workspace_config(base_cfg, conn)
        result = send_ready_queue(
            conn,
            cfg,
            method=method,
            limit=limit,
            dry_run=dry_run,
            cooldown=not dry_run,
        )
        result.update({
            "workspace_id": workspace_id,
            "label": label,
            "processed": result.get("status") != "empty",
            "auto_send": True,
            "method": method,
            "limit": limit,
        })
        persist_auto_send_result(conn, result)
        return result
    finally:
        conn.close()


def list_workspace_targets(base_cfg: Config) -> list[dict[str, Any]]:
    """Return active non-admin workspaces from the shared access-key registry."""
    auth_conn = get_connection(base_cfg.db_path)
    try:
        rows = auth_conn.execute(
            """
            SELECT id, label, role
              FROM access_keys
             WHERE is_active = 1 AND role != 'admin'
             ORDER BY id
            """
        ).fetchall()
    finally:
        auth_conn.close()

    return [
        {
            "workspace_id": row["id"],
            "label": row["label"],
            "role": row["role"],
        }
        for row in rows
    ]


def run_auto_send_for_workspaces(
    base_cfg: Config,
    *,
    workspace_id: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run auto-send across all opted-in workspaces or one requested workspace."""
    if workspace_id is not None:
        targets = [{
            "workspace_id": workspace_id,
            "label": f"Workspace {workspace_id}",
            "role": "user",
        }]
    else:
        targets = list_workspace_targets(base_cfg)

    results: list[dict[str, Any]] = []
    for target in targets:
        results.append(auto_send_workspace(
            base_cfg,
            int(target["workspace_id"]),
            label=target.get("label", ""),
            dry_run=dry_run,
        ))

    return {
        "success": all(result.get("success", False) for result in results),
        "dry_run": dry_run,
        "workspace_count": len(results),
        "processed": sum(1 for result in results if result.get("processed")),
        "sent": sum(int(result.get("sent", 0)) for result in results),
        "failed": sum(int(result.get("failed", 0)) for result in results),
        "results": results,
    }


# ---------------------------------------------------------------------------
# Automatic follow-ups (one polite 7-10 day nudge, generated and sent for you)
# ---------------------------------------------------------------------------

def auto_followup_preferences(settings: dict[str, str]) -> tuple[bool, int, int]:
    """Normalize saved automatic follow-up settings: (enabled, days_since, limit)."""
    enabled = parse_bool(settings.get("auto_followup_enabled"), default=False)
    days_since = _parse_limit(settings.get("auto_followup_days"), default=7, maximum=60)
    limit = _parse_limit(settings.get("auto_followup_limit"), default=5, maximum=20)
    return enabled, days_since, limit


def _eligible_followup_drafts(conn: Any, days_since: int, limit: int) -> list[Draft]:
    """Sent drafts at least *days_since* days old that have no follow-up yet."""
    wid = getattr(conn, "workspace_id", 0)
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=max(0, days_since))).isoformat()
    rows = conn.execute(
        """
        SELECT d.*, COALESCE(MAX(sl.sent_at), d.reviewed_at, d.created_at) AS sent_when
        FROM drafts d
        LEFT JOIN send_log sl
          ON sl.draft_id = d.id AND sl.workspace_id = d.workspace_id AND sl.status = 'success'
        WHERE d.workspace_id = ? AND d.status = 'sent'
          AND d.id NOT IN (SELECT original_draft_id FROM followups WHERE workspace_id = ?)
        GROUP BY d.id
        HAVING sent_when <= ?
        ORDER BY sent_when ASC
        LIMIT ?
        """,
        (wid, wid, cutoff, limit),
    ).fetchall()
    return [Draft.from_row(r) for r in rows]


def auto_followup_workspace(
    base_cfg: Config,
    workspace_id: int,
    *,
    label: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Generate and send due follow-ups for one workspace, if it opted in."""
    conn = get_connection(base_cfg.db_path, workspace_id=workspace_id)
    base = {"workspace_id": workspace_id, "label": label}
    try:
        settings = get_all_settings(conn)
        enabled, days_since, limit = auto_followup_preferences(settings)
        if not enabled:
            return {**base, "status": "disabled", "success": True,
                    "processed": False, "generated": 0, "sent": 0, "failed": 0}

        cfg = workspace_config(base_cfg, conn)
        eligible = _eligible_followup_drafts(conn, days_since, limit)
        if not eligible:
            return {**base, "status": "empty", "success": True,
                    "processed": True, "generated": 0, "sent": 0, "failed": 0}
        if dry_run:
            return {**base, "status": "preview", "success": True, "processed": True,
                    "generated": 0, "sent": 0, "failed": 0, "count": len(eligible)}

        errors = SafeSender(cfg, method="smtp").validate_configuration(method="smtp")
        if errors:
            return {**base, "status": "config_error", "success": False,
                    "processed": False, "generated": 0, "sent": 0, "failed": 0,
                    "errors": errors}

        smtp = SMTPSender()
        generated = sent = failed = 0
        for original in eligible:
            prof = get_professor(conn, original.professor_id)
            sender_profile = get_sender_profile(conn, original.sender_profile_id)
            if (not prof or not sender_profile or not prof.email
                    or is_placeholder_email(prof.email)):
                failed += 1
                continue
            try:
                followup = render_followup(prof, sender_profile, original, cfg)
                followup_id = insert_followup(conn, followup)
                generated += 1
                # Send the follow-up directly (the recipient is already on the
                # suppression list from the first email, so the guarded send
                # path would refuse it — that guard is for first contact).
                draft_like = Draft(
                    id=original.id,
                    professor_id=prof.id or 0,
                    sender_profile_id=sender_profile.id or 0,
                    session_id=original.session_id,
                    subject_lines=json.dumps([followup.subject]),
                    body=followup.body,
                    status="approved",
                )
                record = smtp.send(draft_like, prof, sender_profile, cfg)
                record_send(conn, record)
                if record.status == "success":
                    sent += 1
                    update_followup_status(conn, followup_id, "sent")
                else:
                    failed += 1
                    update_followup_status(conn, followup_id, "failed")
            except Exception as exc:
                failed += 1
                logger.warning("Auto follow-up failed for draft %s: %s", original.id, exc)

        try:
            set_settings_bulk(conn, {
                "auto_followup_last_run": datetime.now(tz=timezone.utc).isoformat(),
                "auto_followup_last_summary": json.dumps(
                    {"generated": generated, "sent": sent, "failed": failed}
                ),
            })
        except Exception:
            pass

        return {**base, "status": "sent" if failed == 0 else "partial",
                "success": failed == 0, "processed": True,
                "generated": generated, "sent": sent, "failed": failed}
    finally:
        conn.close()


def run_auto_followup_for_workspaces(
    base_cfg: Config,
    *,
    workspace_id: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run automatic follow-ups across all opted-in workspaces or just one."""
    if workspace_id is not None:
        targets = [{"workspace_id": workspace_id, "label": f"Workspace {workspace_id}", "role": "user"}]
    else:
        targets = list_workspace_targets(base_cfg)

    results = [
        auto_followup_workspace(
            base_cfg, int(t["workspace_id"]), label=t.get("label", ""), dry_run=dry_run
        )
        for t in targets
    ]
    return {
        "success": all(r.get("success", False) for r in results),
        "dry_run": dry_run,
        "workspace_count": len(results),
        "generated": sum(int(r.get("generated", 0)) for r in results),
        "sent": sum(int(r.get("sent", 0)) for r in results),
        "failed": sum(int(r.get("failed", 0)) for r in results),
        "results": results,
    }
