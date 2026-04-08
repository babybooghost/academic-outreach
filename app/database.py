"""SQLite database layer for the Academic Outreach Email System.

Every public function in this module accepts an explicit ``sqlite3.Connection``
so callers control transaction scope.  Use :func:`get_connection` to obtain a
connection and :func:`init_db` once at startup to ensure tables exist.

Supports both local SQLite and Turso (cloud SQLite via libsql) connections.
Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN env vars to use Turso.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Any, Optional

from app.models import (
    AuditEntry,
    Draft,
    FollowUp,
    Professor,
    SendRecord,
    SenderProfile,
    Session,
)

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Turso / libsql support
# ---------------------------------------------------------------------------

_TURSO_URL: str = os.environ.get("TURSO_DATABASE_URL", "")
_TURSO_TOKEN: str = os.environ.get("TURSO_AUTH_TOKEN", "")


def _is_turso_configured() -> bool:
    """Check if Turso credentials are available."""
    return bool(_TURSO_URL and _TURSO_TOKEN)


def _get_turso_connection() -> sqlite3.Connection:
    """Return a libsql connection to Turso (API-compatible with sqlite3)."""
    try:
        import libsql_experimental as libsql  # type: ignore[import-untyped]
        conn = libsql.connect(
            database=_TURSO_URL,
            auth_token=_TURSO_TOKEN,
        )
        conn.row_factory = sqlite3.Row
        return conn
    except ImportError:
        raise RuntimeError(
            "libsql_experimental is not installed. "
            "Install with: pip install libsql-experimental"
        )
    except Exception as exc:
        logger.error("Failed to connect to Turso: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Connection / bootstrap
# ---------------------------------------------------------------------------

def get_connection(db_path: str) -> sqlite3.Connection:
    """Return a connection with row_factory set to ``sqlite3.Row``.

    Uses Turso if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN are set,
    otherwise uses local SQLite at db_path.

    The caller is responsible for closing the connection.
    """
    if _is_turso_configured():
        return _get_turso_connection()

    try:
        conn: sqlite3.Connection = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    except sqlite3.Error as exc:
        logger.error("Failed to connect to database at %s: %s", db_path, exc)
        raise


def init_db(db_path: str) -> None:
    """Create all tables (idempotent) and enable WAL journal mode."""
    conn: sqlite3.Connection = get_connection(db_path)
    try:
        if not _is_turso_configured():
            conn.execute("PRAGMA journal_mode = WAL")

        # Execute each CREATE TABLE separately for libsql compatibility
        for statement in _SCHEMA_SQL.strip().split(";"):
            statement = statement.strip()
            if statement:
                conn.execute(statement)
        conn.commit()
        logger.info("Database initialised at %s", db_path if not _is_turso_configured() else "Turso")
    except (sqlite3.Error, Exception) as exc:
        logger.error("Database init failed: %s", exc)
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS sender_profiles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    school          TEXT    NOT NULL,
    grade           TEXT    NOT NULL,
    email           TEXT    NOT NULL,
    interests       TEXT    NOT NULL DEFAULT '',
    background      TEXT    NOT NULL DEFAULT '',
    graduation_year TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS professors (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    title            TEXT,
    email            TEXT NOT NULL UNIQUE,
    university       TEXT NOT NULL,
    department       TEXT NOT NULL,
    lab_name         TEXT,
    field            TEXT NOT NULL,
    profile_url      TEXT,
    research_summary TEXT,
    recent_work      TEXT,
    notes            TEXT,
    enrichment_text  TEXT,
    keywords         TEXT,
    summary          TEXT,
    talking_points   TEXT,
    status           TEXT NOT NULL DEFAULT 'new',
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_profile_id INTEGER NOT NULL REFERENCES sender_profiles(id),
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS drafts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    professor_id        INTEGER NOT NULL REFERENCES professors(id),
    sender_profile_id   INTEGER NOT NULL REFERENCES sender_profiles(id),
    session_id          INTEGER NOT NULL REFERENCES sessions(id),
    subject_lines       TEXT    NOT NULL DEFAULT '[]',
    body                TEXT    NOT NULL DEFAULT '',
    template_variant    TEXT    NOT NULL DEFAULT '',
    specificity_score   REAL    NOT NULL DEFAULT 0.0,
    authenticity_score  REAL    NOT NULL DEFAULT 0.0,
    relevance_score     REAL    NOT NULL DEFAULT 0.0,
    conciseness_score   REAL    NOT NULL DEFAULT 0.0,
    completeness_score  REAL    NOT NULL DEFAULT 0.0,
    overall_score       REAL    NOT NULL DEFAULT 0.0,
    similarity_score    REAL,
    warnings            TEXT    NOT NULL DEFAULT '[]',
    status              TEXT    NOT NULL DEFAULT 'generated',
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    reviewed_at         TEXT,
    reviewer_notes      TEXT
);

CREATE TABLE IF NOT EXISTS send_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id       INTEGER NOT NULL REFERENCES drafts(id),
    professor_id   INTEGER NOT NULL REFERENCES professors(id),
    sent_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    method         TEXT    NOT NULL,
    gmail_draft_id TEXT,
    status         TEXT    NOT NULL DEFAULT 'success',
    error_message  TEXT,
    message_id     TEXT
);

CREATE TABLE IF NOT EXISTS suppression_list (
    email    TEXT    NOT NULL UNIQUE,
    reason   TEXT    NOT NULL DEFAULT '',
    added_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS followups (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    original_draft_id INTEGER NOT NULL REFERENCES drafts(id),
    professor_id      INTEGER NOT NULL REFERENCES professors(id),
    sender_profile_id INTEGER NOT NULL REFERENCES sender_profiles(id),
    body              TEXT    NOT NULL DEFAULT '',
    subject           TEXT    NOT NULL DEFAULT '',
    status            TEXT    NOT NULL DEFAULT 'generated',
    scheduled_date    TEXT,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT    NOT NULL DEFAULT (datetime('now')),
    action           TEXT    NOT NULL,
    actor_profile_id INTEGER,
    entity_type      TEXT    NOT NULL,
    entity_id        INTEGER,
    details          TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS access_keys (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key_value  TEXT    NOT NULL UNIQUE,
    label      TEXT    NOT NULL DEFAULT '',
    role       TEXT    NOT NULL DEFAULT 'user',
    is_active  INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    last_used  TEXT,
    created_by TEXT    NOT NULL DEFAULT 'admin'
);

CREATE TABLE IF NOT EXISTS admin_activity_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT    NOT NULL DEFAULT (datetime('now')),
    actor_key_id  INTEGER,
    actor_label   TEXT    NOT NULL DEFAULT '',
    actor_role    TEXT    NOT NULL DEFAULT '',
    action        TEXT    NOT NULL,
    category      TEXT    NOT NULL DEFAULT 'general',
    target_type   TEXT,
    target_id     TEXT,
    details       TEXT    NOT NULL DEFAULT '{}',
    ip_address    TEXT,
    user_agent    TEXT,
    request_method TEXT,
    request_path  TEXT,
    session_id    TEXT,
    response_code INTEGER
);

CREATE TABLE IF NOT EXISTS user_signups (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email          TEXT    NOT NULL UNIQUE,
    display_name   TEXT    NOT NULL,
    password_hash  TEXT    NOT NULL,
    key_value      TEXT    NOT NULL,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.utcnow().isoformat()


# ---------------------------------------------------------------------------
# Professor CRUD
# ---------------------------------------------------------------------------

def upsert_professor(conn: sqlite3.Connection, prof: Professor) -> int:
    """Insert or update a professor by email.  Returns the row id."""
    try:
        cursor: sqlite3.Cursor = conn.execute(
            """
            INSERT INTO professors (
                name, title, email, university, department, lab_name, field,
                profile_url, research_summary, recent_work, notes,
                enrichment_text, keywords, summary, talking_points,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                name             = excluded.name,
                title            = excluded.title,
                university       = excluded.university,
                department       = excluded.department,
                lab_name         = excluded.lab_name,
                field            = excluded.field,
                profile_url      = excluded.profile_url,
                research_summary = excluded.research_summary,
                recent_work      = excluded.recent_work,
                notes            = excluded.notes,
                enrichment_text  = excluded.enrichment_text,
                keywords         = excluded.keywords,
                summary          = excluded.summary,
                talking_points   = excluded.talking_points,
                status           = excluded.status,
                updated_at       = excluded.updated_at
            """,
            (
                prof.name, prof.title, prof.email, prof.university,
                prof.department, prof.lab_name, prof.field, prof.profile_url,
                prof.research_summary, prof.recent_work, prof.notes,
                prof.enrichment_text, prof.keywords, prof.summary,
                prof.talking_points, prof.status,
                prof.created_at, _now_iso(),
            ),
        )
        conn.commit()
        return cursor.lastrowid or 0
    except sqlite3.Error as exc:
        logger.error("upsert_professor failed for %s: %s", prof.email, exc)
        conn.rollback()
        raise


def get_professor(conn: sqlite3.Connection, id: int) -> Optional[Professor]:
    """Fetch a single professor by id."""
    try:
        row: Optional[sqlite3.Row] = conn.execute(
            "SELECT * FROM professors WHERE id = ?", (id,)
        ).fetchone()
        return Professor.from_row(row) if row else None
    except sqlite3.Error as exc:
        logger.error("get_professor failed for id=%d: %s", id, exc)
        raise


def get_professors(
    conn: sqlite3.Connection,
    status: Optional[str] = None,
    field: Optional[str] = None,
) -> list[Professor]:
    """Return professors, optionally filtered by status and/or field."""
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if field is not None:
            clauses.append("field = ?")
            params.append(field)

        query: str = "SELECT * FROM professors"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id"

        rows: list[sqlite3.Row] = conn.execute(query, params).fetchall()
        return [Professor.from_row(r) for r in rows]
    except sqlite3.Error as exc:
        logger.error("get_professors failed: %s", exc)
        raise


def update_professor(conn: sqlite3.Connection, prof: Professor) -> None:
    """Full update of a professor row (must have id set)."""
    if prof.id is None:
        raise ValueError("Cannot update professor without an id")
    try:
        conn.execute(
            """
            UPDATE professors SET
                name = ?, title = ?, email = ?, university = ?,
                department = ?, lab_name = ?, field = ?, profile_url = ?,
                research_summary = ?, recent_work = ?, notes = ?,
                enrichment_text = ?, keywords = ?, summary = ?,
                talking_points = ?, status = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                prof.name, prof.title, prof.email, prof.university,
                prof.department, prof.lab_name, prof.field, prof.profile_url,
                prof.research_summary, prof.recent_work, prof.notes,
                prof.enrichment_text, prof.keywords, prof.summary,
                prof.talking_points, prof.status, _now_iso(), prof.id,
            ),
        )
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("update_professor failed for id=%s: %s", prof.id, exc)
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Draft CRUD
# ---------------------------------------------------------------------------

