"""Delivery orchestration shared by manual sends and scheduled auto-send."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Config, email_provider_smtp_defaults
from app.database import (
    get_all_settings,
    get_connection,
    get_drafts,
    get_professor,
    get_sender_profiles,
    init_db,
    insert_sender_profile,
    set_settings_bulk,
)
from app.models import SenderProfile
from app.sender import SafeSender

SEND_METHODS: set[str] = {"smtp", "gmail_draft", "gmail_send"}


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


def workspace_db_path(config: Config, key_id: int | str, *, create_root: bool = True) -> str:
    """Return the per-access-key workspace database path."""
    workspace_key = str(key_id).strip()
    if not workspace_key:
        raise RuntimeError("No workspace key id was provided")

    base_db = Path(config.db_path)
    workspace_root = base_db.parent / "workspaces"
    if create_root:
        workspace_root.mkdir(parents=True, exist_ok=True)
    return str(workspace_root / f"workspace_{workspace_key}.db")


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
    )


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
    workspace_path: str,
    *,
    workspace_id: int | None = None,
    label: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run auto-send for one workspace if that workspace opted in."""
    init_db(workspace_path)
    conn = get_connection(workspace_path)
    try:
        settings = get_all_settings(conn)
        enabled = parse_bool(settings.get("auto_send_enabled"), default=False)
        if not enabled:
            return {
                "workspace_id": workspace_id,
                "label": label,
                "workspace_path": workspace_path,
                "status": "disabled",
                "success": True,
                "processed": False,
                "sent": 0,
                "failed": 0,
                "count": 0,
            }

        method = settings.get("auto_send_method", settings.get("email_provider", "smtp")).strip() or "smtp"
        if method not in SEND_METHODS:
            method = "smtp"
        limit = _parse_limit(settings.get("auto_send_limit"), default=5, maximum=20)
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
            "workspace_path": workspace_path,
            "processed": result.get("status") != "empty",
            "auto_send": True,
            "method": method,
            "limit": limit,
        })
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
        return result
    finally:
        conn.close()


def list_workspace_targets(base_cfg: Config) -> list[dict[str, Any]]:
    """Discover active non-admin workspace DBs that already exist."""
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

    targets: list[dict[str, Any]] = []
    for row in rows:
        path = workspace_db_path(base_cfg, row["id"], create_root=False)
        if not Path(path).exists():
            continue
        targets.append({
            "workspace_id": row["id"],
            "label": row["label"],
            "role": row["role"],
            "workspace_path": path,
        })
    return targets


def run_auto_send_for_workspaces(
    base_cfg: Config,
    *,
    workspace_id: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run auto-send across discovered workspaces or one requested workspace."""
    if workspace_id is not None:
        targets = [{
            "workspace_id": workspace_id,
            "label": f"Workspace {workspace_id}",
            "role": "user",
            "workspace_path": workspace_db_path(base_cfg, workspace_id, create_root=False),
        }]
    else:
        targets = list_workspace_targets(base_cfg)

    results: list[dict[str, Any]] = []
    for target in targets:
        path = target["workspace_path"]
        if not Path(path).exists():
            results.append({
                "workspace_id": target.get("workspace_id"),
                "label": target.get("label", ""),
                "workspace_path": path,
                "status": "missing_workspace",
                "success": False,
                "processed": False,
                "sent": 0,
                "failed": 0,
                "count": 0,
            })
            continue
        results.append(auto_send_workspace(
            base_cfg,
            path,
            workspace_id=target.get("workspace_id"),
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
