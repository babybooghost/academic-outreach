"""SQLite database layer for the Academic Outreach Email System.

Every public function in this module accepts an explicit connection so callers
control transaction scope.  Use :func:`get_connection` to obtain a connection
and :func:`init_db` once at startup to ensure tables exist.

Supports both local SQLite and Turso (cloud SQLite via libsql) connections.
Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN env vars to use Turso.

Multi-tenancy
-------------
All per-user tables carry a ``workspace_id`` column.  Rather than thread that id
through every call site, it is *bound to the connection*: :func:`get_connection`
records the active ``workspace_id`` on the returned object and the per-user
helpers below read it via :func:`_ws` and scope their SQL accordingly.  Global
tables (``access_keys``, ``admin_activity_log``, ``user_signups``, ...) are not
scoped.  ``workspace_id = 0`` is the implicit CLI / single-user workspace.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
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
# Turso support (pure-Python HTTP adapter)
# ---------------------------------------------------------------------------
# We talk to Turso over its HTTP "pipeline" API instead of the native
# ``libsql_experimental`` driver. The native driver is a Rust extension with
# patchy wheel coverage that fails to install on many serverless builders;
# the HTTP path needs no native dependency and is a drop-in for the small
# sqlite3 surface this app uses (``execute`` + ``fetchone/fetchall`` +
# ``lastrowid`` + ``commit``). Statements autocommit per call.

import base64 as _base64
import http.client as _http_client
import json as _json
import urllib.request as _urllib_request
from urllib.parse import urlparse as _urlparse

_TURSO_URL: str = os.environ.get("TURSO_DATABASE_URL", "")
_TURSO_TOKEN: str = os.environ.get("TURSO_AUTH_TOKEN", "")


def _is_turso_configured() -> bool:
    """Check if Turso credentials are available."""
    return bool(_TURSO_URL and _TURSO_TOKEN)


def _turso_http_endpoint() -> str:
    """Convert a libsql:// database URL into its https pipeline endpoint."""
    url = _TURSO_URL.strip()
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    elif url.startswith("wss://"):
        url = "https://" + url[len("wss://"):]
    elif url.startswith("ws://"):
        url = "http://" + url[len("ws://"):]
    return url.rstrip("/") + "/v2/pipeline"


class _TursoRow:
    """sqlite3.Row-like: supports row["col"], row[idx], dict(row)."""

    __slots__ = ("_cols", "_vals")

    def __init__(self, cols: list[str], vals: list[Any]) -> None:
        self._cols = cols
        self._vals = vals

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self._vals[key]
        return self._vals[self._cols.index(key)]

    def keys(self) -> list[str]:
        return list(self._cols)

    def __iter__(self):
        return iter(self._vals)

    def __len__(self) -> int:
        return len(self._vals)


class _TursoCursor:
    def __init__(self, rows: list[_TursoRow], lastrowid: Optional[int], rowcount: int) -> None:
        self._rows = rows
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    def fetchone(self) -> Optional[_TursoRow]:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[_TursoRow]:
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


def _to_arg(value: Any) -> dict[str, Any]:
    """Encode a Python value as a Turso pipeline argument."""
    if value is None:
        return {"type": "null", "value": None}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, (bytes, bytearray)):
        return {"type": "blob", "base64": _base64.b64encode(bytes(value)).decode()}
    return {"type": "text", "value": str(value)}


def _from_cell(cell: dict[str, Any]) -> Any:
    """Decode a Turso pipeline result cell into a Python value."""
    t = cell.get("type")
    v = cell.get("value")
    if t == "null":
        return None
    if t == "integer":
        return int(v)
    if t == "float":
        return float(v)
    if t == "blob":
        return _base64.b64decode(cell.get("base64", ""))
    return v


class _TursoHTTPConnection:
    """Minimal sqlite3-compatible connection backed by Turso's HTTP API."""

    def __init__(self) -> None:
        self._endpoint = _turso_http_endpoint()
        parsed = _urlparse(self._endpoint)
        self._host = parsed.hostname or ""
        self._port = parsed.port or 443
        self._path = parsed.path or "/v2/pipeline"
        # One persistent HTTPS socket reused across every execute() on this
        # connection. Turso queries are individual round trips, so without
        # keep-alive each one pays a fresh TCP + TLS handshake; a page that
        # runs several queries would handshake several times. We open the
        # socket lazily and transparently reconnect if it goes stale.
        self._http: Optional[_http_client.HTTPSConnection] = None
        self.row_factory = None  # accepted for compatibility; rows are Row-like

    def _http_conn(self) -> _http_client.HTTPSConnection:
        if self._http is None:
            self._http = _http_client.HTTPSConnection(
                self._host, self._port, timeout=30
            )
        return self._http

    def _drop_http(self) -> None:
        if self._http is not None:
            try:
                self._http.close()
            except Exception:
                pass
            self._http = None

    def _pipeline(self, statements: list[dict[str, Any]]) -> list[dict[str, Any]]:
        requests_list = [{"type": "execute", "stmt": s} for s in statements]
        requests_list.append({"type": "close"})
        payload = _json.dumps({"requests": requests_list}).encode()
        headers = {
            "Authorization": f"Bearer {_TURSO_TOKEN}",
            "Content-Type": "application/json",
        }
        # Try the live socket; on a stale/closed keep-alive connection retry
        # once with a fresh one before giving up.
        last_exc: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                conn = self._http_conn()
                conn.request("POST", self._path, body=payload, headers=headers)
                resp = conn.getresponse()
                body = resp.read()
                if resp.status >= 400:
                    raise sqlite3.OperationalError(
                        f"Turso HTTP {resp.status}: {body[:300].decode('utf-8', 'replace')}"
                    )
                data = _json.loads(body)
                break
            except sqlite3.OperationalError:
                raise
            except (OSError, _http_client.HTTPException) as exc:
                last_exc = exc
                self._drop_http()  # stale socket — reconnect on the next pass
        else:
            raise sqlite3.OperationalError(f"Turso request failed: {last_exc}")
        results = data.get("results", [])
        for item in results:
            if item.get("type") == "error":
                msg = item.get("error", {}).get("message", "Turso error")
                raise sqlite3.OperationalError(msg)
        return results

    def execute(self, sql: str, params: Any = ()) -> _TursoCursor:
        stmt: dict[str, Any] = {"sql": sql}
        if params:
            stmt["args"] = [_to_arg(p) for p in params]
        results = self._pipeline([stmt])
        result = results[0]["response"]["result"]
        cols = [c.get("name") for c in result.get("cols", [])]
        rows = [_TursoRow(cols, [_from_cell(c) for c in r]) for r in result.get("rows", [])]
        last = result.get("last_insert_rowid")
        lastrowid = int(last) if last not in (None, "") else None
        return _TursoCursor(rows, lastrowid, int(result.get("affected_row_count", 0) or 0))

    def executescript_batch(self, statements: list[str]) -> None:
        """Run many statements in a single round trip (used for schema setup)."""
        self._pipeline([{"sql": s} for s in statements if s and s.strip()])

    def commit(self) -> None:
        # Each execute autocommits over the pipeline API; nothing to flush.
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        self._drop_http()