def insert_draft(conn: sqlite3.Connection, draft: Draft) -> int:
    """Insert a new draft and return its id."""
    try:
        cursor: sqlite3.Cursor = conn.execute(
            """
            INSERT INTO drafts (
                professor_id, sender_profile_id, session_id,
                subject_lines, body, template_variant,
                specificity_score, authenticity_score, relevance_score,
                conciseness_score, completeness_score, overall_score,
                similarity_score, warnings, status, created_at,
                reviewed_at, reviewer_notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draft.professor_id, draft.sender_profile_id, draft.session_id,
                draft.subject_lines, draft.body, draft.template_variant,
                draft.specificity_score, draft.authenticity_score,
                draft.relevance_score, draft.conciseness_score,
                draft.completeness_score, draft.overall_score,
                draft.similarity_score, draft.warnings, draft.status,
                draft.created_at, draft.reviewed_at, draft.reviewer_notes,
            ),
        )
        conn.commit()
        return cursor.lastrowid or 0
    except sqlite3.Error as exc:
        logger.error("insert_draft failed: %s", exc)
        conn.rollback()
        raise


def get_draft(conn: sqlite3.Connection, id: int) -> Optional[Draft]:
    """Fetch a single draft by id."""
    try:
        row: Optional[sqlite3.Row] = conn.execute(
            "SELECT * FROM drafts WHERE id = ?", (id,)
        ).fetchone()
        return Draft.from_row(row) if row else None
    except sqlite3.Error as exc:
        logger.error("get_draft failed for id=%d: %s", id, exc)
        raise


def get_drafts(
    conn: sqlite3.Connection,
    session_id: Optional[int] = None,
    status: Optional[str] = None,
) -> list[Draft]:
    """Return drafts, optionally filtered by session_id and/or status."""
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)

        query: str = "SELECT * FROM drafts"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id"

        rows: list[sqlite3.Row] = conn.execute(query, params).fetchall()
        return [Draft.from_row(r) for r in rows]
    except sqlite3.Error as exc:
        logger.error("get_drafts failed: %s", exc)
        raise


def update_draft(conn: sqlite3.Connection, draft: Draft) -> None:
    """Full update of a draft row (must have id set)."""
    if draft.id is None:
        raise ValueError("Cannot update draft without an id")
    try:
        conn.execute(
            """
            UPDATE drafts SET
                professor_id = ?, sender_profile_id = ?, session_id = ?,
                subject_lines = ?, body = ?, template_variant = ?,
                specificity_score = ?, authenticity_score = ?,
                relevance_score = ?, conciseness_score = ?,
                completeness_score = ?, overall_score = ?,
                similarity_score = ?, warnings = ?, status = ?,
                reviewed_at = ?, reviewer_notes = ?
            WHERE id = ?
            """,
            (
                draft.professor_id, draft.sender_profile_id, draft.session_id,
                draft.subject_lines, draft.body, draft.template_variant,
                draft.specificity_score, draft.authenticity_score,
                draft.relevance_score, draft.conciseness_score,
                draft.completeness_score, draft.overall_score,
                draft.similarity_score, draft.warnings, draft.status,
                draft.reviewed_at, draft.reviewer_notes, draft.id,
            ),
        )
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("update_draft failed for id=%s: %s", draft.id, exc)
        conn.rollback()
        raise


def update_draft_status(
    conn: sqlite3.Connection,
    draft_id: int,
    status: str,
    notes: Optional[str] = None,
) -> None:
    """Lightweight status-only update for a draft."""
    try:
        conn.execute(
            """
            UPDATE drafts
               SET status = ?, reviewed_at = ?, reviewer_notes = ?
             WHERE id = ?
            """,
            (status, _now_iso(), notes, draft_id),
        )
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("update_draft_status failed for id=%d: %s", draft_id, exc)
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# SendRecord
# ---------------------------------------------------------------------------

def record_send(conn: sqlite3.Connection, record: SendRecord) -> int:
    """Insert a send-log entry and return its id."""
    try:
        cursor: sqlite3.Cursor = conn.execute(
            """
            INSERT INTO send_log (
                draft_id, professor_id, sent_at, method,
                gmail_draft_id, status, error_message, message_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.draft_id, record.professor_id, record.sent_at,
                record.method, record.gmail_draft_id, record.status,
                record.error_message, record.message_id,
            ),
        )
        conn.commit()
        return cursor.lastrowid or 0
    except sqlite3.Error as exc:
        logger.error("record_send failed: %s", exc)
        conn.rollback()
        raise


