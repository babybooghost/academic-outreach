"""
Centralized logging module for the Academic Outreach Email System.

Provides:
    - ``get_logger(name)`` -- returns a stdlib logger with daily-rotated file
      handler *and* a rich console handler.
    - ``audit_log(...)`` -- writes structured audit entries to both the log file
      and (optionally) an SQLite ``audit_log`` table.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import traceback
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from rich.console import Console
from rich.logging import RichHandler

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
_LOG_DIR: Optional[Path] = None
_CONSOLE: Console = Console(stderr=True)
_INITIALIZED_LOGGERS: Dict[str, logging.Logger] = {}
_DEFAULT_LOG_LEVEL: int = logging.INFO
_DATE_FMT: str = "%Y-%m-%d %H:%M:%S"
_FILE_FMT: str = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


# ---------------------------------------------------------------------------
# Initialization helper
# ---------------------------------------------------------------------------
def _resolve_log_dir(log_dir: Optional[str] = None) -> Path:
    """Return a concrete, existing log directory."""
    global _LOG_DIR  # noqa: PLW0603
    if log_dir is not None:
        _LOG_DIR = Path(log_dir)
    if _LOG_DIR is None:
        # Fallback: <project_root>/logs
        _LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    return _LOG_DIR


def init_logging(
    log_dir: Optional[str] = None,
    level: int = logging.INFO,
) -> None:
    """
    One-time bootstrap called early in the application lifecycle.

    Sets the global log directory and default level so that subsequent
    ``get_logger`` calls produce consistently configured loggers.
    """
    global _DEFAULT_LOG_LEVEL  # noqa: PLW0603
    _DEFAULT_LOG_LEVEL = level
    _resolve_log_dir(log_dir)


# ---------------------------------------------------------------------------
# Public: get_logger
# ---------------------------------------------------------------------------
def get_logger(
    name: str,
    log_dir: Optional[str] = None,
    level: Optional[int] = None,
) -> logging.Logger:
    """
    Return a named logger with a daily-rotated file handler and rich console
    handler.  Safe to call multiple times with the same *name* -- the
    handlers are attached only once.

    Parameters
    ----------
    name : str
        Logger name (typically ``__name__``).
    log_dir : str, optional
        Override the log directory for this logger.
    level : int, optional
        Override the log level for this logger.
    """
    if name in _INITIALIZED_LOGGERS:
        return _INITIALIZED_LOGGERS[name]

    effective_dir: Path = _resolve_log_dir(log_dir)
    effective_level: int = level if level is not None else _DEFAULT_LOG_LEVEL

    logger: logging.Logger = logging.getLogger(name)
    logger.setLevel(effective_level)
    # Prevent duplicate propagation when the root logger also has handlers
    logger.propagate = False

    # --- Daily rotating file handler ---
    today_str: str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    log_file: Path = effective_dir / f"outreach_{today_str}.log"
    file_handler: TimedRotatingFileHandler = TimedRotatingFileHandler(
        filename=str(log_file),
        when="midnight",
        interval=1,
        backupCount=30,  # keep ~1 month of daily logs
        encoding="utf-8",
        utc=True,
    )
    file_handler.setLevel(effective_level)
    file_handler.setFormatter(logging.Formatter(_FILE_FMT, datefmt=_DATE_FMT))
    logger.addHandler(file_handler)

    # --- Rich console handler ---
    console_handler: RichHandler = RichHandler(
        console=_CONSOLE,
        show_time=True,
        show_path=False,
        rich_tracebacks=True,
        tracebacks_show_locals=False,
        markup=True,
    )
    console_handler.setLevel(effective_level)
    logger.addHandler(console_handler)

    _INITIALIZED_LOGGERS[name] = logger
    return logger


# ---------------------------------------------------------------------------
# SQLite audit table bootstrap
# ---------------------------------------------------------------------------
_AUDIT_TABLE_DDL: str = """
CREATE TABLE IF NOT EXISTS audit_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT    NOT NULL DEFAULT (datetime('now')),
    action           TEXT    NOT NULL,
    actor_profile_id INTEGER,
    entity_type      TEXT    NOT NULL DEFAULT '',
    entity_id        INTEGER,
    details          TEXT    NOT NULL DEFAULT '{}'
);
"""


def _ensure_audit_table(db_path: str) -> None:
    """Create the audit_log table if it does not yet exist."""
    try:
        conn: sqlite3.Connection = sqlite3.connect(db_path)
        try:
            conn.execute(_AUDIT_TABLE_DDL)
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        _get_audit_file_logger().warning(
            "Could not ensure audit_log table in %s: %s",
            db_path,
            traceback.format_exc(),
        )


def _insert_audit_row(
    db_path: str,
    timestamp: str,
    action: str,
    detail: str,
    actor: str,
    metadata_json: str,
) -> None:
    """Insert a single row into the SQLite audit_log table.

    Maps the logger's (action, detail, actor, metadata) interface onto the
    database schema (action, actor_profile_id, entity_type, entity_id, details).
    """
    import json as _json
    try:
        # Merge detail and metadata into the 'details' JSON column
        combined: dict = {}
        try:
            combined = _json.loads(metadata_json) if metadata_json else {}
        except (_json.JSONDecodeError, TypeError):
            pass
        if detail:
            combined["_detail"] = detail
        if actor:
            combined["_actor"] = actor
        details_str: str = _json.dumps(combined, default=str)

        conn: sqlite3.Connection = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO audit_log (timestamp, action, entity_type, details) "
                "VALUES (?, ?, ?, ?)",
                (timestamp, action, "log", details_str),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        _get_audit_file_logger().warning(
            "Failed to write audit row to %s: %s",
            db_path,
            traceback.format_exc(),
        )


# ---------------------------------------------------------------------------
# Internal audit-only logger (avoids recursion with get_logger)
# ---------------------------------------------------------------------------
_audit_file_logger: Optional[logging.Logger] = None


def _get_audit_file_logger() -> logging.Logger:
    """Lazily create a dedicated logger for audit_log messages."""
    global _audit_file_logger  # noqa: PLW0603
    if _audit_file_logger is None:
        _audit_file_logger = get_logger("audit")
    return _audit_file_logger


# ---------------------------------------------------------------------------
# Public: audit_log
# ---------------------------------------------------------------------------
def audit_log(
    action: str,
    detail: str = "",
    actor: str = "system",
    metadata: Optional[Dict[str, Any]] = None,
    db_path: Optional[str] = None,
    db_callback: Optional[Callable[[str, str, str, str, str], None]] = None,
) -> None:
    """
    Record a structured audit entry.

    The entry is **always** written to the rotating log file.  If *db_path*
    is supplied the entry is **also** persisted to the ``audit_log`` table in
    the given SQLite database.  Alternatively a *db_callback* can be passed
    for custom persistence (receives ``timestamp, action, detail, actor,
    metadata_json``).

    Parameters
    ----------
    action : str
        Short machine-readable action tag (e.g. ``"email_sent"``).
    detail : str
        Human-readable description of what happened.
    actor : str
        Who or what triggered the action (default ``"system"``).
    metadata : dict, optional
        Arbitrary key/value pairs serialised as JSON.
    db_path : str, optional
        Path to the SQLite database for persistent audit storage.
    db_callback : callable, optional
        ``callback(timestamp, action, detail, actor, metadata_json)``
    """
    now: str = datetime.now(tz=timezone.utc).isoformat()
    metadata_json: str = json.dumps(metadata or {}, default=str)

    # -- always log to file --
    logger: logging.Logger = _get_audit_file_logger()
    logger.info(
        "AUDIT | action=%s | actor=%s | detail=%s | meta=%s",
        action,
        actor,
        detail,
        metadata_json,
    )

    # -- optionally persist to SQLite --
    if db_path:
        _ensure_audit_table(db_path)
        _insert_audit_row(db_path, now, action, detail, actor, metadata_json)

    # -- optionally invoke caller-supplied callback --
    if db_callback is not None:
        try:
            db_callback(now, action, detail, actor, metadata_json)
        except Exception:
            logger.warning(
                "audit db_callback failed: %s", traceback.format_exc()
            )