def _get_turso_connection() -> Any:
    """Return an HTTP-backed Turso connection (sqlite3-compatible subset)."""
    try:
        return _TursoHTTPConnection()
    except Exception as exc:
        logger.error("Failed to connect to Turso: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Workspace binding
# ---------------------------------------------------------------------------

# The implicit workspace used by the CLI and any single-user / global context.
DEFAULT_WORKSPACE_ID: int = 0


class _BoundConnection:
    """Thin proxy around a real DB connection that carries the active workspace.

    All attribute/method access (``execute``, ``commit``, ``close``, ``row_factory``)
    transparently proxies to the wrapped connection, so existing call sites keep
    working.  The bound ``workspace_id`` lets per-user helpers scope their SQL
    without every caller passing it explicitly.
    """

    __slots__ = ("_raw", "workspace_id")

    def __init__(self, raw: Any, workspace_id: int) -> None:
        object.__setattr__(self, "_raw", raw)
        object.__setattr__(self, "workspace_id", int(workspace_id))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "workspace_id":
            object.__setattr__(self, name, int(value))
        else:
            setattr(self._raw, name, value)


def _ws(conn: Any) -> int:
    """Return the workspace_id bound to *conn* (defaults to the CLI workspace)."""
    return int(getattr(conn, "workspace_id", DEFAULT_WORKSPACE_ID) or DEFAULT_WORKSPACE_ID)


# ---------------------------------------------------------------------------
# Connection / bootstrap
# ---------------------------------------------------------------------------

def get_connection(db_path: str, workspace_id: int = DEFAULT_WORKSPACE_ID) -> Any:
    """Return a connection with ``row_factory`` set and a bound ``workspace_id``.

    Uses Turso if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN are set, otherwise a
    local SQLite file at *db_path*.  Per-user helpers in this module read the
    bound ``workspace_id`` to isolate tenants.  The caller closes the connection.
    """
    if _is_turso_configured():
        raw = _get_turso_connection()
        return _BoundConnection(raw, workspace_id)

    try:
        raw = sqlite3.connect(db_path)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys = ON")
        return _BoundConnection(raw, workspace_id)
    except sqlite3.Error as exc:
        logger.error("Failed to connect to database at %s: %s", db_path, exc)
        raise


# Per-user tables that gained a workspace_id column for tenant isolation.
_WORKSPACE_TABLES: tuple[str, ...] = (
    "sender_profiles", "professors", "sessions", "drafts", "send_log",
    "suppression_list", "followups", "audit_log",
)


def _column_names(conn: Any, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except Exception:
        return set()
    names: set[str] = set()
    for row in rows:
        try:
            names.add(row["name"])
        except Exception:
            names.add(row[1])
    return names


def _migrate_schema(conn: Any) -> None:
    """Bring an existing database up to the multi-tenant schema (idempotent).

    Adds the ``workspace_id`` column to per-user tables that predate it and
    rebuilds ``app_settings`` with a composite ``(workspace_id, key)`` key.
    Fresh databases already match the schema, so every step is a no-op there.
    """
    for table in _WORKSPACE_TABLES:
        cols = _column_names(conn, table)
        if cols and "workspace_id" not in cols:
            try:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN workspace_id INTEGER NOT NULL DEFAULT 0"
                )
            except Exception as exc:
                logger.warning("Could not add workspace_id to %s: %s", table, exc)

    # Newer sender-profile detail columns (awards/skills/goal/age) on databases
    # created before they existed.
    sp_cols = _column_names(conn, "sender_profiles")
    if sp_cols:
        for col in ("awards", "skills", "goal", "age"):
            if col not in sp_cols:
                try:
                    conn.execute(
                        f"ALTER TABLE sender_profiles ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                    )
                except Exception as exc:
                    logger.warning("Could not add %s to sender_profiles: %s", col, exc)

    # Reply-tracking columns on drafts (databases created before they existed).
    draft_cols = _column_names(conn, "drafts")
    if draft_cols:
        if "outcome" not in draft_cols:
            try:
                conn.execute("ALTER TABLE drafts ADD COLUMN outcome TEXT NOT NULL DEFAULT ''")
            except Exception as exc:
                logger.warning("Could not add outcome to drafts: %s", exc)
        if "replied_at" not in draft_cols:
            try:
                conn.execute("ALTER TABLE drafts ADD COLUMN replied_at TEXT")
            except Exception as exc:
                logger.warning("Could not add replied_at to drafts: %s", exc)

    # app_settings needs a composite primary key; rebuild if it still has the
    # legacy single-column (key) primary key.
    settings_cols = _column_names(conn, "app_settings")
    if settings_cols and "workspace_id" not in settings_cols:
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS app_settings_new (
                    workspace_id INTEGER NOT NULL DEFAULT 0,
                    key   TEXT NOT NULL,
                    value TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (workspace_id, key)
                )"""
            )
            conn.execute(
                "INSERT OR IGNORE INTO app_settings_new (workspace_id, key, value) "
                "SELECT 0, key, value FROM app_settings"
            )
            conn.execute("DROP TABLE app_settings")
            conn.execute("ALTER TABLE app_settings_new RENAME TO app_settings")
        except Exception as exc:
            logger.warning("Could not migrate app_settings: %s", exc)

    # Composite uniqueness for tenant-scoped natural keys.
    for stmt in (
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_professors_ws_email ON professors(workspace_id, email)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_suppression_ws_email ON suppression_list(workspace_id, email)",
    ):
        try:
            conn.execute(stmt)
        except Exception as exc:
            logger.warning("Could not create index (%s): %s", stmt, exc)
    conn.commit()


def init_db(db_path: str) -> None:
    """Create all tables (idempotent), migrate, and enable WAL journal mode."""
    conn = get_connection(db_path)
    try:
        statements = [s.strip() for s in _SCHEMA_SQL.strip().split(";") if s.strip()]
        if _is_turso_configured():
            # One round trip for all CREATE TABLE statements (fast cold start).
            conn.executescript_batch(statements)
        else:
            conn.execute("PRAGMA journal_mode = WAL")
            for statement in statements:
                conn.execute(statement)
        conn.commit()
        _migrate_schema(conn)
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
    workspace_id    INTEGER NOT NULL DEFAULT 0,
    name            TEXT    NOT NULL,
    school          TEXT    NOT NULL,
    grade           TEXT    NOT NULL,
    email           TEXT    NOT NULL,
    interests       TEXT    NOT NULL DEFAULT '',
    background      TEXT    NOT NULL DEFAULT '',
    graduation_year TEXT,
    awards          TEXT    NOT NULL DEFAULT '',
    skills          TEXT    NOT NULL DEFAULT '',
    goal            TEXT    NOT NULL DEFAULT '',
    age             TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS professors (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id     INTEGER NOT NULL DEFAULT 0,
    name             TEXT NOT NULL,
    title            TEXT,
    email            TEXT NOT NULL,
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
    workspace_id      INTEGER NOT NULL DEFAULT 0,
    sender_profile_id INTEGER NOT NULL REFERENCES sender_profiles(id),
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS drafts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id        INTEGER NOT NULL DEFAULT 0,
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
    reviewer_notes      TEXT,
    outcome             TEXT    NOT NULL DEFAULT '',
    replied_at          TEXT
);

CREATE TABLE IF NOT EXISTS send_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id   INTEGER NOT NULL DEFAULT 0,
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
    workspace_id INTEGER NOT NULL DEFAULT 0,
    email    TEXT    NOT NULL,
    reason   TEXT    NOT NULL DEFAULT '',
    added_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS followups (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id      INTEGER NOT NULL DEFAULT 0,
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
    workspace_id     INTEGER NOT NULL DEFAULT 0,
    timestamp        TEXT    NOT NULL DEFAULT (datetime('now')),
    action           TEXT    NOT NULL,
    actor_profile_id INTEGER,
    entity_type      TEXT    NOT NULL,
    entity_id        INTEGER,
    details          TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS app_settings (
    workspace_id INTEGER NOT NULL DEFAULT 0,
    key   TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (workspace_id, key)
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

CREATE TABLE IF NOT EXISTS email_verifications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email          TEXT    NOT NULL,
    code_hash      TEXT    NOT NULL,
    display_name   TEXT    NOT NULL DEFAULT '',
    password_hash  TEXT    NOT NULL DEFAULT '',
    key_value      TEXT    NOT NULL DEFAULT '',
    purpose        TEXT    NOT NULL DEFAULT 'signup',
    attempts       INTEGER NOT NULL DEFAULT 0,
    expires_at     TEXT    NOT NULL,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS request_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL DEFAULT (datetime('now')),
    method       TEXT    NOT NULL DEFAULT '',
    path         TEXT    NOT NULL DEFAULT '',
    status       INTEGER NOT NULL DEFAULT 0,
    duration_ms  INTEGER NOT NULL DEFAULT 0,
    workspace_id INTEGER,
    actor_label  TEXT    NOT NULL DEFAULT '',
    role         TEXT    NOT NULL DEFAULT '',
    ip           TEXT    NOT NULL DEFAULT '',
    user_agent   TEXT    NOT NULL DEFAULT '',
    referrer     TEXT    NOT NULL DEFAULT '',
    query        TEXT    NOT NULL DEFAULT '',
    body         TEXT    NOT NULL DEFAULT '',
    error        TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_request_log_id ON request_log(id);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.utcnow().isoformat()


# ---------------------------------------------------------------------------
# Email verification codes (signup)
# ---------------------------------------------------------------------------

def create_email_verification(
    conn: Any,
    *,
    email: str,
    code_hash: str,
    display_name: str,
    password_hash: str,
    key_value: str,
    expires_at: str,
    purpose: str = "signup",
) -> int:
    """Replace any pending verification for *email* and store a fresh one."""
    try:
        conn.execute("DELETE FROM email_verifications WHERE email = ?", (email,))
        cursor = conn.execute(
            """INSERT INTO email_verifications
               (email, code_hash, display_name, password_hash, key_value,
                purpose, attempts, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            (email, code_hash, display_name, password_hash, key_value,
             purpose, expires_at, _now_iso()),
        )
        conn.commit()
        return cursor.lastrowid or 0
    except sqlite3.Error as exc:
        logger.error("create_email_verification failed: %s", exc)
        conn.rollback()
        raise


def get_email_verification(conn: Any, email: str) -> Optional[dict[str, Any]]:
    """Return the pending verification row for *email*, or None."""
    try:
        row = conn.execute(
            "SELECT * FROM email_verifications WHERE email = ? ORDER BY id DESC LIMIT 1",
            (email,),
        ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error as exc:
        logger.error("get_email_verification failed: %s", exc)
        raise


def bump_verification_attempts(conn: Any, verification_id: int) -> None:
    try:
        conn.execute(
            "UPDATE email_verifications SET attempts = attempts + 1 WHERE id = ?",
            (verification_id,),
        )
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("bump_verification_attempts failed: %s", exc)


def delete_email_verification(conn: Any, email: str) -> None:
    try:
        conn.execute("DELETE FROM email_verifications WHERE email = ?", (email,))
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("delete_email_verification failed: %s", exc)


_REQUEST_LOG_COLS = (
    "method", "path", "status", "duration_ms", "workspace_id", "actor_label",
    "role", "ip", "user_agent", "referrer", "query", "body", "error",
)


def insert_request_log(conn: Any, **fields: Any) -> int:
    """Insert one detailed request-log row. Best-effort; caller guards errors."""
    cols = ("ts", *_REQUEST_LOG_COLS)
    values = [_now_iso()] + [fields.get(c) for c in _REQUEST_LOG_COLS]
    placeholders = ",".join("?" for _ in cols)
    cursor = conn.execute(
        f"INSERT INTO request_log ({','.join(cols)}) VALUES ({placeholders})",
        values,
    )
    conn.commit()
    return cursor.lastrowid or 0


def get_request_logs(
    conn: Any,
    *,
    method: Optional[str] = None,
    status_class: Optional[int] = None,
    path_like: Optional[str] = None,
    actor: Optional[str] = None,
    errors_only: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return detailed request logs (newest first) with optional filters."""
    clauses: list[str] = []
    params: list[Any] = []
    if method:
        clauses.append("method = ?")
        params.append(method)
    if status_class:
        clauses.append("status >= ? AND status < ?")
        params.extend([int(status_class), int(status_class) + 100])
    if path_like:
        clauses.append("path LIKE ?")
        params.append(f"%{path_like}%")
    if actor:
        clauses.append("actor_label = ?")
        params.append(actor)
    if errors_only:
        clauses.append("(status >= 400 OR error != '')")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM request_log{where} ORDER BY id DESC LIMIT ?",
        [*params, int(limit)],
    ).fetchall()
    return [dict(r) for r in rows]


def prune_request_logs(conn: Any, keep_days: int = 14) -> None:
    """Delete request-log rows older than *keep_days* to bound table growth."""
    cutoff = (datetime.utcnow() - timedelta(days=max(1, keep_days))).isoformat()
    conn.execute("DELETE FROM request_log WHERE ts < ?", (cutoff,))
    conn.commit()


def find_workspace_by_owner_email(conn: Any, email: str) -> Optional[dict[str, Any]]:
    """Return the active access-key row whose workspace is owned by *email*.

    Matches case-insensitively (emails aren't case-sensitive), and falls back to
    the ``user_signups`` table so a returning user is recognised even if the
    ``workspace_owner_email`` setting was never written. This is what keeps a
    Google sign-in mapping back to the same workspace instead of looking "new".
    """
    em = (email or "").strip().lower()
    if not em:
        return None
    try:
        row = conn.execute(
            "SELECT workspace_id FROM app_settings "
            "WHERE key = 'workspace_owner_email' AND LOWER(value) = ? LIMIT 1",
            (em,),
        ).fetchone()
        if row:
            key_row = conn.execute(
                "SELECT * FROM access_keys WHERE id = ? AND is_active = 1",
                (row["workspace_id"],),
            ).fetchone()
            if key_row:
                return dict(key_row)

        # Fallback: map via the signup record's access key.
        srow = conn.execute(
            "SELECT key_value FROM user_signups WHERE LOWER(email) = ? LIMIT 1",
            (em,),
        ).fetchone()
        if srow:
            key_row = conn.execute(
                "SELECT * FROM access_keys WHERE key_value = ? AND is_active = 1",
                (srow["key_value"],),
            ).fetchone()
            if key_row:
                return dict(key_row)
        return None
    except sqlite3.Error as exc:
        logger.error("find_workspace_by_owner_email failed: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Professor CRUD
# ---------------------------------------------------------------------------

def upsert_professor(conn: Any, prof: Professor) -> int:
    """Insert or update a professor by (workspace, email).  Returns the row id."""
    wid = _ws(conn)
    try:
        existing = conn.execute(
            "SELECT id FROM professors WHERE workspace_id = ? AND email = ?",
            (wid, prof.email),
        ).fetchone()
        if existing:
            row_id = existing["id"]
            conn.execute(
                """
                UPDATE professors SET
                    name = ?, title = ?, university = ?, department = ?,
                    lab_name = ?, field = ?, profile_url = ?,
                    research_summary = ?, recent_work = ?, notes = ?,
                    enrichment_text = ?, keywords = ?, summary = ?,
                    talking_points = ?, status = ?, updated_at = ?
                WHERE id = ? AND workspace_id = ?
                """,
                (
                    prof.name, prof.title, prof.university, prof.department,
                    prof.lab_name, prof.field, prof.profile_url,
                    prof.research_summary, prof.recent_work, prof.notes,
                    prof.enrichment_text, prof.keywords, prof.summary,
                    prof.talking_points, prof.status, _now_iso(),
                    row_id, wid,
                ),
            )
            conn.commit()
            return row_id

        cursor = conn.execute(
            """
            INSERT INTO professors (
                workspace_id, name, title, email, university, department,
                lab_name, field, profile_url, research_summary, recent_work,
                notes, enrichment_text, keywords, summary, talking_points,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                wid, prof.name, prof.title, prof.email, prof.university,
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


def get_professor(conn: Any, id: int) -> Optional[Professor]:
    """Fetch a single professor by id within the active workspace."""
    try:
        row: Optional[sqlite3.Row] = conn.execute(
            "SELECT * FROM professors WHERE id = ? AND workspace_id = ?",
            (id, _ws(conn)),
        ).fetchone()
        return Professor.from_row(row) if row else None
    except sqlite3.Error as exc:
        logger.error("get_professor failed for id=%d: %s", id, exc)
        raise


def get_professors(
    conn: Any,
    status: Optional[str] = None,
    field: Optional[str] = None,
) -> list[Professor]:
    """Return professors in the active workspace, optionally filtered."""
    try:
        clauses: list[str] = ["workspace_id = ?"]
        params: list[Any] = [_ws(conn)]
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if field is not None:
            clauses.append("field = ?")
            params.append(field)

        query: str = "SELECT * FROM professors WHERE " + " AND ".join(clauses) + " ORDER BY id"
        rows: list[sqlite3.Row] = conn.execute(query, params).fetchall()
        return [Professor.from_row(r) for r in rows]
    except sqlite3.Error as exc:
        logger.error("get_professors failed: %s", exc)
        raise


def get_professors_by_ids(conn: Any, ids: Any) -> dict[int, Professor]:
    """Fetch many professors by id in a single query, keyed by id.

    Replaces per-row ``get_professor`` loops (each of which is a network round
    trip on Turso) with one ``IN (...)`` lookup. Returns only ids that exist in
    the active workspace.
    """
    id_list = [int(i) for i in {i for i in ids if i is not None}]
    if not id_list:
        return {}
    try:
        placeholders = ",".join("?" for _ in id_list)
        rows = conn.execute(
            f"SELECT * FROM professors WHERE workspace_id = ? AND id IN ({placeholders})",
            [_ws(conn), *id_list],
        ).fetchall()
        result: dict[int, Professor] = {}
        for r in rows:
            prof = Professor.from_row(r)
            if prof and prof.id is not None:
                result[prof.id] = prof
        return result
    except sqlite3.Error as exc:
        logger.error("get_professors_by_ids failed: %s", exc)
        raise


def update_professor(conn: Any, prof: Professor) -> None:
    """Full update of a professor row (must have id set) within the workspace."""
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
            WHERE id = ? AND workspace_id = ?
            """,
            (
                prof.name, prof.title, prof.email, prof.university,
                prof.department, prof.lab_name, prof.field, prof.profile_url,
                prof.research_summary, prof.recent_work, prof.notes,
                prof.enrichment_text, prof.keywords, prof.summary,
                prof.talking_points, prof.status, _now_iso(), prof.id, _ws(conn),
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

def insert_draft(conn: Any, draft: Draft) -> int:
    """Insert a new draft into the active workspace and return its id."""
    try:
        cursor = conn.execute(
            """
            INSERT INTO drafts (
                workspace_id, professor_id, sender_profile_id, session_id,
                subject_lines, body, template_variant,
                specificity_score, authenticity_score, relevance_score,
                conciseness_score, completeness_score, overall_score,
                similarity_score, warnings, status, created_at,
                reviewed_at, reviewer_notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _ws(conn), draft.professor_id, draft.sender_profile_id, draft.session_id,
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


def get_draft(conn: Any, id: int) -> Optional[Draft]:
    """Fetch a single draft by id within the active workspace."""
    try:
        row: Optional[sqlite3.Row] = conn.execute(
            "SELECT * FROM drafts WHERE id = ? AND workspace_id = ?",
            (id, _ws(conn)),
        ).fetchone()
        return Draft.from_row(row) if row else None
    except sqlite3.Error as exc:
        logger.error("get_draft failed for id=%d: %s", id, exc)
        raise


def get_drafts(
    conn: Any,
    session_id: Optional[int] = None,
    status: Optional[str] = None,
) -> list[Draft]:
    """Return drafts in the active workspace, optionally filtered."""
    try:
        clauses: list[str] = ["workspace_id = ?"]
        params: list[Any] = [_ws(conn)]
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)

        query: str = "SELECT * FROM drafts WHERE " + " AND ".join(clauses) + " ORDER BY id"
        rows: list[sqlite3.Row] = conn.execute(query, params).fetchall()
        return [Draft.from_row(r) for r in rows]
    except sqlite3.Error as exc:
        logger.error("get_drafts failed: %s", exc)
        raise


def update_draft(conn: Any, draft: Draft) -> None:
    """Full update of a draft row (must have id set) within the workspace."""
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
            WHERE id = ? AND workspace_id = ?
            """,
            (
                draft.professor_id, draft.sender_profile_id, draft.session_id,
                draft.subject_lines, draft.body, draft.template_variant,
                draft.specificity_score, draft.authenticity_score,
                draft.relevance_score, draft.conciseness_score,
                draft.completeness_score, draft.overall_score,
                draft.similarity_score, draft.warnings, draft.status,
                draft.reviewed_at, draft.reviewer_notes, draft.id, _ws(conn),
            ),
        )
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("update_draft failed for id=%s: %s", draft.id, exc)
        conn.rollback()
        raise


def update_draft_status(
    conn: Any,
    draft_id: int,
    status: str,
    notes: Optional[str] = None,
) -> None:
    """Lightweight status-only update for a draft within the workspace."""
    try:
        conn.execute(
            """
            UPDATE drafts
               SET status = ?, reviewed_at = ?, reviewer_notes = ?
             WHERE id = ? AND workspace_id = ?
            """,
            (status, _now_iso(), notes, draft_id, _ws(conn)),
        )
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("update_draft_status failed for id=%d: %s", draft_id, exc)
        conn.rollback()
        raise


VALID_OUTCOMES: tuple[str, ...] = ("", "replied", "meeting", "declined")


def set_draft_outcome(conn: Any, draft_id: int, outcome: str) -> None:
    """Record a reply outcome for a draft (workspace-scoped).

    A non-empty outcome means the professor responded — which excludes them from
    follow-up nudges. Empty outcome clears it (back to 'awaiting reply').
    """
    outcome = outcome if outcome in VALID_OUTCOMES else ""
    replied_at = _now_iso() if outcome else None
    try:
        conn.execute(
            "UPDATE drafts SET outcome = ?, replied_at = ? WHERE id = ? AND workspace_id = ?",
            (outcome, replied_at, draft_id, _ws(conn)),
        )
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("set_draft_outcome failed for id=%d: %s", draft_id, exc)
        conn.rollback()
        raise


def get_outreach_stats(conn: Any) -> dict[str, int]:
    """Funnel metrics for the active workspace: sent, replied, meetings, rate."""
    wid = _ws(conn)

    def _count(where: str) -> int:
        try:
            return int(conn.execute(
                f"SELECT COUNT(*) AS c FROM drafts WHERE workspace_id = ? AND {where}",
                (wid,),
            ).fetchone()["c"])
        except sqlite3.Error:
            return 0

    sent = _count("status = 'sent'")
    replied = _count("outcome != ''")
    meetings = _count("outcome = 'meeting'")
    return {
        "sent": sent,
        "replied": replied,
        "meetings": meetings,
        "reply_rate": round(100 * replied / sent) if sent else 0,
    }


def get_quality_outcome_matrix(conn: Any, threshold: float = 7.0) -> dict[str, Any]:
    """Confusion matrix: did the AI quality score predict real replies?

    Among SENT drafts, cross predicted quality (overall_score >= threshold =
    "high") with the actual outcome (any reply vs none). Lets the user see
    whether high-scored emails actually land replies.
    """
    wid = _ws(conn)

    def _count(where: str, *params: Any) -> int:
        try:
            return int(conn.execute(
                f"SELECT COUNT(*) AS c FROM drafts WHERE workspace_id = ? AND status = 'sent' AND {where}",
                (wid, *params),
            ).fetchone()["c"])
        except sqlite3.Error:
            return 0

    hi_rep = _count("overall_score >= ? AND outcome != ''", threshold)
    hi_no = _count("overall_score >= ? AND outcome = ''", threshold)
    lo_rep = _count("overall_score < ? AND outcome != ''", threshold)
    lo_no = _count("overall_score < ? AND outcome = ''", threshold)
    return {
        "threshold": threshold,
        "high_replied": hi_rep, "high_noreply": hi_no,
        "low_replied": lo_rep, "low_noreply": lo_no,
        "high_rate": round(100 * hi_rep / (hi_rep + hi_no)) if (hi_rep + hi_no) else 0,
        "low_rate": round(100 * lo_rep / (lo_rep + lo_no)) if (lo_rep + lo_no) else 0,
        "total": hi_rep + hi_no + lo_rep + lo_no,
    }


# ---------------------------------------------------------------------------
# SendRecord
# ---------------------------------------------------------------------------

def record_send(conn: Any, record: SendRecord) -> int:
    """Insert a send-log entry into the active workspace and return its id."""
    try:
        cursor = conn.execute(
            """
            INSERT INTO send_log (
                workspace_id, draft_id, professor_id, sent_at, method,
                gmail_draft_id, status, error_message, message_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _ws(conn), record.draft_id, record.professor_id, record.sent_at,
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


def is_duplicate_send(conn: Any, professor_id: int) -> bool:
    """Return True if any successful send exists for this professor in the workspace."""
    try:
        row: Optional[sqlite3.Row] = conn.execute(
            "SELECT 1 FROM send_log WHERE professor_id = ? AND status = 'success' "
            "AND workspace_id = ? LIMIT 1",
            (professor_id, _ws(conn)),
        ).fetchone()
        return row is not None
    except sqlite3.Error as exc:
        logger.error("is_duplicate_send check failed for professor_id=%d: %s", professor_id, exc)
        raise


# ---------------------------------------------------------------------------
# Suppression list
# ---------------------------------------------------------------------------

def add_suppression(conn: Any, email: str, reason: str) -> None:
    """Add an email to the workspace suppression list (idempotent)."""
    wid = _ws(conn)
    try:
        existing = conn.execute(
            "SELECT 1 FROM suppression_list WHERE workspace_id = ? AND email = ? LIMIT 1",
            (wid, email),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE suppression_list SET reason = ? WHERE workspace_id = ? AND email = ?",
                (reason, wid, email),
            )
        else:
            conn.execute(
                "INSERT INTO suppression_list (workspace_id, email, reason, added_at) "
                "VALUES (?, ?, ?, ?)",
                (wid, email, reason, _now_iso()),
            )
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("add_suppression failed for %s: %s", email, exc)
        conn.rollback()
        raise


def is_suppressed(conn: Any, email: str) -> bool:
    """Return True if the email is on the workspace suppression list."""
    try:
        row: Optional[sqlite3.Row] = conn.execute(
            "SELECT 1 FROM suppression_list WHERE email = ? AND workspace_id = ? LIMIT 1",
            (email, _ws(conn)),
        ).fetchone()
        return row is not None
    except sqlite3.Error as exc:
        logger.error("is_suppressed check failed for %s: %s", email, exc)
        raise


def get_suppression_list(conn: Any) -> list[dict[str, Any]]:
    """Return every suppression entry in the workspace as a plain dict."""
    try:
        rows: list[sqlite3.Row] = conn.execute(
            "SELECT email, reason, added_at FROM suppression_list "
            "WHERE workspace_id = ? ORDER BY added_at DESC",
            (_ws(conn),),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error as exc:
        logger.error("get_suppression_list failed: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

def create_session(
    conn: Any,
    sender_profile_id: int,
    notes: Optional[str] = None,
) -> int:
    """Create a new session in the active workspace and return its id."""
    try:
        cursor = conn.execute(
            "INSERT INTO sessions (workspace_id, sender_profile_id, created_at, notes) "
            "VALUES (?, ?, ?, ?)",
            (_ws(conn), sender_profile_id, _now_iso(), notes),
        )
        conn.commit()
        return cursor.lastrowid or 0
    except sqlite3.Error as exc:
        logger.error("create_session failed: %s", exc)
        conn.rollback()
        raise


def get_session(conn: Any, session_id: int) -> Optional[Session]:
    """Fetch a session by id within the active workspace."""
    try:
        row: Optional[sqlite3.Row] = conn.execute(
            "SELECT * FROM sessions WHERE id = ? AND workspace_id = ?",
            (session_id, _ws(conn)),
        ).fetchone()
        return Session.from_row(row) if row else None
    except sqlite3.Error as exc:
        logger.error("get_session failed for id=%d: %s", session_id, exc)
        raise


# ---------------------------------------------------------------------------
# SenderProfile
# ---------------------------------------------------------------------------

def insert_sender_profile(conn: Any, profile: SenderProfile) -> int:
    """Insert a sender profile into the active workspace and return its id."""
    try:
        cursor = conn.execute(
            """
            INSERT INTO sender_profiles (
                workspace_id, name, school, grade, email, interests, background,
                graduation_year, awards, skills, goal, age, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _ws(conn), profile.name, profile.school, profile.grade, profile.email,
                profile.interests, profile.background, profile.graduation_year,
                profile.awards, profile.skills, profile.goal, profile.age,
                profile.created_at,
            ),
        )
        conn.commit()
        return cursor.lastrowid or 0
    except sqlite3.Error as exc:
        logger.error("insert_sender_profile failed: %s", exc)
        conn.rollback()
        raise


def get_sender_profiles(conn: Any) -> list[SenderProfile]:
    """Return all sender profiles in the active workspace."""
    try:
        rows: list[sqlite3.Row] = conn.execute(
            "SELECT * FROM sender_profiles WHERE workspace_id = ? ORDER BY id",
            (_ws(conn),),
        ).fetchall()
        return [SenderProfile.from_row(r) for r in rows]
    except sqlite3.Error as exc:
        logger.error("get_sender_profiles failed: %s", exc)
        raise


def get_sender_profile(conn: Any, id: int) -> Optional[SenderProfile]:
    """Fetch a single sender profile by id within the active workspace."""
    try:
        row: Optional[sqlite3.Row] = conn.execute(
            "SELECT * FROM sender_profiles WHERE id = ? AND workspace_id = ?",
            (id, _ws(conn)),
        ).fetchone()
        return SenderProfile.from_row(row) if row else None
    except sqlite3.Error as exc:
        logger.error("get_sender_profile failed for id=%d: %s", id, exc)
        raise


# ---------------------------------------------------------------------------
# FollowUp
# ---------------------------------------------------------------------------

def insert_followup(conn: Any, followup: FollowUp) -> int:
    """Insert a follow-up into the active workspace and return its id."""
    try:
        cursor = conn.execute(
            """
            INSERT INTO followups (
                workspace_id, original_draft_id, professor_id, sender_profile_id,
                body, subject, status, scheduled_date, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _ws(conn), followup.original_draft_id, followup.professor_id,
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
    conn: Any,
    status: Optional[str] = None,
) -> list[FollowUp]:
    """Return follow-ups in the active workspace, optionally filtered by status."""
    try:
        if status is not None:
            rows: list[sqlite3.Row] = conn.execute(
                "SELECT * FROM followups WHERE status = ? AND workspace_id = ? ORDER BY id",
                (status, _ws(conn)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM followups WHERE workspace_id = ? ORDER BY id",
                (_ws(conn),),
            ).fetchall()
        return [FollowUp.from_row(r) for r in rows]
    except sqlite3.Error as exc:
        logger.error("get_followups failed: %s", exc)
        raise


def update_followup_status(conn: Any, followup_id: int, status: str) -> None:
    """Set a follow-up's status (e.g. 'sent', 'failed') within the workspace."""
    try:
        conn.execute(
            "UPDATE followups SET status = ? WHERE id = ? AND workspace_id = ?",
            (status, followup_id, _ws(conn)),
        )
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("update_followup_status failed for id=%s: %s", followup_id, exc)
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def log_audit(conn: Any, entry: AuditEntry) -> int:
    """Insert an audit-log entry into the active workspace and return its id."""
    try:
        cursor = conn.execute(
            """
            INSERT INTO audit_log (
                workspace_id, timestamp, action, actor_profile_id,
                entity_type, entity_id, details
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _ws(conn), entry.timestamp, entry.action, entry.actor_profile_id,
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
    conn: Any,
    entity_type: Optional[str] = None,
    limit: int = 100,
) -> list[AuditEntry]:
    """Return recent audit entries in the workspace, optionally filtered."""
    try:
        if entity_type is not None:
            rows: list[sqlite3.Row] = conn.execute(
                "SELECT * FROM audit_log WHERE entity_type = ? AND workspace_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (entity_type, _ws(conn), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE workspace_id = ? ORDER BY id DESC LIMIT ?",
                (_ws(conn), limit),
            ).fetchall()
        return [AuditEntry.from_row(r) for r in rows]
    except sqlite3.Error as exc:
        logger.error("get_audit_log failed: %s", exc)
        raise


# ---------------------------------------------------------------------------
# App Settings (key-value store for runtime configuration)
# ---------------------------------------------------------------------------

def _upsert_setting(conn: Any, wid: int, key: str, value: str) -> None:
    """Insert-or-update one (workspace, key) setting.

    ``app_settings`` only holds (workspace_id, key, value) with a composite
    primary key, so INSERT OR REPLACE is a safe, dialect-portable upsert.
    """
    conn.execute(
        "INSERT OR REPLACE INTO app_settings (workspace_id, key, value) VALUES (?, ?, ?)",
        (wid, key, value),
    )


def get_setting(conn: Any, key: str, default: str = "") -> str:
    """Get a single workspace setting value, returning *default* if not found."""
    try:
        row: Optional[sqlite3.Row] = conn.execute(
            "SELECT value FROM app_settings WHERE key = ? AND workspace_id = ?",
            (key, _ws(conn)),
        ).fetchone()
        return row["value"] if row else default
    except sqlite3.Error as exc:
        logger.error("get_setting failed for key=%s: %s", key, exc)
        return default


def set_setting(conn: Any, key: str, value: str) -> None:
    """Upsert a single setting key-value pair for the active workspace."""
    try:
        _upsert_setting(conn, _ws(conn), key, value)
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("set_setting failed for key=%s: %s", key, exc)
        conn.rollback()
        raise


def get_all_settings(conn: Any) -> dict[str, str]:
    """Return all settings for the active workspace as a dict."""
    try:
        rows: list[sqlite3.Row] = conn.execute(
            "SELECT key, value FROM app_settings WHERE workspace_id = ? ORDER BY key",
            (_ws(conn),),
        ).fetchall()
        return {r["key"]: r["value"] for r in rows}
    except sqlite3.Error as exc:
        logger.error("get_all_settings failed: %s", exc)
        return {}


def set_settings_bulk(conn: Any, settings: dict[str, str]) -> None:
    """Upsert multiple settings at once for the active workspace."""
    wid = _ws(conn)
    try:
        for key, value in settings.items():
            _upsert_setting(conn, wid, key, value)
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


# ---------------------------------------------------------------------------
# Bug reports (support inbox)
# ---------------------------------------------------------------------------

VALID_BUG_STATUSES: tuple[str, ...] = ("open", "resolved")


def get_bug_reports(conn: Any, status: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
    """Return submitted bug reports (newest first), optionally filtered by status.

    Pass an unscoped connection — bug reports are global support data keyed by
    the reporter's access key, not per-workspace.
    """
    try:
        query = "SELECT * FROM bug_reports"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        rows = conn.execute(query, params).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]
    except sqlite3.Error as exc:
        logger.warning("get_bug_reports failed: %s", exc)
        return []


def get_bug_report_stats(conn: Any) -> dict[str, int]:
    """Counts of bug reports by status (open / resolved / total)."""
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM bug_reports GROUP BY status"
        ).fetchall()
        by = {r["status"]: r["c"] for r in rows}
        return {"open": by.get("open", 0), "resolved": by.get("resolved", 0),
                "total": sum(by.values())}
    except sqlite3.Error:
        return {"open": 0, "resolved": 0, "total": 0}


def set_bug_report_status(conn: Any, report_id: int, status: str) -> None:
    """Move a bug report between open/resolved."""
    if status not in VALID_BUG_STATUSES:
        raise ValueError(f"invalid bug status: {status!r}")
    conn.execute("UPDATE bug_reports SET status = ? WHERE id = ?", (status, int(report_id)))
    conn.commit()


# ---------------------------------------------------------------------------
# Full-database backup (restorable snapshot)
# ---------------------------------------------------------------------------

# Content + config tables worth backing up. Sensitive columns are redacted.
_BACKUP_TABLES: tuple[str, ...] = (
    "access_keys", "sender_profiles", "professors", "sessions", "drafts",
    "send_log", "suppression_list", "followups", "app_settings", "user_signups",
    "bug_reports",
)
_BACKUP_REDACT: dict[str, set[str]] = {
    "access_keys": {"key_value"},                      # the login credential
    "user_signups": {"password_hash", "key_value"},    # signup secrets
}


def dump_database(conn: Any) -> dict[str, Any]:
    """Serialize the backup-worthy tables to a JSON-able dict.

    Pass an *unscoped* connection (no ``workspace_id``) for a full, all-workspace
    snapshot. Password hashes are redacted so the dump is safe to download and
    store. The result round-trips through ``json.dumps(..., default=str)``.
    """
    data: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    for table in _BACKUP_TABLES:
        try:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        except sqlite3.Error as exc:
            logger.warning("dump_database: skipping %s (%s)", table, exc)
            continue
        redact = _BACKUP_REDACT.get(table, set())
        serialized: list[dict[str, Any]] = []
        for r in rows:
            d = {k: r[k] for k in r.keys()}
            for col in redact:
                if col in d:
                    d[col] = "***redacted***"
            serialized.append(d)
        data[table] = serialized
        counts[table] = len(serialized)
    return {
        "meta": {
            "generated_at": datetime.utcnow().isoformat(),
            "tables": list(data.keys()),
            "row_counts": counts,
        },
        "data": data,
    }