def is_duplicate_send(conn: sqlite3.Connection, professor_id: int) -> bool:
    """Return True if any successful send exists for this professor."""
    try:
        row: Optional[sqlite3.Row] = conn.execute(
            "SELECT 1 FROM send_log WHERE professor_id = ? AND status = 'success' LIMIT 1",
            (professor_id,),
        ).fetchone()
        return row is not None
    except sqlite3.Error as exc:
        logger.error("is_duplicate_send check failed for professor_id=%d: %s", professor_id, exc)
        raise


# ---------------------------------------------------------------------------
# Suppression list
# ---------------------------------------------------------------------------

def add_suppression(conn: sqlite3.Connection, email: str, reason: str) -> None:
    """Add an email to the suppression list (idempotent)."""
    try:
        conn.execute(
            """
            INSERT INTO suppression_list (email, reason, added_at)
            VALUES (?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET reason = excluded.reason
            """,
            (email, reason, _now_iso()),
        )
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("add_suppression failed for %s: %s", email, exc)
        conn.rollback()
        raise


def is_suppressed(conn: sqlite3.Connection, email: str) -> bool:
    """Return True if the email is on the suppression list."""
    try:
        row: Optional[sqlite3.Row] = conn.execute(
            "SELECT 1 FROM suppression_list WHERE email = ? LIMIT 1", (email,)
        ).fetchone()
        return row is not None
    except sqlite3.Error as exc:
        logger.error("is_suppressed check failed for %s: %s", email, exc)
        raise


def get_suppression_list(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every suppression entry as a plain dict."""
    try:
        rows: list[sqlite3.Row] = conn.execute(
            "SELECT email, reason, added_at FROM suppression_list ORDER BY added_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error as exc:
        logger.error("get_suppression_list failed: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

def create_session(
    conn: sqlite3.Connection,
    sender_profile_id: int,
    notes: Optional[str] = None,
) -> int:
    """Create a new session and return its id."""
    try:
        cursor: sqlite3.Cursor = conn.execute(
            "INSERT INTO sessions (sender_profile_id, created_at, notes) VALUES (?, ?, ?)",
            (sender_profile_id, _now_iso(), notes),
        )
        conn.commit()
        return cursor.lastrowid or 0
    except sqlite3.Error as exc:
        logger.error("create_session failed: %s", exc)
        conn.rollback()
        raise


def get_session(conn: sqlite3.Connection, session_id: int) -> Optional[Session]:
    """Fetch a session by id."""
    try:
        row: Optional[sqlite3.Row] = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return Session.from_row(row) if row else None
    except sqlite3.Error as exc:
        logger.error("get_session failed for id=%d: %s", session_id, exc)
        raise


# ---------------------------------------------------------------------------
# SenderProfile
# ---------------------------------------------------------------------------

def insert_sender_profile(conn: sqlite3.Connection, profile: SenderProfile) -> int:
    """Insert a sender profile and return its id."""
    try:
        cursor: sqlite3.Cursor = conn.execute(
            """
            INSERT INTO sender_profiles (
                name, school, grade, email, interests, background,
                graduation_year, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile.name, profile.school, profile.grade, profile.email,
                profile.interests, profile.background,
                profile.graduation_year, profile.created_at,
            ),
        )
        conn.commit()
        return cursor.lastrowid or 0
    except sqlite3.Error as exc:
        logger.error("insert_sender_profile failed: %s", exc)
        conn.rollback()
        raise


def get_sender_profiles(conn: sqlite3.Connection) -> list[SenderProfile]:
    """Return all sender profiles."""
    try:
        rows: list[sqlite3.Row] = conn.execute(
            "SELECT * FROM sender_profiles ORDER BY id"
        ).fetchall()
        return [SenderProfile.from_row(r) for r in rows]
    except sqlite3.Error as exc:
        logger.error("get_sender_profiles failed: %s", exc)
        raise


def get_sender_profile(conn: sqlite3.Connection, id: int) -> Optional[SenderProfile]:
    """Fetch a single sender profile by id."""
    try:
        row: Optional[sqlite3.Row] = conn.execute(
            "SELECT * FROM sender_profiles WHERE id = ?", (id,)
        ).fetchone()
        return SenderProfile.from_row(row) if row else None
    except sqlite3.Error as exc:
        logger.error("get_sender_profile failed for id=%d: %s", id, exc)
        raise


# ---------------------------------------------------------------------------
# FollowUp
# ---------------------------------------------------------------------------

def insert_followup(conn: sqlite3.Connection, followup: FollowUp) -> int:
    """Insert a follow-up and return its id."""
    try:
        cursor: sqlite3.Cursor = conn.execute(
            """
            INSERT INTO followups (
                original_draft_id, professor_id, sender_profile_id,
                body, subject, status, scheduled_date, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                followup.original_draft_id, followup.professor_id,
                followup.sender_profile_id, followup.body, followup.subject,
                followup.status, followup.scheduled_date, followup.created_at,
            ),
        )
        conn.commit()
        return cursor.lastrowid or 0
    except sqlite3.Error as exc:
        logger.error("insert_followup failed: %s", exc)
        conn.rollback()
        raise


def get_followups(
    conn: sqlite3.Connection,
    status: Optional[str] = None,
) -> list[FollowUp]:
    """Return follow-ups, optionally filtered by status."""
    try:
        if status is not None:
            rows: list[sqlite3.Row] = conn.execute(
                "SELECT * FROM followups WHERE status = ? ORDER BY id", (status,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM followups ORDER BY id"
            ).fetchall()
        return [FollowUp.from_row(r) for r in rows]
    except sqlite3.Error as exc:
        logger.error("get_followups failed: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def log_audit(conn: sqlite3.Connection, entry: AuditEntry) -> int:
    """Insert an audit-log entry and return its id."""
    try:
        cursor: sqlite3.Cursor = conn.execute(
            """
            INSERT INTO audit_log (
                timestamp, action, actor_profile_id,
                entity_type, entity_id, details
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                entry.timestamp, entry.action, entry.actor_profile_id,
                entry.entity_type, entry.entity_id, entry.details,
            ),
        )
        conn.commit()
        return cursor.lastrowid or 0
    except sqlite3.Error as exc:
        logger.error("log_audit failed: %s", exc)
        conn.rollback()
        raise


def get_audit_log(
    conn: sqlite3.Connection,
    entity_type: Optional[str] = None,
    limit: int = 100,
) -> list[AuditEntry]:
    """Return recent audit entries, optionally filtered by entity_type."""
    try:
        if entity_type is not None:
            rows: list[sqlite3.Row] = conn.execute(
                "SELECT * FROM audit_log WHERE entity_type = ? ORDER BY id DESC LIMIT ?",
                (entity_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [AuditEntry.from_row(r) for r in rows]
    except sqlite3.Error as exc:
        logger.error("get_audit_log failed: %s", exc)
        raise


# ---------------------------------------------------------------------------
# App Settings (key-value store for runtime configuration)
# ---------------------------------------------------------------------------

def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    """Get a single setting value by key, returning *default* if not found."""
    try:
        row: Optional[sqlite3.Row] = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default
    except sqlite3.Error as exc:
        logger.error("get_setting failed for key=%s: %s", key, exc)
        return default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert a single setting key-value pair."""
    try:
        conn.execute(
            """
            INSERT INTO app_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("set_setting failed for key=%s: %s", key, exc)
        conn.rollback()
        raise


def get_all_settings(conn: sqlite3.Connection) -> dict[str, str]:
    """Return all settings as a dict."""
    try:
        rows: list[sqlite3.Row] = conn.execute(
            "SELECT key, value FROM app_settings ORDER BY key"
        ).fetchall()
        return {r["key"]: r["value"] for r in rows}
    except sqlite3.Error as exc:
        logger.error("get_all_settings failed: %s", exc)
        return {}


def set_settings_bulk(conn: sqlite3.Connection, settings: dict[str, str]) -> None:
    """Upsert multiple settings at once."""
    try:
        for key, value in settings.items():
            conn.execute(
                """
                INSERT INTO app_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("set_settings_bulk failed: %s", exc)
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Access Keys
# ---------------------------------------------------------------------------

def create_access_key(conn: sqlite3.Connection, key_value: str, label: str, role: str = "user", created_by: str = "admin") -> int:
    """Create a new access key and return its id."""
    try:
        cursor: sqlite3.Cursor = conn.execute(
            """
            INSERT INTO access_keys (key_value, label, role, is_active, created_by)
            VALUES (?, ?, ?, 1, ?)
            """,
            (key_value, label, role, created_by),
        )
        conn.commit()
        return cursor.lastrowid or 0
    except sqlite3.Error as exc:
        logger.error("create_access_key failed: %s", exc)
        conn.rollback()
        raise


def validate_access_key(conn: sqlite3.Connection, key_value: str) -> Optional[dict[str, Any]]:
    """Validate an access key. Returns key row dict if valid, None otherwise."""
    try:
        row: Optional[sqlite3.Row] = conn.execute(
            "SELECT * FROM access_keys WHERE key_value = ? AND is_active = 1",
            (key_value,),
        ).fetchone()
        if row:
            # Update last_used
            conn.execute(
                "UPDATE access_keys SET last_used = ? WHERE id = ?",
                (_now_iso(), row["id"]),
            )
            conn.commit()
            return dict(row)
        return None
    except sqlite3.Error as exc:
        logger.error("validate_access_key failed: %s", exc)
        return None


def get_access_keys(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all access keys."""
    try:
        rows: list[sqlite3.Row] = conn.execute(
            "SELECT * FROM access_keys ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error as exc:
        logger.error("get_access_keys failed: %s", exc)
        return []


def revoke_access_key(conn: sqlite3.Connection, key_id: int) -> None:
    """Deactivate an access key."""
    try:
        conn.execute(
            "UPDATE access_keys SET is_active = 0 WHERE id = ?",
            (key_id,),
        )
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("revoke_access_key failed for id=%d: %s", key_id, exc)
        conn.rollback()
        raise


def delete_access_key(conn: sqlite3.Connection, key_id: int) -> None:
    """Delete an access key permanently."""
    try:
        conn.execute("DELETE FROM access_keys WHERE id = ?", (key_id,))
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("delete_access_key failed for id=%d: %s", key_id, exc)
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Admin Activity Log
# ---------------------------------------------------------------------------

def log_admin_activity(
    conn: sqlite3.Connection,
    *,
    actor_key_id: Optional[int] = None,
    actor_label: str = "",
    actor_role: str = "",
    action: str,
    category: str = "general",
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_method: Optional[str] = None,
    request_path: Optional[str] = None,
    session_id: Optional[str] = None,
    response_code: Optional[int] = None,
) -> int:
    """Insert a detailed admin activity log entry. Returns the row id."""
    try:
        cursor: sqlite3.Cursor = conn.execute(
            """
            INSERT INTO admin_activity_log
                (actor_key_id, actor_label, actor_role, action, category,
                 target_type, target_id, details, ip_address, user_agent,
                 request_method, request_path, session_id, response_code)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                actor_key_id, actor_label, actor_role, action, category,
                target_type, target_id,
                json.dumps(details) if details else "{}",
                ip_address, user_agent,
                request_method, request_path, session_id, response_code,
            ),
        )
        conn.commit()
        return cursor.lastrowid or 0
    except sqlite3.Error as exc:
        logger.error("log_admin_activity failed: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
        return 0


def get_admin_activity_log(
    conn: sqlite3.Connection,
    category: Optional[str] = None,
    actor_label: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return recent admin activity log entries with optional filters."""
    try:
        conditions: list[str] = []
        params: list[Any] = []

        if category:
            conditions.append("category = ?")
            params.append(category)
        if actor_label:
            conditions.append("actor_label = ?")
            params.append(actor_label)
        if action:
            conditions.append("action = ?")
            params.append(action)

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)

        rows: list[sqlite3.Row] = conn.execute(
            f"SELECT * FROM admin_activity_log{where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error as exc:
        logger.error("get_admin_activity_log failed: %s", exc)
        return []


def get_admin_activity_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return aggregate stats for the admin activity log."""
    try:
        total = conn.execute("SELECT COUNT(*) as cnt FROM admin_activity_log").fetchone()
        today = conn.execute(
            "SELECT COUNT(*) as cnt FROM admin_activity_log WHERE timestamp >= date('now')"
        ).fetchone()
        unique_actors = conn.execute(
            "SELECT COUNT(DISTINCT actor_label) as cnt FROM admin_activity_log WHERE actor_label != ''"
        ).fetchone()
        unique_ips = conn.execute(
            "SELECT COUNT(DISTINCT ip_address) as cnt FROM admin_activity_log WHERE ip_address IS NOT NULL"
        ).fetchone()
        by_category = conn.execute(
            "SELECT category, COUNT(*) as cnt FROM admin_activity_log GROUP BY category ORDER BY cnt DESC"
        ).fetchall()
        by_action = conn.execute(
            "SELECT action, COUNT(*) as cnt FROM admin_activity_log GROUP BY action ORDER BY cnt DESC LIMIT 15"
        ).fetchall()

        return {
            "total_events": total["cnt"] if total else 0,
            "today_events": today["cnt"] if today else 0,
            "unique_actors": unique_actors["cnt"] if unique_actors else 0,
            "unique_ips": unique_ips["cnt"] if unique_ips else 0,
            "by_category": {r["category"]: r["cnt"] for r in by_category},
            "by_action": {r["action"]: r["cnt"] for r in by_action},
        }
    except sqlite3.Error as exc:
        logger.error("get_admin_activity_stats failed: %s", exc)
        return {"total_events": 0, "today_events": 0, "unique_actors": 0, "unique_ips": 0, "by_category": {}, "by_action": {}}
