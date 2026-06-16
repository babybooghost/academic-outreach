"""Flask web UI for the Academic Outreach Email System.

Provides a review interface with access-key authentication,
admin hub, and dark-themed dashboard.
"""

from __future__ import annotations

import base64
import glob
import hashlib
import json
import os
import secrets
import time
import traceback
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import requests

from flask import (
    Flask,
    Response,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from app.config import load_config
from app.database import (
    bump_verification_attempts,
    create_access_key,
    create_email_verification,
    delete_access_key,
    delete_email_verification,
    find_workspace_by_owner_email,
    get_access_keys,
    get_admin_activity_log,
    get_admin_activity_stats,
    get_all_settings,
    get_connection,
    get_draft,
    get_drafts,
    get_outreach_stats,
    get_quality_outcome_matrix,
    get_email_verification,
    get_professor,
    get_professors,
    get_professors_by_ids,
    get_request_logs,
    get_sender_profiles,
    get_setting,
    get_suppression_list,
    init_db,
    insert_request_log,
    insert_sender_profile,
    log_admin_activity,
    prune_request_logs,
    revoke_access_key,
    set_settings_bulk,
    set_draft_outcome,
    update_draft_status,
    upsert_professor,
    validate_access_key,
)
from app.logger import get_logger
from app.models import Draft, Professor, SenderProfile
from app.generation_service import run_generation_pipeline
from app.delivery import (
    SEND_METHODS,
    auto_send_preferences,
    delivery_setup_diagnostics,
    parse_bool,
    persist_auto_send_result,
    run_auto_followup_for_workspaces,
    run_auto_send_for_workspaces,
    send_ready_queue,
    send_mailbox_test,
    send_verification_email,
    seed_person_workspace_identity,
    system_mailbox_ready,
    workspace_config as build_workspace_config,
)

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

logger = get_logger(__name__)

_APP_VERSION = "1.0.0"

# Legacy hardcoded key that must never be used to sign sessions in production.
_LEGACY_DEV_SECRET = "outreach-local-dev-key"


def _resolve_secret_key() -> str:
    """Resolve the Flask session-signing secret.

    Requires FLASK_SECRET_KEY in any hosted (serverless) deployment so sessions
    cannot be forged with the old hardcoded key and stay valid across deploys.
    Falls back to a stable, clearly-marked dev key only for local development.
    """
    key = os.environ.get("FLASK_SECRET_KEY", "").strip()
    if key:
        return key
    if os.environ.get("VERCEL"):
        # No stable secret configured on a hosted instance. Generate an ephemeral
        # one so cookies are at least unforgeable, and warn loudly — sessions will
        # not survive a redeploy/cold start until FLASK_SECRET_KEY is set.
        logger.error(
            "FLASK_SECRET_KEY is not set on a hosted deployment. Using an ephemeral "
            "key; users will be logged out on every cold start until you set it."
        )
        return secrets.token_hex(32)
    logger.warning(
        "FLASK_SECRET_KEY not set; using the local development key. Do not use this "
        "in a hosted deployment."
    )
    return _LEGACY_DEV_SECRET


def create_app() -> Flask:
    """Create and configure the Flask application."""
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).resolve().parent / "templates"),
        static_folder=str(Path(__file__).resolve().parent / "static"),
    )
    app.secret_key = _resolve_secret_key()

    # Session-cookie hardening. SameSite=Lax is the primary CSRF defense: the
    # browser withholds the session cookie on cross-site POSTs, so forged
    # requests from other origins arrive unauthenticated. Secure is enabled on
    # hosted (HTTPS) deployments; local http dev keeps it off so login works.
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL")),
    )

    # Load config once and store on app
    try:
        cfg = load_config()
    except Exception:
        cfg = None
    app.config["APP_CFG"] = cfg
    app.config["APP_VERSION"] = _APP_VERSION

    # Admin password from env (for initial admin creation)
    app.config["ADMIN_PASSWORD"] = os.environ.get("ADMIN_PASSWORD", "")

    # Persistence guard: on a serverless host the local filesystem is ephemeral
    # (Vercel wipes /tmp between deploys/invocations), so without Turso every
    # redeploy would silently lose all data. Fail loudly rather than quietly.
    _is_serverless = bool(
        os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    )
    _has_turso = bool(
        os.environ.get("TURSO_DATABASE_URL", "").strip()
        and os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    )
    if _is_serverless and not _has_turso:
        logger.critical(
            "EPHEMERAL STORAGE: running on a serverless host without TURSO_DATABASE_URL/"
            "TURSO_AUTH_TOKEN. Data will be LOST on every redeploy. Set the Turso env vars."
        )

    # Ensure database exists
    if cfg:
        init_db(cfg.db_path)
        # Auto-create default admin key if none exist
        _ensure_default_admin_key(cfg.db_path)

    # Jinja filter: render stored ISO timestamps as a clean, readable date/time
    # instead of leaking raw "2026-06-13T14:32:11.123456" strings into the UI.
    def _pretty_dt(value: Any, with_time: bool = True) -> str:
        if not value:
            return "—"
        text = str(value)
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text[:16].replace("T", " ")
        return dt.strftime("%b %-d, %Y %H:%M" if with_time else "%b %-d, %Y")

    app.jinja_env.filters["prettydt"] = _pretty_dt
    app.jinja_env.filters["prettydate"] = lambda v: _pretty_dt(v, with_time=False)

    # Static cache-busting: append ?v=<hash-of-static-mtimes> to every
    # url_for('static', ...). The hash changes whenever a CSS/JS file changes
    # (each deploy), so browsers/CDN fetch fresh assets instead of serving a
    # stale style.css — the reason redeploys "looked like nothing changed".
    def _compute_static_version() -> str:
        try:
            files = sorted(
                glob.glob(os.path.join(app.static_folder or "", "*.css"))
                + glob.glob(os.path.join(app.static_folder or "", "*.js"))
            )
            stamp = "|".join(str(os.path.getmtime(f)) for f in files)
            return hashlib.md5(stamp.encode()).hexdigest()[:8] if stamp else _APP_VERSION
        except Exception:
            return _APP_VERSION

    _static_version = _compute_static_version()

    @app.url_defaults
    def _static_cache_bust(endpoint: str, values: dict) -> None:
        if endpoint == "static" and values is not None and "filename" in values:
            values.setdefault("v", _static_version)

    # Context processor
    @app.context_processor
    def inject_globals() -> dict[str, Any]:
        return {
            "app_version": _APP_VERSION,
            "now": datetime.utcnow(),
            "storage_status": _storage_status(),
            "google_enabled": _google_enabled(),
        }

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------
    def _auth_conn():
        c = app.config.get("APP_CFG")
        if c is None:
            raise RuntimeError("Application config not loaded")
        return get_connection(c.db_path)

    def _workspace_id(key_id: Optional[int] = None) -> int:
        workspace_key_id = key_id or session.get("key_id")
        if not workspace_key_id:
            raise RuntimeError("No workspace is associated with this session")
        return int(workspace_key_id)

    def _seed_workspace_identity(
        key_id: int,
        *,
        email: str,
        display_name: str,
    ) -> None:
        cfg = app.config.get("APP_CFG")
        if cfg is None:
            raise RuntimeError("Application config not loaded")
        workspace_conn = get_connection(cfg.db_path, workspace_id=key_id)
        try:
            seed_person_workspace_identity(
                workspace_conn,
                email=email,
                display_name=display_name,
                default_provider=cfg.email_provider,
            )
        finally:
            workspace_conn.close()

    def _workspace_conn():
        """Connection to the shared DB bound to the current user's workspace."""
        cfg = app.config.get("APP_CFG")
        if cfg is None:
            raise RuntimeError("Application config not loaded")
        return get_connection(cfg.db_path, workspace_id=_workspace_id())

    def _workspace_output_dir() -> Path:
        cfg = app.config.get("APP_CFG")
        if cfg is None:
            raise RuntimeError("Application config not loaded")
        output_dir = Path(cfg.output_dir) / f"workspace_{session.get('key_id')}"
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    def _workspace_config(conn) -> Any:
        base_cfg = app.config.get("APP_CFG")
        if base_cfg is None:
            raise RuntimeError("Application config not loaded")

        return build_workspace_config(base_cfg, conn)

    # --- Per-workspace daily spend caps (protect the OpenRouter budget) ---
    # Generous for a small trusted group; override via env if needed.
    _AI_CHAT_DAILY_LIMIT = int(os.environ.get("AI_CHAT_DAILY_LIMIT", "150"))
    _GENERATION_DAILY_LIMIT = int(os.environ.get("GENERATION_DAILY_LIMIT", "80"))

    def _utc_day_start() -> str:
        return datetime.utcnow().strftime("%Y-%m-%dT00:00:00")

    def _ai_chats_today() -> int:
        """Count AI assistant replies logged for this user since midnight UTC."""
        conn = _auth_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM chat_logs WHERE user_key_id = ? "
                "AND prompt_key = 'ai' AND created_at >= ?",
                (session.get("key_id"), _utc_day_start()),
            ).fetchone()
            return int(row["c"]) if row else 0
        except Exception:
            return 0
        finally:
            conn.close()

    def _drafts_today(conn) -> int:
        """Count drafts generated in this workspace since midnight UTC."""
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM drafts WHERE workspace_id = ? AND created_at >= ?",
                (conn.workspace_id, _utc_day_start()),
            ).fetchone()
            return int(row["c"]) if row else 0
        except Exception:
            return 0

    def _storage_status() -> dict[str, Any]:
        cfg = app.config.get("APP_CFG")
        db_path = cfg.db_path if cfg else ""
        has_turso = bool(
            os.environ.get("TURSO_DATABASE_URL", "").strip()
            and os.environ.get("TURSO_AUTH_TOKEN", "").strip()
        )
        if has_turso:
            return {
                "mode": "remote-shared",
                "persistent": True,
                "workspace_isolated": True,
                "severity": "info",
                "label": "Persistent remote database",
                "detail": (
                    "This deployment persists data in a shared Turso database. Each workspace is "
                    "isolated by a workspace_id on every per-user table, and data survives deploys, "
                    "cold starts, and instance replacement."
                ),
            }

        if os.environ.get("VERCEL") and str(db_path).startswith("/tmp"):
            return {
                "mode": "ephemeral-instance",
                "persistent": False,
                "workspace_isolated": True,
                "severity": "warning",
                "label": "Temporary hosted storage",
                "detail": (
                    "This deployment stores workspace data on the server instance filesystem. Per-user workspaces "
                    "are separated, but data can reset after cold starts, deploys, or instance replacement."
                ),
            }

        return {
            "mode": "local-files",
            "persistent": True,
            "workspace_isolated": True,
            "severity": "info",
            "label": "Local persistent workspace files",
            "detail": (
                "Workspace data is stored in per-user database files on this machine."
            ),
        }

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------
    def login_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get("authenticated"):
                return redirect(url_for("login"))
            return f(*args, **kwargs)
        return decorated

    # Ensure user_signups table exists (for upgrades from old DB)
    if cfg:
        try:
            conn = get_connection(cfg.db_path)
            conn.execute("""CREATE TABLE IF NOT EXISTS user_signups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                key_value TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )""")
            conn.commit()
            conn.close()
        except Exception:
            pass

    def admin_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get("admin_authenticated"):
                return redirect(url_for("admin_login"))
            return f(*args, **kwargs)
        return decorated

    # ------------------------------------------------------------------
    # Activity logging helper
    # ------------------------------------------------------------------
    def _log_activity(action: str, category: str = "general",
                      target_type: str = None, target_id: str = None,
                      details: dict = None, is_admin: bool = False):
        """Log an activity entry with full request context."""
        try:
            conn = _auth_conn()
            try:
                ip = request.headers.get("X-Forwarded-For", request.remote_addr)
                if ip and "," in ip:
                    ip = ip.split(",")[0].strip()
                log_admin_activity(
                    conn,
                    actor_key_id=session.get("admin_key_id") if is_admin else session.get("key_id"),
                    actor_label=session.get("admin_key_label", "") if is_admin else session.get("key_label", ""),
                    actor_role="admin" if is_admin else session.get("role", "user"),
                    action=action,
                    category=category,
                    target_type=target_type,
                    target_id=str(target_id) if target_id is not None else None,
                    details=details,
                    ip_address=ip,
                    user_agent=request.headers.get("User-Agent", "")[:500],
                    request_method=request.method,
                    request_path=request.path,
                    session_id=session.get("_id", session.sid if hasattr(session, "sid") else None),
                    response_code=None,
                )
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("Activity logging failed: %s", exc)

    # ------------------------------------------------------------------
    # Security headers
    # ------------------------------------------------------------------
    @app.after_request
    def _set_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response

    # ------------------------------------------------------------------
    # Detailed request logging (admin-visible, secrets redacted)
    # ------------------------------------------------------------------
    _REDACT_FIELDS = {
        "password", "password_confirm", "access_key", "code", "smtp_password",
        "api_key", "apikey", "llm_api_key", "token", "id_token", "refresh_token",
        "client_secret", "secret", "authorization", "csrf_token",
    }
    _LOG_SKIP_PREFIXES = ("/static/",)
    _LOG_SKIP_PATHS = {"/health", "/favicon.ico", "/admin/logs", "/admin/logs/api"}

    def _redact_map(data: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in (data or {}).items():
            if k.lower() in _REDACT_FIELDS:
                out[k] = "***"
            elif isinstance(v, dict):
                out[k] = _redact_map(v)  # recurse: catch nested secrets
            elif isinstance(v, list):
                out[k] = [_redact_map(i) if isinstance(i, dict) else i for i in v]
            else:
                out[k] = v
        return out

    @app.before_request
    def _log_request_start():
        g._req_start = time.monotonic()

    _request_log_enabled = os.environ.get("REQUEST_LOG_ENABLED", "1").strip() != "0"

    @app.after_request
    def _log_request(response):
        if not _request_log_enabled:
            return response
        try:
            path = request.path or ""
            if path in _LOG_SKIP_PATHS or any(path.startswith(p) for p in _LOG_SKIP_PREFIXES):
                return response
            start = getattr(g, "_req_start", None)
            duration_ms = int((time.monotonic() - start) * 1000) if start else 0
            ip = request.headers.get("X-Forwarded-For", request.remote_addr) or ""
            if "," in ip:
                ip = ip.split(",")[0].strip()

            query = _redact_map(request.args.to_dict(flat=True))
            body: dict[str, Any] = {}
            if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                if request.is_json:
                    parsed = request.get_json(silent=True)
                    if isinstance(parsed, dict):
                        body = _redact_map(parsed)
                else:
                    body = _redact_map(request.form.to_dict(flat=True))

            is_admin = bool(session.get("admin_authenticated"))
            if is_admin:
                actor_label = session.get("admin_key_label", "")
                role = "admin"
                wsid = session.get("admin_key_id")
            elif session.get("authenticated"):
                actor_label = session.get("key_label", "")
                role = session.get("role", "user")
                wsid = session.get("key_id")
            else:
                actor_label, role, wsid = "", "anon", None

            conn = _auth_conn()
            try:
                insert_request_log(
                    conn,
                    method=request.method, path=path, status=response.status_code,
                    duration_ms=duration_ms, workspace_id=wsid, actor_label=actor_label,
                    role=role, ip=ip,
                    user_agent=request.headers.get("User-Agent", "")[:300],
                    referrer=request.headers.get("Referer", "")[:300],
                    query=json.dumps(query)[:2000],
                    body=json.dumps(body)[:4000],
                    error=(getattr(g, "_log_error", "") or "")[:6000],
                )
                if secrets.randbelow(100) == 0:  # opportunistic retention prune
                    prune_request_logs(conn, keep_days=14)
            finally:
                conn.close()
        except Exception as exc:  # logging must never break a response
            logger.warning("Request logging failed: %s", exc)
        return response

    # ------------------------------------------------------------------
    # Error handler
    # ------------------------------------------------------------------
    @app.errorhandler(Exception)
    def handle_exception(e):
        # Preserve real HTTP errors (404/403/405/...) instead of masking them
        # all as 500 — keeps status codes (and the request log) accurate.
        if isinstance(e, HTTPException):
            return render_template("error.html", code=e.code, message=e.description), e.code
        import traceback
        tb = traceback.format_exc()
        g._log_error = tb  # surfaced in the detailed request log
        logger.error("Unhandled exception: %s\n%s", e, tb)
        # Never leak stack traces to users. Show the full traceback only when
        # explicitly debugging locally.
        if app.debug or app.config.get("SHOW_TRACEBACKS"):
            return (
                "<h1>Error</h1>"
                f"<pre style='white-space:pre-wrap; background:#111; color:#e44; padding:1rem;'>{tb}</pre>"
            ), 500
        return render_template("error.html", code=500), 500

    # ------------------------------------------------------------------
    # Homepage (public landing page)
    # ------------------------------------------------------------------
    @app.route("/")
    def homepage():
        # Logged-in users can view the homepage too (not trapped in the app).
        return render_template("homepage.html")

    # Friendly page URLs — old paths permanently redirect to the renamed ones so
    # existing bookmarks keep working. (Sub-routes like /finder/search are
    # unchanged; only the top-level page paths moved.)
    def _legacy_redirect(target_endpoint: str):
        return lambda: redirect(url_for(target_endpoint))

    for _old_path, _target in (
        ("/dashboard", "dashboard"), ("/finder", "finder_page"),
        ("/followups", "followups_page"), ("/professors", "professors_list"),
        ("/send", "send_page"), ("/settings", "settings_page"),
    ):
        # GET-only redirect; coexists with any POST handler on the same path.
        app.add_url_rule(_old_path, f"legacy_{_target}", _legacy_redirect(_target))

    # ------------------------------------------------------------------
    # Auth routes
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Google OAuth + signup helpers
    # ------------------------------------------------------------------
    def _google_enabled() -> bool:
        return bool(
            os.environ.get("GOOGLE_CLIENT_ID", "").strip()
            and os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
        )

    def _google_redirect_uri() -> str:
        override = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()
        if override:
            return override
        host = request.host
        scheme = "http" if host.startswith(("localhost", "127.0.0.1")) else "https"
        return f"{scheme}://{host}{url_for('google_callback')}"

    def _google_exchange(code: str) -> tuple[str, str, bool]:
        """Exchange an auth code for the user's (email, name, email_verified)."""
        client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
        token_resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", "").strip(),
                "redirect_uri": _google_redirect_uri(),
                "grant_type": "authorization_code",
            },
            timeout=20,
        )
        token_resp.raise_for_status()
        id_token = token_resp.json().get("id_token", "")
        parts = id_token.split(".")
        if len(parts) != 3:
            raise ValueError("malformed id_token")
        # The token came directly from Google's HTTPS token endpoint, so parsing
        # the payload (not user-supplied) is safe; we still check the audience.
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        if claims.get("aud") != client_id:
            raise ValueError("audience mismatch")
        email = (claims.get("email") or "").strip().lower()
        name = (claims.get("name") or (email.split("@", 1)[0] if email else "")).strip()
        verified = bool(claims.get("email_verified"))
        return email, name, verified

    def _complete_user_login(key_row: dict[str, Any]) -> None:
        """Set the session for a normal (non-admin) user workspace."""
        session["authenticated"] = True
        session["key_id"] = key_row["id"]
        session["key_label"] = key_row.get("label", "")
        session["role"] = key_row.get("role", "user")

    def _create_signup_account(
        conn: Any, email: str, display_name: str,
        *, password_hash: Optional[str] = None, key_value: Optional[str] = None,
    ) -> tuple[str, int]:
        """Create the user_signups row + access key + workspace identity."""
        email = (email or "").strip().lower()  # emails are case-insensitive
        if password_hash is None:  # Google users never set a password (login is by key)
            password_hash = generate_password_hash(
                "ao_" + secrets.token_hex(16), method="pbkdf2:sha256")
        if key_value is None:
            key_value = "ao_" + secrets.token_hex(24)
        conn.execute(
            """INSERT INTO user_signups
               (email, display_name, password_hash, key_value, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (email, display_name, password_hash, key_value, datetime.utcnow().isoformat()),
        )
        conn.commit()
        key_id = create_access_key(
            conn, key_value, display_name, "user", created_by=f"signup:{email}")
        _seed_workspace_identity(key_id, email=email, display_name=display_name)
        ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        if ip and "," in ip:
            ip = ip.split(",")[0].strip()
        log_admin_activity(
            conn, actor_key_id=None, actor_label=display_name, actor_role="user",
            action="user_signup", category="auth", target_type="access_key",
            target_id=None, details={"email": email, "display_name": display_name},
            ip_address=ip, user_agent=request.headers.get("User-Agent", "")[:500],
            request_method=request.method, request_path=request.path,
            session_id=None, response_code=None,
        )
        logger.info("New signup: %s (%s)", display_name, email)
        return key_value, key_id

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("authenticated"):
            return redirect(url_for("dashboard"))

        error = None
        if request.method == "POST":
            key_value = request.form.get("access_key", "").strip()
            if not key_value:
                error = "Please enter an access key."
            else:
                conn = _auth_conn()
                try:
                    key_data = validate_access_key(conn, key_value)
                    if key_data:
                        if key_data["role"] == "admin":
                            error = "Admin access keys must use the admin login."
                            _log_activity(
                                "user_login_denied_admin_key", category="auth",
                                details={"label": key_data["label"], "role": key_data["role"]},
                            )
                        else:
                            if str(key_data.get("created_by", "")).startswith("signup:"):
                                _seed_workspace_identity(
                                    key_data["id"],
                                    email=str(key_data.get("created_by", "")).split("signup:", 1)[1],
                                    display_name=key_data.get("label", ""),
                                )
                            session["authenticated"] = True
                            session["key_id"] = key_data["id"]
                            session["key_label"] = key_data["label"]
                            session["role"] = key_data["role"]
                            _log_activity(
                                "user_login", category="auth",
                                details={"label": key_data["label"], "role": key_data["role"]},
                            )
                            return redirect(url_for("dashboard"))
                    else:
                        error = "Invalid or revoked access key."
                        _log_activity(
                            "failed_login", category="auth",
                            details={"key_prefix": key_value[:8] + "..." if len(key_value) > 8 else "short"},
                        )
                finally:
                    conn.close()

        return render_template("login.html", error=error)

    @app.route("/signup", methods=["GET", "POST"])
    def signup():
        if session.get("authenticated"):
            return redirect(url_for("dashboard"))

        error = None
        access_key = None
        cfg = app.config.get("APP_CFG")
        pending_google = session.get("pending_google")

        # Optional invite gate so only people with the shared code can register
        # (signups consume the workspace owner's LLM API key).
        required_invite = os.environ.get("SIGNUP_INVITE_CODE", "").strip()

        if request.method == "POST":
            invite_code = request.form.get("invite_code", "").strip()
            display_name = request.form.get("display_name", "").strip()
            # Google-verified signups carry their email in the session, not the form.
            if pending_google:
                email = (pending_google.get("email") or "").strip()
            else:
                email = request.form.get("email", "").strip()
            password = request.form.get("password", "")
            password_confirm = request.form.get("password_confirm", "")

            if required_invite and not secrets.compare_digest(invite_code, required_invite):
                error = "That invite code is not valid. Ask the workspace owner for the current code."
                _log_activity("signup_invalid_invite", category="auth", details={"email": email[:80]})
            elif not email or not display_name:
                error = "Email and display name are required."
            elif not pending_google and len(password) < 6:
                error = "Password must be at least 6 characters."
            elif not pending_google and password != password_confirm:
                error = "Passwords do not match."
            else:
                conn = _auth_conn()
                try:
                    existing = conn.execute(
                        "SELECT id FROM user_signups WHERE email = ?", (email,)
                    ).fetchone()
                    if existing:
                        error = "This email is already registered. Use your existing key to log in."
                    elif pending_google:
                        # Google already proved the email — create the account and
                        # sign in immediately (no password, no email code needed).
                        _, key_id = _create_signup_account(conn, email, display_name)
                        session.pop("pending_google", None)
                        key_row = conn.execute(
                            "SELECT * FROM access_keys WHERE id = ?", (key_id,)
                        ).fetchone()
                        _complete_user_login(dict(key_row))
                        _log_activity("user_signup", category="auth",
                                      details={"email": email, "via": "google"})
                        return redirect(url_for("dashboard"))
                    else:
                        pw_hash = generate_password_hash(password, method="pbkdf2:sha256")
                        key_value = "ao_" + secrets.token_hex(24)
                        verify_on = parse_bool(
                            os.environ.get("SIGNUP_EMAIL_VERIFICATION"), default=False)
                        if cfg and verify_on and system_mailbox_ready(cfg):
                            # Email-verification phase: stash the pending signup and
                            # mail a 6-digit code; the account is created on /verify.
                            verify_code = f"{secrets.randbelow(900000) + 100000}"
                            create_email_verification(
                                conn, email=email,
                                code_hash=generate_password_hash(verify_code, method="pbkdf2:sha256"),
                                display_name=display_name, password_hash=pw_hash,
                                key_value=key_value,
                                expires_at=(datetime.utcnow() + timedelta(minutes=15)).isoformat(),
                            )
                            if send_verification_email(cfg, email, verify_code):
                                _log_activity("signup_code_sent", category="auth",
                                              details={"email": email[:80]})
                                return render_template(
                                    "signup.html", verify_email=email,
                                    invite_required=bool(required_invite),
                                )
                            delete_email_verification(conn, email)
                            error = (
                                "The system mailbox is configured but the send was rejected. "
                                "Check SMTP_PASSWORD is a Gmail App Password (not your account "
                                "password) and that SENDER_EMAIL matches SMTP_USER. The exact SMTP "
                                "error is in the server logs."
                            )
                        else:
                            # No system mailbox configured — create directly (fallback).
                            _create_signup_account(conn, email, display_name,
                                                   password_hash=pw_hash, key_value=key_value)
                            access_key = key_value
                except Exception as exc:
                    logger.exception("Signup failed")
                    error = f"Signup failed: {exc}"
                finally:
                    conn.close()

        return render_template(
            "signup.html", error=error, access_key=access_key,
            invite_required=bool(required_invite), pending_google=pending_google,
        )

    @app.route("/signup/verify", methods=["POST"])
    def signup_verify():
        if session.get("authenticated"):
            return redirect(url_for("dashboard"))
        required_invite = os.environ.get("SIGNUP_INVITE_CODE", "").strip()
        email = request.form.get("email", "").strip()
        code = request.form.get("code", "").strip()
        error = None
        access_key = None
        conn = _auth_conn()
        try:
            record = get_email_verification(conn, email)
            if not record:
                error = "No pending verification for that email. Start signup again."
            elif record["expires_at"] < datetime.utcnow().isoformat():
                delete_email_verification(conn, email)
                error = "That code expired. Start signup again."
            elif int(record["attempts"]) >= 5:
                delete_email_verification(conn, email)
                error = "Too many incorrect attempts. Start signup again."
            elif not code or not check_password_hash(record["code_hash"], code):
                bump_verification_attempts(conn, int(record["id"]))
                error = "Incorrect code. Check the email and try again."
            else:
                _create_signup_account(
                    conn, email, record["display_name"],
                    password_hash=record["password_hash"], key_value=record["key_value"],
                )
                delete_email_verification(conn, email)
                access_key = record["key_value"]
                _log_activity("signup_verified", category="auth", details={"email": email[:80]})
        except Exception as exc:
            logger.exception("Signup verification failed")
            error = f"Verification failed: {exc}"
        finally:
            conn.close()

        if access_key:
            return render_template("signup.html", access_key=access_key,
                                   invite_required=bool(required_invite))
        return render_template("signup.html", verify_email=email, error=error,
                               invite_required=bool(required_invite))

    @app.route("/auth/google/login")
    def google_login():
        if not _google_enabled():
            flash("Google sign-in is not configured.", "error")
            return redirect(url_for("login"))
        state = secrets.token_urlsafe(24)
        session["oauth_state"] = state
        params = {
            "client_id": os.environ.get("GOOGLE_CLIENT_ID", "").strip(),
            "redirect_uri": _google_redirect_uri(),
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "online",
            "prompt": "select_account",
        }
        return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))

    @app.route("/auth/google/callback")
    def google_callback():
        if not _google_enabled():
            return redirect(url_for("login"))
        if request.args.get("error"):
            flash("Google sign-in was cancelled.", "error")
            return redirect(url_for("login"))
        state = request.args.get("state", "")
        if not state or state != session.pop("oauth_state", None):
            flash("Sign-in session expired. Please try again.", "error")
            return redirect(url_for("login"))
        code = request.args.get("code", "")
        if not code:
            return redirect(url_for("login"))
        try:
            email, name, verified = _google_exchange(code)
        except Exception as exc:
            logger.warning("Google token exchange failed: %s", exc)
            flash("Could not complete Google sign-in. Please try again.", "error")
            return redirect(url_for("login"))
        if not email or not verified:
            flash("Your Google account email isn't verified.", "error")
            return redirect(url_for("login"))

        email = email.strip().lower()
        name = (name or "").strip() or email.split("@")[0]

        conn = _auth_conn()
        try:
            key_row = find_workspace_by_owner_email(conn, email)

            # Existing workspace → log in.
            if key_row:
                if key_row.get("role") == "admin":
                    flash("Admin accounts must use the admin login.", "error")
                    return redirect(url_for("login"))
                _complete_user_login(key_row)
                _log_activity("user_login", category="auth",
                              details={"label": key_row.get("label", ""),
                                       "email": email, "via": "google"})
                return redirect(url_for("dashboard"))

            # New Google identity → auto-provision a workspace. Google sign-in is
            # self-sufficient: no invite code, no access key needed.
            try:
                _, key_id = _create_signup_account(conn, email, name)
            except Exception as exc:
                # Almost certainly a races/duplicate email — recover by mapping.
                logger.warning("Google auto-provision fell back to lookup: %s", exc)
                key_row = find_workspace_by_owner_email(conn, email)
                if not key_row:
                    flash("Could not finish creating your workspace. Please try again.", "error")
                    return redirect(url_for("login"))
                _complete_user_login(key_row)
                _log_activity("user_login", category="auth",
                              details={"email": email, "via": "google"})
                return redirect(url_for("dashboard"))
        finally:
            conn.close()

        _complete_user_login({"id": key_id, "label": name, "role": "user"})
        _log_activity("user_login", category="auth",
                      details={"email": email, "name": name, "via": "google",
                               "new_workspace": True})
        flash("Welcome! Your workspace is ready.", "success")
        return redirect(url_for("dashboard"))

    @app.route("/logout")
    def logout():
        label = session.get("key_label", "")
        was_admin = session.get("admin_authenticated", False)
        if was_admin:
            _log_activity("admin_logout", category="auth", is_admin=True,
                          details={"label": session.get("admin_key_label", "")})
        elif session.get("authenticated"):
            _log_activity("user_logout", category="auth", details={"label": label})
        session.clear()
        return redirect(url_for("homepage"))

    # ------------------------------------------------------------------
    # Admin Auth (separate login)
    # ------------------------------------------------------------------
    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if session.get("admin_authenticated"):
            return redirect(url_for("admin_hub"))

        error = None
        if request.method == "POST":
            key_value = request.form.get("access_key", "").strip()
            if not key_value:
                error = "Please enter an admin access key."
            else:
                conn = _auth_conn()
                try:
                    key_data = validate_access_key(conn, key_value)
                    if key_data and key_data["role"] == "admin":
                        session["admin_authenticated"] = True
                        session["admin_key_id"] = key_data["id"]
                        session["admin_key_label"] = key_data["label"]
                        _log_activity(
                            "admin_login", category="auth", is_admin=True,
                            details={"label": key_data["label"]},
                        )
                        return redirect(url_for("admin_hub"))
                    elif key_data:
                        error = "This key does not have admin privileges."
                        _log_activity(
                            "admin_login_denied", category="auth",
                            details={"label": key_data["label"], "role": key_data["role"]},
                        )
                    else:
                        error = "Invalid or revoked access key."
                        _log_activity(
                            "admin_failed_login", category="auth",
                            details={"key_prefix": key_value[:8] + "..." if len(key_value) > 8 else "short"},
                        )
                finally:
                    conn.close()

        return render_template("admin_login.html", error=error)

    @app.route("/admin/logout")
    def admin_logout():
        _log_activity("admin_logout", category="auth", is_admin=True,
                      details={"label": session.get("admin_key_label", "")})
        session.pop("admin_authenticated", None)
        session.pop("admin_key_id", None)
        session.pop("admin_key_label", None)
        return redirect(url_for("admin_login"))

    # ------------------------------------------------------------------
    # Health check (no auth)
    # ------------------------------------------------------------------
    @app.route("/health")
    def health_check():
        cfg = app.config.get("APP_CFG")
        storage = _storage_status()
        return jsonify({
            "status": "ok",
            "config_loaded": cfg is not None,
            "db_path": cfg.db_path if cfg else None,
            "storage": storage,
        })

    def _cron_authorized() -> bool:
        cron_secret = os.environ.get("CRON_SECRET", "").strip()
        if cron_secret:
            return request.headers.get("Authorization", "") == f"Bearer {cron_secret}"
        return bool(app.config.get("TESTING") or session.get("admin_authenticated"))

    @app.route("/api/cron/auto-send", methods=["GET", "POST"])
    def auto_send_cron():
        if not _cron_authorized():
            return jsonify({
                "success": False,
                "error": "Unauthorized cron request.",
            }), 401

        cfg = app.config.get("APP_CFG")
        if cfg is None:
            return jsonify({
                "success": False,
                "error": "Application config not loaded.",
            }), 503

        workspace_id_raw = request.args.get("workspace_id")
        workspace_id = None
        if workspace_id_raw:
            try:
                workspace_id = int(workspace_id_raw)
            except ValueError:
                return jsonify({
                    "success": False,
                    "error": "workspace_id must be an integer.",
                }), 400

        dry_run = parse_bool(request.args.get("dry_run"), default=False)
        summary = run_auto_send_for_workspaces(
            cfg, workspace_id=workspace_id, dry_run=dry_run,
        )
        # Same daily run also generates and sends due 7-10 day follow-ups for
        # any workspace that opted in (separate toggle from first-contact send).
        followups = run_auto_followup_for_workspaces(
            cfg, workspace_id=workspace_id, dry_run=dry_run,
        )
        summary["followups"] = followups
        summary["success"] = summary.get("success", False) and followups.get("success", False)

        _log_activity(
            "auto_send_cron",
            category="send",
            details={
                "workspace_count": summary.get("workspace_count"),
                "processed": summary.get("processed"),
                "sent": summary.get("sent"),
                "failed": summary.get("failed"),
                "followups_sent": followups.get("sent"),
                "followups_generated": followups.get("generated"),
                "dry_run": summary.get("dry_run"),
            },
        )
        return jsonify(summary), 200 if summary.get("success") else 207

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------
    @app.route("/desk")
    @login_required
    def dashboard():
        workspace_conn = _workspace_conn()
        try:
            wid = workspace_conn.workspace_id
            prof_rows = workspace_conn.execute(
                "SELECT status, COUNT(*) as cnt FROM professors WHERE workspace_id = ? GROUP BY status",
                (wid,),
            ).fetchall()
            prof_counts: dict[str, int] = {r["status"]: r["cnt"] for r in prof_rows}
            total_professors = sum(prof_counts.values())

            draft_rows = workspace_conn.execute(
                "SELECT status, COUNT(*) as cnt FROM drafts WHERE workspace_id = ? GROUP BY status",
                (wid,),
            ).fetchall()
            draft_counts: dict[str, int] = {r["status"]: r["cnt"] for r in draft_rows}
            total_drafts = sum(draft_counts.values())

            recent_sends = workspace_conn.execute(
                """SELECT sl.*, p.name as professor_name
                   FROM send_log sl
                   JOIN professors p ON sl.professor_id = p.id
                   WHERE sl.workspace_id = ?
                   ORDER BY sl.sent_at DESC LIMIT 10""",
                (wid,),
            ).fetchall()

            outreach = get_outreach_stats(workspace_conn)
            matrix = get_quality_outcome_matrix(workspace_conn)

            return render_template(
                "dashboard.html",
                prof_counts=prof_counts,
                total_professors=total_professors,
                draft_counts=draft_counts,
                total_drafts=total_drafts,
                recent_sends=[dict(r) for r in recent_sends],
                outreach=outreach,
                matrix=matrix,
            )
        finally:
            workspace_conn.close()

    # ------------------------------------------------------------------
    # Professors
    # ------------------------------------------------------------------
    @app.route("/faculty")
    @login_required
    def professors_list():
        conn = _workspace_conn()
        try:
            status_filter = request.args.get("status")
            field_filter = request.args.get("field")
            profs = get_professors(
                conn,
                status=status_filter if status_filter else None,
                field=field_filter if field_filter else None,
            )
            all_profs = get_professors(conn)
            fields = sorted({p.field for p in all_profs if p.field})
            statuses = sorted({p.status for p in all_profs if p.status})

            return render_template(
                "professors.html",
                professors=profs, fields=fields, statuses=statuses,
                current_status=status_filter or "",
                current_field=field_filter or "",
            )
        finally:
            conn.close()

    @app.route("/professors/import", methods=["POST"])
    @login_required
    def professors_import():
        """Import professors from an uploaded CSV into this workspace."""
        upload = request.files.get("csv_file")
        if not upload or not upload.filename:
            flash("Choose a CSV file to import.", "error")
            return redirect(url_for("professors_list"))

        raw = upload.read()
        if len(raw) > 5 * 1024 * 1024:
            flash("That file is larger than 5 MB. Split it into smaller batches.", "error")
            return redirect(url_for("professors_list"))

        import csv as _csv
        import io as _io
        try:
            text = raw.decode("utf-8-sig", errors="replace")
            rows = list(_csv.DictReader(_io.StringIO(text)))
        except Exception as exc:
            flash(f"Could not read that CSV: {exc}", "error")
            return redirect(url_for("professors_list"))

        from app.csv_loader import import_professor_rows
        conn = _workspace_conn()
        try:
            imported, skipped, warnings = import_professor_rows(conn, rows)
        finally:
            conn.close()

        _log_activity(
            "csv_import", category="professors",
            details={"imported": imported, "skipped": skipped, "filename": upload.filename[:120]},
        )
        if imported:
            msg = f"Imported {imported} professor(s); skipped {skipped}."
            if warnings:
                msg += f" {len(warnings)} row note(s) — e.g. {warnings[0]}"
            flash(msg, "success")
        else:
            detail = warnings[0] if warnings else "No valid rows found."
            flash(f"Nothing imported. {detail}", "warning")
        return redirect(url_for("professors_list"))

    @app.route("/professors/<int:prof_id>")
    @login_required
    def professor_detail(prof_id: int):
        conn = _workspace_conn()
        try:
            prof = get_professor(conn, prof_id)
            if prof is None:
                flash("Professor not found.", "error")
                return redirect(url_for("professors_list"))

            drafts = conn.execute(
                "SELECT * FROM drafts WHERE professor_id = ? AND workspace_id = ? ORDER BY id DESC",
                (prof_id, conn.workspace_id),
            ).fetchall()
            draft_objs = [Draft.from_row(r) for r in drafts]

            send_history = [dict(r) for r in conn.execute(
                "SELECT sent_at, method, status, error_message FROM send_log "
                "WHERE professor_id = ? AND workspace_id = ? ORDER BY id DESC",
                (prof_id, conn.workspace_id),
            ).fetchall()]

            return render_template(
                "professor_detail.html", professor=prof,
                drafts=draft_objs, send_history=send_history,
            )
        finally:
            conn.close()

    def _is_placeholder(email: Optional[str]) -> bool:
        e = (email or "").strip().lower()
        return not e or e.endswith(".placeholder")

    @app.route("/professors/<int:prof_id>/find-email", methods=["POST"])
    @login_required
    def professor_find_email(prof_id: int):
        """On-demand: scrape the professor's profile page for a real email."""
        from app.enricher import find_professor_email
        conn = _workspace_conn()
        try:
            prof = get_professor(conn, prof_id)
            if prof is None:
                return jsonify({"success": False, "error": "Professor not found."}), 404
            if not prof.profile_url:
                return jsonify({
                    "success": False,
                    "error": "No profile URL on file. Add an email manually instead.",
                })
            found = find_professor_email(prof.profile_url, prof.name, prof.university or "")
            if not found:
                _log_activity("professor_find_email", category="finder",
                              target_type="professor", target_id=str(prof_id),
                              details={"result": "not_found"})
                return jsonify({
                    "success": False,
                    "error": "No published email found on that page. Add one manually.",
                })
            conn.execute(
                "UPDATE professors SET email = ?, status = CASE WHEN status = 'needs_email' "
                "THEN 'new' ELSE status END, updated_at = datetime('now') "
                "WHERE id = ? AND workspace_id = ?",
                (found, prof_id, conn.workspace_id),
            )
            conn.commit()
            _log_activity("professor_find_email", category="finder",
                          target_type="professor", target_id=str(prof_id),
                          details={"result": "found"})
            return jsonify({"success": True, "email": found})
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 500
        finally:
            conn.close()

    @app.route("/professors/<int:prof_id>/set-email", methods=["POST"])
    @login_required
    def professor_set_email(prof_id: int):
        """Manually set/correct a professor's email address."""
        conn = _workspace_conn()
        try:
            prof = get_professor(conn, prof_id)
            if prof is None:
                return jsonify({"success": False, "error": "Professor not found."}), 404
            new_email = (request.form.get("email") or
                         (request.get_json(silent=True) or {}).get("email") or "").strip()
            if "@" not in new_email or _is_placeholder(new_email):
                return jsonify({"success": False, "error": "Enter a valid email address."}), 400
            # Guard against colliding with another professor in this workspace.
            clash = conn.execute(
                "SELECT id FROM professors WHERE email = ? AND workspace_id = ? AND id != ?",
                (new_email, conn.workspace_id, prof_id),
            ).fetchone()
            if clash:
                return jsonify({
                    "success": False,
                    "error": "Another saved professor already uses that email.",
                }), 409
            conn.execute(
                "UPDATE professors SET email = ?, status = CASE WHEN status = 'needs_email' "
                "THEN 'new' ELSE status END, updated_at = datetime('now') "
                "WHERE id = ? AND workspace_id = ?",
                (new_email, prof_id, conn.workspace_id),
            )
            conn.commit()
            _log_activity("professor_set_email", category="finder",
                          target_type="professor", target_id=str(prof_id))
            if request.is_json:
                return jsonify({"success": True, "email": new_email})
            flash("Email updated.", "success")
            return redirect(url_for("professor_detail", prof_id=prof_id))
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 500
        finally:
            conn.close()

    @app.route("/professors/bulk-delete", methods=["POST"])
    @login_required
    def professors_bulk_delete():
        """Remove selected faculty files (and their drafts/sends/follow-ups)."""
        from app.database import delete_professors
        ids = request.form.getlist("professor_ids", type=int)
        if not ids:
            flash("Select at least one faculty file to remove.", "warning")
            return redirect(url_for("professors_list"))
        conn = _workspace_conn()
        try:
            removed = delete_professors(conn, ids)
            _log_activity("professors_bulk_delete", category="faculty",
                          details={"count": removed, "ids": ids[:50]})
            if removed:
                flash(f"Removed {removed} faculty file(s) and their drafts.", "success")
            else:
                flash("Nothing was removed.", "warning")
        except Exception as exc:
            flash(f"Could not remove the selected files: {exc}", "error")
        finally:
            conn.close()
        return redirect(url_for("professors_list"))

    @app.route("/professors/<int:prof_id>/delete", methods=["POST"])
    @login_required
    def professor_delete(prof_id: int):
        """Remove a single faculty file (and its drafts/sends/follow-ups)."""
        from app.database import delete_professors
        conn = _workspace_conn()
        try:
            removed = delete_professors(conn, [prof_id])
            _log_activity("professor_delete", category="faculty",
                          target_type="professor", target_id=str(prof_id))
            flash("Faculty file removed." if removed else "Faculty file not found.",
                  "success" if removed else "warning")
        except Exception as exc:
            flash(f"Could not remove the file: {exc}", "error")
        finally:
            conn.close()
        return redirect(url_for("professors_list"))

    @app.route("/professors/<int:prof_id>/edit", methods=["POST"])
    @login_required
    def professor_edit(prof_id: int):
        """Correct a faculty file. Fixing research_summary improves the AI draft,
        which now grounds the email in this text."""
        from app.database import update_professor
        conn = _workspace_conn()
        try:
            prof = get_professor(conn, prof_id)
            if prof is None:
                flash("Faculty file not found.", "warning")
                return redirect(url_for("professors_list"))
            f = request.form
            name = f.get("name", "").strip()
            if name:
                prof.name = name
            prof.email = f.get("email", prof.email).strip()
            prof.title = f.get("title", "").strip() or None
            prof.field = f.get("field", "").strip()
            prof.university = f.get("university", "").strip()
            prof.department = f.get("department", "").strip()
            prof.lab_name = f.get("lab_name", "").strip() or None
            prof.profile_url = f.get("profile_url", "").strip() or None
            prof.research_summary = f.get("research_summary", "").strip() or None
            prof.recent_work = f.get("recent_work", "").strip() or None
            prof.notes = f.get("notes", "").strip() or None
            update_professor(conn, prof)
            _log_activity("professor_edit", category="faculty",
                          target_type="professor", target_id=str(prof_id))
            flash("Faculty file updated.", "success")
        except Exception as exc:
            flash(f"Could not update the file: {exc}", "error")
        finally:
            conn.close()
        return redirect(url_for("professor_detail", prof_id=prof_id))

    # ------------------------------------------------------------------
    # Drafts
    # ------------------------------------------------------------------
    @app.route("/drafts")
    @login_required
    def drafts_list():
        conn = _workspace_conn()
        try:
            session_filter = request.args.get("session", type=int)
            status_filter = request.args.get("status")

            drafts = get_drafts(conn, session_id=session_filter,
                                status=status_filter if status_filter else None)

            prof_map: dict[int, str] = {
                pid: p.name
                for pid, p in get_professors_by_ids(
                    conn, {d.professor_id for d in drafts}
                ).items()
            }

            # Lightweight DISTINCT lookups for the filter dropdowns — far cheaper
            # than re-fetching every draft (with full bodies) just for two lists.
            wid = conn.workspace_id
            sessions = sorted(
                r["session_id"] for r in conn.execute(
                    "SELECT DISTINCT session_id FROM drafts WHERE workspace_id = ?",
                    (wid,),
                ).fetchall()
            )
            statuses = sorted(
                r["status"] for r in conn.execute(
                    "SELECT DISTINCT status FROM drafts WHERE workspace_id = ?",
                    (wid,),
                ).fetchall()
            )
            profiles = get_sender_profiles(conn)
            cfg = _workspace_config(conn)
            variants = list(cfg.generation.template_variants)
            professor_count = conn.execute(
                "SELECT COUNT(*) AS count FROM professors WHERE workspace_id = ?",
                (conn.workspace_id,),
            ).fetchone()["count"]

            return render_template(
                "drafts.html", drafts=drafts, prof_map=prof_map,
                sessions=sessions, statuses=statuses,
                current_session=session_filter or "",
                current_status=status_filter or "",
                profiles=profiles,
                variants=variants,
                professor_count=professor_count,
            )
        finally:
            conn.close()

    @app.route("/drafts/generate", methods=["POST"])
    @login_required
    def generate_drafts_route():
        conn = _workspace_conn()
        try:
            sender_profile_id = request.form.get("sender_profile_id", type=int)
            variant = request.form.get("variant", "").strip() or None
            professor_count = conn.execute(
                "SELECT COUNT(*) AS count FROM professors WHERE workspace_id = ?",
                (conn.workspace_id,),
            ).fetchone()["count"]
            if not sender_profile_id:
                flash("Choose a sender profile before generating drafts.", "error")
                return redirect(url_for("drafts_list"))
            if professor_count == 0:
                flash("Save at least one professor before generating drafts.", "error")
                return redirect(url_for("finder_page"))
            over_cap = _drafts_today(conn) >= _GENERATION_DAILY_LIMIT
            cfg = _workspace_config(conn)
        finally:
            conn.close()
        if over_cap:
            flash(
                f"You've reached today's generation limit ({_GENERATION_DAILY_LIMIT} drafts). "
                "It resets at midnight UTC.",
                "warning",
            )
            return redirect(url_for("drafts_list"))

        try:
            summary = run_generation_pipeline(
                db_path=cfg.db_path,
                config=cfg,
                sender_profile_id=sender_profile_id,
                variant=variant,
                workspace_id=_workspace_id(),
            )
        except Exception as exc:
            flash(f"Draft generation failed: {exc}", "error")
            return redirect(url_for("drafts_list"))

        _log_activity(
            "draft_generate",
            category="drafts",
            target_type="session",
            target_id=str(summary.session_id),
            details={
                "sender_profile_id": sender_profile_id,
                "variant": variant or "auto",
                "created": summary.created,
                "skipped": summary.skipped,
                "failed": summary.failed,
                "scored": summary.scored,
                "warnings": summary.warnings[:5],
            },
        )

        if summary.created == 0:
            warning_detail = f" {summary.warnings[0]}" if summary.warnings else ""
            flash(
                "No drafts were created. Add richer research summaries or save better faculty matches first."
                + warning_detail,
                "warning",
            )
            return redirect(url_for("drafts_list"))

        message = (
            f"Created {summary.created} draft(s) in session {summary.session_id}. "
            f"Skipped {summary.skipped}, failed {summary.failed}."
        )
        if summary.flagged_similarity:
            message += f" {summary.flagged_similarity} draft(s) were flagged for similarity."
        elif summary.warnings:
            message += f" {len(summary.warnings)} warning(s) were recorded during generation."
        flash(message, "success")
        return redirect(url_for("drafts_list", session=summary.session_id))

    @app.route("/drafts/sample", methods=["POST"])
    @login_required
    def sample_draft_route():
        """Generate a single draft so the user can eyeball tone before a batch."""
        conn = _workspace_conn()
        try:
            sender_profile_id = request.form.get("sender_profile_id", type=int)
            variant = request.form.get("variant", "").strip() or None
            if not sender_profile_id:
                flash("Choose a sender profile before generating a sample.", "error")
                return redirect(url_for("drafts_list"))
            # Prefer a professor who has no draft yet; otherwise just the first.
            wid = conn.workspace_id
            row = conn.execute(
                "SELECT id FROM professors WHERE workspace_id = ? AND id NOT IN "
                "(SELECT professor_id FROM drafts WHERE workspace_id = ?) ORDER BY id LIMIT 1",
                (wid, wid),
            ).fetchone() or conn.execute(
                "SELECT id FROM professors WHERE workspace_id = ? ORDER BY id LIMIT 1",
                (wid,),
            ).fetchone()
            if not row:
                flash("Save at least one professor before generating a sample.", "warning")
                return redirect(url_for("finder_page"))
            target_id = int(row["id"])
            over_cap = _drafts_today(conn) >= _GENERATION_DAILY_LIMIT
            cfg = _workspace_config(conn)
        finally:
            conn.close()
        if over_cap:
            flash(
                f"You've reached today's generation limit ({_GENERATION_DAILY_LIMIT} drafts). "
                "It resets at midnight UTC.",
                "warning",
            )
            return redirect(url_for("drafts_list"))

        try:
            summary = run_generation_pipeline(
                db_path=cfg.db_path, config=cfg,
                sender_profile_id=sender_profile_id, professor_ids=[target_id],
                variant=variant, workspace_id=_workspace_id(),
            )
        except Exception as exc:
            flash(f"Sample generation failed: {exc}", "error")
            return redirect(url_for("drafts_list"))

        _log_activity("draft_sample", category="drafts",
                      target_type="professor", target_id=str(target_id),
                      details={"created": summary.created})

        conn = _workspace_conn()
        try:
            made = conn.execute(
                "SELECT id FROM drafts WHERE workspace_id = ? AND session_id = ? "
                "AND professor_id = ? ORDER BY id DESC LIMIT 1",
                (conn.workspace_id, summary.session_id, target_id),
            ).fetchone()
        finally:
            conn.close()
        if made:
            flash("Sample draft ready — check the tone and specificity before a full batch.", "success")
            return redirect(url_for("draft_detail", draft_id=int(made["id"])))
        warning = summary.warnings[0] if summary.warnings else "the professor may need a richer research summary."
        flash(f"No sample draft was created — {warning}", "warning")
        return redirect(url_for("drafts_list"))

    @app.route("/drafts/<int:draft_id>")
    @login_required
    def draft_detail(draft_id: int):
        conn = _workspace_conn()
        try:
            draft = get_draft(conn, draft_id)
            if draft is None:
                flash("Draft not found.", "error")
                return redirect(url_for("drafts_list"))
            prof = get_professor(conn, draft.professor_id)
            from app.deliverability import scan_spam
            subject = draft.subject_lines_list[0] if draft.subject_lines_list else ""
            spam_flags = scan_spam(f"{subject}\n{draft.body}")
            return render_template("draft_detail.html", draft=draft, professor=prof, spam_flags=spam_flags)
        finally:
            conn.close()

    @app.route("/drafts/<int:draft_id>/approve", methods=["POST"])
    @login_required
    def approve_draft_route(draft_id: int):
        conn = _workspace_conn()
        try:
            draft = get_draft(conn, draft_id)
            if draft is None:
                return jsonify({"error": "Draft not found"}), 404
            update_draft_status(conn, draft_id, "approved")
            _log_activity("draft_approve", category="drafts",
                          target_type="draft", target_id=str(draft_id))
            return jsonify({"success": True, "status": "approved"})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        finally:
            conn.close()

    @app.route("/drafts/<int:draft_id>/reject", methods=["POST"])
    @login_required
    def reject_draft_route(draft_id: int):
        conn = _workspace_conn()
        try:
            draft = get_draft(conn, draft_id)
            if draft is None:
                return jsonify({"error": "Draft not found"}), 404
            notes = (request.get_json(silent=True) or {}).get("notes")
            update_draft_status(conn, draft_id, "rejected", notes=notes)
            _log_activity("draft_reject", category="drafts",
                          target_type="draft", target_id=str(draft_id),
                          details={"notes": notes} if notes else None)
            return jsonify({"success": True, "status": "rejected"})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        finally:
            conn.close()

    @app.route("/drafts/<int:draft_id>/outcome", methods=["POST"])
    @login_required
    def draft_outcome_route(draft_id: int):
        """Record whether the professor replied (excludes them from follow-ups)."""
        conn = _workspace_conn()
        try:
            draft = get_draft(conn, draft_id)
            if draft is None:
                return jsonify({"error": "Draft not found"}), 404
            outcome = (request.get_json(silent=True) or {}).get("outcome", "")
            from app.database import VALID_OUTCOMES
            if outcome not in VALID_OUTCOMES:
                return jsonify({"error": "Unknown outcome"}), 400
            set_draft_outcome(conn, draft_id, outcome)
            _log_activity("draft_outcome", category="drafts",
                          target_type="draft", target_id=str(draft_id),
                          details={"outcome": outcome or "none"})
            return jsonify({"success": True, "outcome": outcome})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        finally:
            conn.close()

    @app.route("/drafts/bulk", methods=["POST"])
    @login_required
    def bulk_draft_action():
        """Approve or reject every draft in the current filtered view at once."""
        action = request.form.get("action", "")
        if action not in {"approve", "reject"}:
            flash("Unknown bulk action.", "error")
            return redirect(url_for("drafts_list"))
        session_id = request.form.get("session", type=int)
        status_filter = request.form.get("status") or None
        new_status = "approved" if action == "approve" else "rejected"

        conn = _workspace_conn()
        try:
            drafts = get_drafts(conn, session_id=session_id, status=status_filter)
            # Don't touch already-sent drafts; don't re-approve rejected ones in bulk.
            skip = {"sent", new_status}
            changed = 0
            for d in drafts:
                if d.status in skip:
                    continue
                update_draft_status(conn, d.id, new_status)
                changed += 1
            _log_activity(
                "draft_bulk_" + action, category="drafts",
                details={"changed": changed, "session": session_id, "status_filter": status_filter},
            )
            if changed:
                flash(f"{action.capitalize()}d {changed} draft(s).", "success")
            else:
                flash("No drafts matched that bulk action.", "warning")
        except Exception as exc:
            flash(f"Bulk action failed: {exc}", "error")
        finally:
            conn.close()
        return redirect(url_for("drafts_list", session=session_id or "", status=status_filter or ""))

    @app.route("/drafts/bulk-delete", methods=["POST"])
    @login_required
    def drafts_bulk_delete():
        """Permanently remove selected drafts (or all rejected) from the queue."""
        from app.database import delete_drafts
        ids = request.form.getlist("draft_ids", type=int)
        return_status = request.form.get("return_status") or ""
        return_session = request.form.get("return_session") or ""
        # Convenience: "delete all rejected in view" without per-row selection.
        if not ids and request.form.get("scope") == "rejected":
            conn = _workspace_conn()
            try:
                session_id = request.form.get("session", type=int)
                rejected = get_drafts(conn, session_id=session_id, status="rejected")
                ids = [d.id for d in rejected]
            finally:
                conn.close()
        if not ids:
            flash("Select at least one draft to delete.", "warning")
            return redirect(url_for("drafts_list", status=return_status, session=return_session))
        conn = _workspace_conn()
        try:
            removed = delete_drafts(conn, ids)
            _log_activity("drafts_bulk_delete", category="drafts", details={"count": removed})
            flash(f"Deleted {removed} draft(s)." if removed else "Nothing was deleted.",
                  "success" if removed else "warning")
        except Exception as exc:
            flash(f"Could not delete the selected drafts: {exc}", "error")
        finally:
            conn.close()
        return redirect(url_for("drafts_list", status=return_status, session=return_session))

    @app.route("/drafts/<int:draft_id>/delete", methods=["POST"])
    @login_required
    def draft_delete(draft_id: int):
        """Permanently remove a single draft."""
        from app.database import delete_drafts
        conn = _workspace_conn()
        try:
            removed = delete_drafts(conn, [draft_id])
            _log_activity("draft_delete", category="drafts",
                          target_type="draft", target_id=str(draft_id))
            flash("Draft deleted." if removed else "Draft not found.",
                  "success" if removed else "warning")
        except Exception as exc:
            flash(f"Could not delete the draft: {exc}", "error")
        finally:
            conn.close()
        return redirect(url_for("drafts_list"))

    @app.route("/drafts/<int:draft_id>/edit", methods=["POST"])
    @login_required
    def edit_draft_route(draft_id: int):
        conn = _workspace_conn()
        try:
            draft = get_draft(conn, draft_id)
            if draft is None:
                return jsonify({"error": "Draft not found"}), 404
            data = request.get_json(silent=True) or {}
            new_body = data.get("body")
            new_subject = data.get("subject")
            if new_body is not None:
                conn.execute(
                    "UPDATE drafts SET body = ? WHERE id = ? AND workspace_id = ?",
                    (new_body, draft_id, conn.workspace_id),
                )
            if new_subject is not None:
                subjects = draft.subject_lines_list
                if subjects:
                    subjects[0] = new_subject
                else:
                    subjects = [new_subject]
                conn.execute(
                    "UPDATE drafts SET subject_lines = ? WHERE id = ? AND workspace_id = ?",
                    (json.dumps(subjects), draft_id, conn.workspace_id),
                )
            update_draft_status(conn, draft_id, "edited")
            _log_activity("draft_edit", category="drafts",
                          target_type="draft", target_id=str(draft_id),
                          details={"body_changed": new_body is not None, "subject_changed": new_subject is not None})
            return jsonify({"success": True, "status": "edited"})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------
    @app.route("/delivery")
    @login_required
    def send_page():
        conn = _workspace_conn()
        try:
            approved = get_drafts(conn, status="approved")
            edited = get_drafts(conn, status="edited")
            send_queue = approved + edited

            prof_map: dict[int, Professor] = get_professors_by_ids(
                conn, {d.professor_id for d in send_queue}
            )

            # Deliverability guardrails: daily cap + per-draft spam-risk flags.
            from app.deliverability import scan_spam, daily_send_count, cap_status
            spam_flags: dict[int, list[str]] = {}
            for d in send_queue:
                subject = d.subject_lines_list[0] if d.subject_lines_list else ""
                flags = scan_spam(f"{subject}\n{d.body}")
                if flags:
                    spam_flags[d.id] = flags
            caps = cap_status(daily_send_count(conn), len(send_queue))

            return render_template(
                "send.html", send_queue=send_queue, prof_map=prof_map,
                spam_flags=spam_flags, caps=caps,
            )
        finally:
            conn.close()

    @app.route("/send", methods=["POST"])
    @login_required
    def send_trigger():
        conn = _workspace_conn()
        try:
            data = request.get_json(silent=True) or {}
            raw_dry_run = data.get("dry_run", True)
            dry_run = parse_bool(raw_dry_run, default=True)
            method = str(data.get("method", "smtp")).strip()
            if method not in SEND_METHODS:
                return jsonify({
                    "success": False,
                    "error": f"Unknown send method: {method}",
                }), 400
            try:
                requested_limit = int(data.get("limit", 10))
            except (TypeError, ValueError):
                requested_limit = 10
            limit = max(1, min(requested_limit, 50))

            try:
                cfg = _workspace_config(conn)
                if not cfg:
                    return jsonify({"success": False, "error": "Config not loaded."}), 500

                result = send_ready_queue(
                    conn,
                    cfg,
                    method=method,
                    limit=limit,
                    dry_run=dry_run,
                    cooldown=False,
                )
                _log_activity(
                    "send_dry_run" if dry_run else "send_execute",
                    category="send",
                    details={
                        "count": result.get("count", 0),
                        "method": method,
                        "sent": result.get("sent", 0),
                        "failed": result.get("failed", 0),
                        "status": result.get("status"),
                    },
                )
                # "empty" (nothing approved yet) is a benign state, not a client
                # error — return 200 so the UI shows the message cleanly.
                ok = result.get("success") or result.get("results") or result.get("status") == "empty"
                return jsonify(result), 200 if ok else 400
            except ImportError:
                return jsonify({"success": False, "error": "Sender module not available"}), 500
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Follow-ups (7-10 day nudge scheduler)
    # ------------------------------------------------------------------
    def _followup_overview(conn, days_since: int = 7):
        """Return (existing_followups, drafts_due_for_followup) for the workspace."""
        from app.database import get_followups
        followups = get_followups(conn)
        already = {f.original_draft_id for f in followups}

        sent_map: dict[int, str] = {}
        try:
            for r in conn.execute(
                "SELECT draft_id, MAX(sent_at) AS s FROM send_log "
                "WHERE workspace_id = ? AND status = 'success' GROUP BY draft_id",
                (conn.workspace_id,),
            ).fetchall():
                sent_map[r["draft_id"]] = r["s"]
        except Exception:
            pass

        from datetime import timedelta as _td
        cutoff = (datetime.utcnow() - _td(days=max(0, days_since))).isoformat()
        eligible = []
        for d in get_drafts(conn, status="sent"):
            if d.id in already:
                continue
            if d.outcome:  # professor already replied — don't nudge them
                continue
            sent_at = sent_map.get(d.id) or d.reviewed_at or d.created_at
            if sent_at and sent_at <= cutoff:
                eligible.append(d)
        return followups, eligible

    @app.route("/follow-ups")
    @login_required
    def followups_page():
        conn = _workspace_conn()
        try:
            days_since = request.args.get("days_since", default=7, type=int)
            followups, eligible = _followup_overview(conn, days_since)
            prof_map: dict[int, Professor] = get_professors_by_ids(
                conn,
                {f.professor_id for f in followups} | {d.professor_id for d in eligible},
            )
            return render_template(
                "followups.html", followups=followups, eligible=eligible,
                prof_map=prof_map, days_since=days_since,
            )
        finally:
            conn.close()

    @app.route("/followups/generate", methods=["POST"])
    @login_required
    def followups_generate():
        from app.database import get_sender_profile, insert_followup
        from app.template_engine import render_followup
        days_since = request.form.get("days_since", default=7, type=int)
        conn = _workspace_conn()
        try:
            cfg = _workspace_config(conn)
            _, eligible = _followup_overview(conn, days_since)
            generated = 0
            errors = 0
            for d in eligible:
                try:
                    prof = get_professor(conn, d.professor_id)
                    sender = get_sender_profile(conn, d.sender_profile_id)
                    if not prof or not sender:
                        errors += 1
                        continue
                    insert_followup(conn, render_followup(prof, sender, d, cfg))
                    generated += 1
                except Exception as exc:
                    errors += 1
                    logger.warning("Follow-up generation failed for draft %s: %s", d.id, exc)
            _log_activity("followup_generate", category="drafts",
                          details={"generated": generated, "errors": errors, "days_since": days_since})
            if generated:
                flash(f"Generated {generated} follow-up(s). Review and copy them below.", "success")
            else:
                flash(
                    f"Nothing due yet — follow-ups appear once a draft has been sent "
                    f"and is at least {days_since} day(s) old.",
                    "warning",
                )
            return redirect(url_for("followups_page", days_since=days_since))
        except Exception as exc:
            flash(f"Follow-up generation failed: {exc}", "error")
            return redirect(url_for("followups_page"))
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    @app.route("/export")
    @login_required
    def export_page():
        return render_template("export.html")

    @app.route("/export", methods=["POST"])
    @login_required
    def export_trigger():
        conn = _workspace_conn()
        try:
            cfg = _workspace_config(conn)
            if not cfg:
                return jsonify({"error": "Config not loaded"}), 500

            output_dir = _workspace_output_dir()
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            filename = f"drafts_export_{timestamp}.csv"
            filepath = output_dir / filename

            import csv
            drafts = get_drafts(conn)
            with open(filepath, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["id", "professor_id", "session_id", "subject", "body", "overall_score", "status", "warnings", "created_at"])
                for d in drafts:
                    subj = d.subject_lines_list[0] if d.subject_lines_list else ""
                    writer.writerow([d.id, d.professor_id, d.session_id, subj, d.body, d.overall_score, d.status, ", ".join(d.warnings_list), d.created_at])

            return jsonify({"success": True, "filename": filename, "download_url": url_for("download_export", filename=filename)})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        finally:
            conn.close()

    @app.route("/export/download/<filename>")
    @login_required
    def download_export(filename: str):
        filepath = _workspace_output_dir() / filename
        if not filepath.exists():
            flash("File not found.", "error")
            return redirect(url_for("export_page"))
        return send_file(str(filepath), as_attachment=True)

    def _write_csv_export(prefix: str, header: list, rows: list) -> dict:
        """Write a CSV to the workspace output dir; return the JSON download payload."""
        import csv
        output_dir = _workspace_output_dir()
        filename = f"{prefix}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
        with open(output_dir / filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(rows)
        return {"success": True, "filename": filename,
                "download_url": url_for("download_export", filename=filename)}

    @app.route("/export/tracking", methods=["POST"])
    @login_required
    def export_tracking():
        """A clean outreach tracking sheet (one row per draft) for advisors/handoff."""
        conn = _workspace_conn()
        try:
            drafts = get_drafts(conn)
            pmap = get_professors_by_ids(conn, {d.professor_id for d in drafts})
            rows = []
            for d in drafts:
                p = pmap.get(d.professor_id)
                subj = d.subject_lines_list[0] if d.subject_lines_list else ""
                rows.append([
                    p.name if p else "?", p.university if p else "", p.email if p else "",
                    p.field if p else "", subj, round(d.overall_score, 1), d.status,
                    d.outcome or "awaiting", d.replied_at or "", d.created_at,
                ])
            payload = _write_csv_export(
                "outreach_tracking",
                ["professor", "university", "email", "field", "subject", "score",
                 "status", "reply_outcome", "replied_at", "created_at"],
                rows,
            )
            _log_activity("export_tracking", category="export", details={"rows": len(rows)})
            return jsonify(payload)
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 500
        finally:
            conn.close()

    @app.route("/export/activity", methods=["POST"])
    @login_required
    def export_activity():
        """A shareable record of what changed in this workspace and when."""
        conn = _workspace_conn()
        try:
            wid = conn.workspace_id
            log_rows = conn.execute(
                "SELECT timestamp, action, entity_type, entity_id, details "
                "FROM audit_log WHERE workspace_id = ? ORDER BY id DESC LIMIT 2000",
                (wid,),
            ).fetchall()
            rows = [[r["timestamp"], r["action"], r["entity_type"], r["entity_id"], r["details"]]
                    for r in log_rows]
            payload = _write_csv_export(
                "activity_log",
                ["timestamp", "action", "entity_type", "entity_id", "details"],
                rows,
            )
            _log_activity("export_activity", category="export", details={"rows": len(rows)})
            return jsonify(payload)
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 500
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    # Slugs verified against the live OpenRouter catalog. Ordered cheap -> premium
    # so the parsing-model dropdown reads top-down from most economical.
    _LLM_MODELS: dict[str, str] = {
        "google/gemini-2.5-flash": "Gemini 2.5 Flash (cheapest)",
        "google/gemini-3.5-flash": "Gemini 3.5 Flash",
        "google/gemini-2.5-pro": "Gemini 2.5 Pro",
        "anthropic/claude-haiku-4.5": "Claude Haiku 4.5",
        "anthropic/claude-sonnet-4.6": "Claude Sonnet 4.6",
        "anthropic/claude-opus-4.8": "Claude Opus 4.8 (best)",
    }

    _EMAIL_PROVIDERS: dict[str, str] = {
        "gmail": "Gmail",
        "outlook": "Outlook",
        "hotmail": "Hotmail",
    }

    @app.route("/setup")
    @login_required
    def settings_page():
        conn = _workspace_conn()
        try:
            cfg = _workspace_config(conn)
            profiles = get_sender_profiles(conn)
            suppression = get_suppression_list(conn)
            saved = get_all_settings(conn)

            effective: dict[str, Any] = {
                "sender_email": cfg.sender_email if cfg else "",
                "llm_provider": cfg.llm_provider if cfg else "",
                "llm_api_key_set": bool((cfg.llm_api_key if cfg else "") or os.environ.get("LLM_API_KEY", "")),
                "llm_model": cfg.llm_model if cfg else "google/gemini-2.5-flash",
                "llm_model_parse": (cfg.llm_model_parse if cfg else "") or "",
                "email_provider": cfg.email_provider if cfg else "gmail",
                "smtp_user": cfg.smtp_user if cfg else "",
                "smtp_password": cfg.smtp_password if cfg else "",
                "auto_send_enabled": parse_bool(saved.get("auto_send_enabled"), default=False),
                "auto_send_method": saved.get("auto_send_method", "smtp"),
                "auto_send_limit": saved.get("auto_send_limit", "5"),
                "auto_send_last_run": saved.get("auto_send_last_run", ""),
                "auto_send_last_status": saved.get("auto_send_last_status", ""),
                "auto_followup_enabled": parse_bool(saved.get("auto_followup_enabled"), default=False),
                "auto_followup_days": saved.get("auto_followup_days", "7"),
                "auto_followup_limit": saved.get("auto_followup_limit", "5"),
                "auto_followup_last_run": saved.get("auto_followup_last_run", ""),
                "workspace_owner_email": saved.get("workspace_owner_email", ""),
                "workspace_owner_name": saved.get("workspace_owner_name", session.get("key_label", "")),
                "attachment_filename": saved.get("attachment_filename", ""),
            }
            delivery_checks = delivery_setup_diagnostics(cfg, conn) if cfg else []

            return render_template(
                "settings.html", effective=effective,
                llm_models=_LLM_MODELS, email_providers=_EMAIL_PROVIDERS,
                profiles=profiles, suppression=suppression,
                delivery_checks=delivery_checks,
            )
        finally:
            conn.close()

    @app.route("/settings", methods=["POST"])
    @login_required
    def settings_save():
        conn = _workspace_conn()
        try:
            new_settings: dict[str, str] = {}
            for key in ("sender_email", "llm_provider", "llm_model", "llm_model_parse",
                        "email_provider", "smtp_user", "smtp_password",
                        "auto_send_method", "auto_send_limit",
                        "auto_followup_days", "auto_followup_limit"):
                val = request.form.get(key, "").strip()
                new_settings[key] = val
            new_settings["auto_send_enabled"] = "1" if request.form.get("auto_send_enabled") else "0"
            new_settings["auto_followup_enabled"] = "1" if request.form.get("auto_followup_enabled") else "0"

            set_settings_bulk(conn, new_settings)

            _log_activity("settings_update", category="settings",
                          details={k: v for k, v in new_settings.items() if k != "smtp_password"})
            flash("Settings saved for this workspace only.", "success")
            return redirect(url_for("settings_page"))
        except Exception as exc:
            flash(f"Failed to save settings: {exc}", "error")
            return redirect(url_for("settings_page"))
        finally:
            conn.close()

    @app.route("/settings/attachment", methods=["POST"])
    @login_required
    def settings_attachment_upload():
        """Store a single outgoing attachment (e.g. a CV PDF) for this workspace."""
        upload = request.files.get("attachment")
        if not upload or not upload.filename:
            flash("Choose a file to attach.", "error")
            return redirect(url_for("settings_page"))
        raw = upload.read()
        if not raw:
            flash("That file was empty.", "error")
            return redirect(url_for("settings_page"))
        if len(raw) > 3 * 1024 * 1024:
            flash("Attachments must be 3 MB or smaller (most CVs are well under 1 MB).", "error")
            return redirect(url_for("settings_page"))

        import base64 as _b64
        # Strip path and control chars (newlines etc.) so the name is safe to
        # place in an email Content-Disposition header later.
        filename = "".join(
            ch for ch in os.path.basename(upload.filename) if ch >= " " and ch != "\x7f"
        )[:160] or "attachment"
        conn = _workspace_conn()
        try:
            set_settings_bulk(conn, {
                "attachment_filename": filename,
                "attachment_mimetype": upload.mimetype or "application/octet-stream",
                "attachment_b64": _b64.b64encode(raw).decode("ascii"),
            })
        finally:
            conn.close()
        _log_activity("attachment_upload", category="settings",
                      details={"filename": filename, "bytes": len(raw)})
        flash(f"Attached “{filename}” — it will be included on every email you send.", "success")
        return redirect(url_for("settings_page"))

    @app.route("/settings/attachment/remove", methods=["POST"])
    @login_required
    def settings_attachment_remove():
        conn = _workspace_conn()
        try:
            set_settings_bulk(conn, {
                "attachment_filename": "",
                "attachment_mimetype": "",
                "attachment_b64": "",
            })
        finally:
            conn.close()
        _log_activity("attachment_remove", category="settings")
        flash("Attachment removed. Emails will send without a file.", "success")
        return redirect(url_for("settings_page"))

    @app.route("/settings/test-email", methods=["POST"])
    @login_required
    def settings_test_email():
        conn = _workspace_conn()
        try:
            cfg = _workspace_config(conn)
            saved = get_all_settings(conn)
            recipient = request.form.get("test_recipient", "").strip() or cfg.sender_email
            result = send_mailbox_test(
                cfg,
                recipient=recipient,
                sender_name=saved.get("workspace_owner_name", session.get("key_label", "")),
            )
            _log_activity(
                "mailbox_test",
                category="settings",
                details={
                    "recipient": recipient,
                    "status": result.get("status"),
                    "success": result.get("success"),
                },
            )
            if result.get("success"):
                flash(f"Test email sent to {result.get('recipient')}.", "success")
            else:
                details = "; ".join(result.get("errors", [])) if result.get("errors") else result.get("error", "Unknown error")
                flash(f"Test email failed: {details}", "error")
            return redirect(url_for("settings_page"))
        except Exception as exc:
            flash(f"Test email failed: {exc}", "error")
            return redirect(url_for("settings_page"))
        finally:
            conn.close()

    @app.route("/settings/test-ai", methods=["POST"])
    @login_required
    def settings_test_ai():
        """Prove the configured AI provider/model/key can actually generate.

        Runs one tiny completion so a bad key or a retired model slug surfaces
        here instead of silently breaking draft generation later.
        """
        conn = _workspace_conn()
        try:
            cfg = _workspace_config(conn)
            provider = (cfg.llm_provider or "").strip()
            api_key = (cfg.llm_api_key or os.environ.get("LLM_API_KEY", "")).strip()
            model = cfg.llm_model
            if not provider or not api_key:
                flash(
                    "AI is in keyword-only mode: set an LLM provider and API key "
                    "(or the LLM_API_KEY env var) to generate personalized drafts.",
                    "warning",
                )
                return redirect(url_for("settings_page"))

            from app.summarizer import probe_openrouter, DEFAULT_PARSE_MODEL
            parse_model = (cfg.llm_model_parse or DEFAULT_PARSE_MODEL)

            # Probe each model independently and report the model OpenRouter
            # actually served — so "is it really using model X?" is verifiable,
            # not assumed.
            results = []
            all_ok = True
            for label, m in (("Writing", model), ("Parsing", parse_model)):
                r = probe_openrouter(api_key, m)
                all_ok = all_ok and r["ok"]
                if r["ok"]:
                    # OpenRouter resolves a slug to its dated snapshot (e.g.
                    # claude-sonnet-4.6 -> claude-4.6-sonnet-20260217); that's
                    # normal, so show it as info, not a warning.
                    served = r["served_model"]
                    info = f" (served {served})" if served and served != m else ""
                    results.append(f"{label}: {m} ✓{info}")
                else:
                    results.append(f"{label}: {m} ✗ — {r['error']}")

            _log_activity("ai_test", category="settings",
                          details={"provider": provider, "writing": model,
                                   "parsing": parse_model, "ok": all_ok})
            summary = " · ".join(results)
            flash(("AI check — " + summary) if all_ok else ("AI check found problems — " + summary),
                  "success" if all_ok else "error")
            return redirect(url_for("settings_page"))
        except Exception as exc:
            _log_activity("ai_test", category="settings",
                          details={"model": locals().get("model"), "ok": False, "error": str(exc)[:200]})
            flash(
                f"AI test failed for {locals().get('model', 'the configured model')}: "
                f"{exc}. Check the API key and that the model name is current.",
                "error",
            )
            return redirect(url_for("settings_page"))
        finally:
            conn.close()

    @app.route("/settings/auto-send/<action>", methods=["POST"])
    @login_required
    def settings_auto_send_action(action: str):
        if action not in {"preview", "run"}:
            flash("Unknown automatic delivery action.", "error")
            return redirect(url_for("settings_page"))

        conn = _workspace_conn()
        try:
            cfg = _workspace_config(conn)
            saved = get_all_settings(conn)
            enabled, method, limit = auto_send_preferences(saved)
            if action == "run" and not enabled:
                flash("Enable automatic delivery before running the auto queue.", "error")
                return redirect(url_for("settings_page"))

            result = send_ready_queue(
                conn,
                cfg,
                method=method,
                limit=limit,
                dry_run=action == "preview",
                cooldown=False,
            )
            persist_auto_send_result(conn, result)
            _log_activity(
                "auto_send_preview" if action == "preview" else "auto_send_run_now",
                category="send",
                details={
                    "method": method,
                    "limit": limit,
                    "status": result.get("status"),
                    "sent": result.get("sent", 0),
                    "failed": result.get("failed", 0),
                    "count": result.get("count", 0),
                },
            )
            summary = (
                f"Auto {'preview' if action == 'preview' else 'delivery'}: "
                f"{result.get('count', 0)} draft(s), "
                f"{result.get('sent', 0)} sent, {result.get('failed', 0)} failed."
            )
            if result.get("success"):
                flash(summary, "success")
            else:
                errors = "; ".join(result.get("errors", [])) if result.get("errors") else result.get("error", "No runnable auto-delivery queue.")
                flash(f"{summary} {errors}", "error")
            return redirect(url_for("settings_page"))
        except Exception as exc:
            flash(f"Automatic delivery failed: {exc}", "error")
            return redirect(url_for("settings_page"))
        finally:
            conn.close()

    @app.route("/settings/profiles", methods=["POST"])
    @login_required
    def create_sender_profile_route():
        conn = _workspace_conn()
        try:
            profile = SenderProfile(
                name=request.form.get("name", "").strip(),
                school=request.form.get("school", "").strip(),
                grade=request.form.get("grade", "").strip(),
                email=request.form.get("email", "").strip(),
                interests=request.form.get("interests", "").strip(),
                background=request.form.get("background", "").strip(),
                graduation_year=request.form.get("graduation_year", "").strip() or None,
                awards=request.form.get("awards", "").strip(),
                skills=request.form.get("skills", "").strip(),
                goal=request.form.get("goal", "").strip(),
                age=request.form.get("age", "").strip(),
            )
            if not profile.name or not profile.school or not profile.grade or not profile.email:
                flash("Name, school, grade, and email are required for a sender profile.", "error")
                return redirect(url_for("settings_page"))

            edit_id = request.form.get("profile_id", type=int)
            if edit_id:
                from app.database import update_sender_profile
                changed = update_sender_profile(conn, edit_id, profile)
                _log_activity("sender_profile_update", category="settings",
                              target_type="sender_profile", target_id=str(edit_id),
                              details={"email": profile.email})
                flash("Sender profile updated." if changed else "Profile not found.",
                      "success" if changed else "warning")
                return redirect(url_for("settings_page"))

            profile_id = insert_sender_profile(conn, profile)
            _log_activity(
                "sender_profile_create",
                category="settings",
                target_type="sender_profile",
                target_id=str(profile_id),
                details={"email": profile.email, "school": profile.school},
            )
            flash("Sender profile saved to this workspace.", "success")
            return redirect(url_for("settings_page"))
        except Exception as exc:
            flash(f"Failed to save sender profile: {exc}", "error")
            return redirect(url_for("settings_page"))
        finally:
            conn.close()

    @app.route("/settings/profiles/<int:profile_id>/delete", methods=["POST"])
    @login_required
    def delete_sender_profile_route(profile_id: int):
        from app.database import delete_sender_profile
        conn = _workspace_conn()
        try:
            result = delete_sender_profile(conn, profile_id)
            if result == "deleted":
                _log_activity("sender_profile_delete", category="settings",
                              target_type="sender_profile", target_id=str(profile_id))
                flash("Sender profile deleted.", "success")
            elif result == "in_use":
                flash("That profile is used by existing drafts or sessions, so it wasn't deleted. "
                      "Delete those drafts first if you really want to remove it.", "warning")
            else:
                flash("Sender profile not found.", "warning")
        except Exception as exc:
            flash(f"Could not delete the profile: {exc}", "error")
        finally:
            conn.close()
        return redirect(url_for("settings_page"))

    # ------------------------------------------------------------------
    # Professor Finder
    # ------------------------------------------------------------------

    @app.route("/search")
    @login_required
    def finder_page():
        from app.finder import list_known_universities
        universities = list_known_universities()
        return render_template("finder.html", universities=universities)

    @app.route("/finder/search", methods=["POST"])
    @login_required
    def finder_search():
        from app.finder import find_professors
        data = request.get_json(silent=True) or {}
        query = data.get("scholar_query", "").strip()
        universities = data.get("universities", [])
        field = data.get("field", "").strip()
        max_results = min(int(data.get("max_results", 25)), 50)
        sources = data.get("sources") or None  # None = all sources
        journals = [j.strip() for j in (data.get("journals") or []) if j and j.strip()]
        arxiv_categories = [c.strip() for c in (data.get("arxiv_categories") or []) if c and c.strip()]

        if not query and not journals and not arxiv_categories:
            return jsonify({"success": False, "error": "Enter a research topic, a journal name/ISSN, or an arXiv category."})

        try:
            professors, warnings = find_professors(
                query=query,
                universities=universities if universities else None,
                field=field,
                max_scholar_results=max_results,
                sources=sources,
                journals=journals or None,
                arxiv_categories=arxiv_categories or None,
            )

            results = []
            for prof in professors:
                source = "Scholar" if "scholar" in (prof.profile_url or "").lower() or "Scholar" in (prof.notes or "") else "Directory"
                results.append({
                    "name": prof.name,
                    "university": prof.university or "",
                    "email": prof.email or "",
                    "field": prof.field or "",
                    "title": prof.title or "",
                    "profile_url": prof.profile_url or "",
                    "research_summary": prof.research_summary or "",
                    "notes": prof.notes or "",
                    "source": source,
                })

            return jsonify({
                "success": True,
                "count": len(results),
                "professors": results,
                "warnings": warnings,
            })
        except Exception as exc:
            logger.exception("Finder search failed")
            return jsonify({"success": False, "error": str(exc)})

    # Cap how many faculty pages we scrape per save so the request stays well
    # within serverless time limits; the rest can be looked up on demand later.
    _MAX_EMAIL_LOOKUPS_PER_SAVE = 12

    @app.route("/finder/save", methods=["POST"])
    @login_required
    def finder_save():
        from app.database import upsert_professor
        from app.enricher import find_professor_email
        data = request.get_json(silent=True) or {}
        professors_data = data.get("professors", [])

        if not professors_data:
            return jsonify({"success": False, "error": "No professors to save."})

        conn = _workspace_conn()
        saved = 0
        skipped = 0
        errors = 0
        email_found = 0
        needs_email = 0
        lookups_used = 0
        try:
            for pd in professors_data:
                try:
                    name = pd.get("name", "").strip()
                    if not name:
                        skipped += 1
                        continue

                    university = pd.get("university", "")
                    email = pd.get("email", "").strip()
                    profile_url = (pd.get("profile_url") or "").strip()

                    # No email from the finder — try to scrape a real one from
                    # the faculty page (best-effort, bounded). Never guess.
                    if not email and profile_url and lookups_used < _MAX_EMAIL_LOOKUPS_PER_SAVE:
                        lookups_used += 1
                        discovered = find_professor_email(profile_url, name, university, timeout=8)
                        if discovered:
                            email = discovered

                    if email:
                        prof_status = "new"
                        email_found += 1
                    else:
                        # Unique, clearly non-sendable placeholder so each
                        # professor still gets their own row. Flagged for the
                        # user to add or look up an address.
                        slug = name.lower().replace(" ", ".").replace(",", "")
                        uni_slug = (university or "unknown").lower().replace(" ", "-")[:30]
                        email = f"{slug}@{uni_slug}.placeholder"
                        prof_status = "needs_email"
                        needs_email += 1

                    # Check if this exact name+university already exists (avoid
                    # saving duplicates from the same search)
                    existing = conn.execute(
                        "SELECT id, email FROM professors WHERE name = ? AND university = ? AND workspace_id = ?",
                        (name, university, conn.workspace_id),
                    ).fetchone()
                    if existing:
                        # Update context, and upgrade to a real email/status only
                        # when we actually found one (never downgrade a real email
                        # back to a placeholder).
                        promote = prof_status == "new" and str(existing["email"] or "").endswith(".placeholder")
                        conn.execute(
                            """UPDATE professors SET
                                field = COALESCE(NULLIF(?, ''), field),
                                profile_url = COALESCE(NULLIF(?, ''), profile_url),
                                research_summary = COALESCE(NULLIF(?, ''), research_summary),
                                notes = COALESCE(NULLIF(?, ''), notes),
                                email = CASE WHEN ? THEN ? ELSE email END,
                                status = CASE WHEN ? THEN 'new' ELSE status END,
                                updated_at = datetime('now')
                            WHERE id = ? AND workspace_id = ?""",
                            (pd.get("field", ""), profile_url,
                             pd.get("research_summary", ""), pd.get("notes", ""),
                             promote, email, promote,
                             existing["id"], conn.workspace_id),
                        )
                        conn.commit()
                        saved += 1
                        continue

                    prof = Professor(
                        name=name,
                        title=pd.get("title"),
                        email=email,
                        university=university,
                        department=pd.get("department", "Computer Science"),
                        field=pd.get("field", ""),
                        profile_url=pd.get("profile_url"),
                        research_summary=pd.get("research_summary"),
                        notes=pd.get("notes"),
                        status=prof_status,
                    )
                    upsert_professor(conn, prof)
                    saved += 1
                except Exception as exc:
                    errors += 1
                    logger.warning("Failed to save professor %s: %s", pd.get("name"), exc)
        finally:
            conn.close()

        _log_activity("finder_save_professors", category="finder",
                      details={"saved": saved, "skipped": skipped, "errors": errors,
                               "email_found": email_found, "needs_email": needs_email,
                               "names": [p.get("name") for p in professors_data[:10]]})
        return jsonify({
            "success": True,
            "saved": saved,
            "skipped": skipped,
            "errors": errors,
            "email_found": email_found,
            "needs_email": needs_email,
        })

    # ------------------------------------------------------------------
    # Chat Support & Bug Reports API
    # ------------------------------------------------------------------
    # Ensure chat and bug tables exist
    if cfg:
        try:
            conn = get_connection(cfg.db_path)
            conn.execute("""CREATE TABLE IF NOT EXISTS chat_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_key_id INTEGER,
                user_label TEXT,
                user_message TEXT NOT NULL,
                bot_response TEXT NOT NULL,
                prompt_key TEXT,
                ip_address TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS bug_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_key_id INTEGER,
                user_label TEXT,
                title TEXT NOT NULL,
                details TEXT,
                severity TEXT DEFAULT 'medium',
                status TEXT DEFAULT 'open',
                ip_address TEXT,
                user_agent TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )""")
            conn.commit()
            # One-shot, token-gated hard reset (set HARD_RESET_TOKEN to wipe all
            # user data once on the next deploy; keeps schema + admin + global config).
            reset_token = os.environ.get("HARD_RESET_TOKEN", "").strip()
            if reset_token:
                from app.database import maybe_hard_reset
                if maybe_hard_reset(conn, reset_token):
                    logger.warning("Startup hard reset executed (token consumed).")
            conn.close()
        except Exception:
            pass

    @app.route("/api/chat/log", methods=["POST"])
    @login_required
    def chat_log():
        """Log a chat interaction."""
        data = request.get_json(silent=True) or {}
        user_message = data.get("user_message", "")
        bot_response = data.get("bot_response", "")
        prompt_key = data.get("prompt_key")

        if not user_message:
            return jsonify({"success": False, "error": "No message"})

        conn = _auth_conn()
        try:
            ip = request.headers.get("X-Forwarded-For", request.remote_addr)
            if ip and "," in ip:
                ip = ip.split(",")[0].strip()
            conn.execute(
                """INSERT INTO chat_logs
                   (user_key_id, user_label, user_message, bot_response, prompt_key, ip_address, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (session.get("key_id"), session.get("key_label", ""),
                 user_message[:2000], bot_response[:5000], prompt_key, ip,
                 datetime.utcnow().isoformat()),
            )
            conn.commit()
            # Also log to admin activity
            _log_activity(
                "chat_message", category="chat",
                details={"user_message": user_message[:200], "prompt_key": prompt_key},
            )
        finally:
            conn.close()

        return jsonify({"success": True})

    _CHAT_SYSTEM = (
        "You are the in-app assistant for Academic Outreach, a tool that helps high-school and "
        "undergraduate students send genuine, specific cold emails to professors. Workflow: "
        "Search (find faculty across OpenAlex/Crossref/DBLP/arXiv/Semantic Scholar and by journal "
        "name or ISSN), Faculty (the saved shortlist), Drafts (AI-generated, scored 1-10, with "
        "spam-risk flags), Delivery (send through the student's own Gmail), Follow-ups (one polite "
        "nudge ~7-10 days later), Setup (writing/parsing models, sender identity, SMTP). "
        "Help with two things: (1) how to use the app, concisely; (2) outreach quality — critique "
        "or rewrite the student's drafts to be specific, grounded in the professor's real work, "
        "humble, and non-spammy, with a bounded ask (e.g. a 15-minute conversation). Use an "
        "authentic student voice: no flattery, no fabricated papers, results, or credentials. "
        "Keep replies short and practical. Don't invent professor details. If asked something "
        "off-topic, gently steer back to outreach. "
        "You can call tools to look up the user's live workspace data (funnel stats, their drafts, "
        "who's due for a follow-up), to search for faculty, and to make reversible changes: mark a "
        "professor's reply, or approve/reject a draft. Use tools when the question needs real data "
        "or a change; otherwise just answer. Before approving/rejecting or marking a reply, make "
        "sure you have the right draft id (list drafts first if unsure), and confirm what you did "
        "afterward. After a tool runs, summarize plainly — don't dump raw JSON. "
        "You may also generate ONE draft for a saved professor with generate_draft — but this costs "
        "the user's AI tokens, so the app will pause and ask the user to confirm before it runs; "
        "just call the tool with the professor id (look it up with search/list first) and the app "
        "handles the confirmation. You CANNOT send email — that leaves the user's inbox, so always "
        "tell the user to review and send from the Delivery page themselves."
    )

    # Read-only tools the assistant may call. No writes, no sends, no token spend
    # beyond the chat itself — everything is scoped to the user's workspace.
    _CHAT_TOOLS = [
        {"type": "function", "function": {
            "name": "search_faculty",
            "description": "Find professors by research topic, optionally at specific universities. Returns candidate names, affiliations, and fields. Does NOT save them — tell the user to save from the Search page.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "research topic, e.g. 'graph neural networks'"},
                "universities": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer"},
            }, "required": ["query"]}}},
        {"type": "function", "function": {
            "name": "list_faculty",
            "description": "List the professors the user has SAVED to their workspace (id, name, university, field, and whether a draft already exists). Use this to get a professor's id before generate_draft.",
            "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {
            "name": "outreach_stats",
            "description": "The workspace's outreach funnel: sent, replied, reply rate, meetings.",
            "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {
            "name": "list_drafts",
            "description": "List the user's drafts (id, professor, status, score, reply outcome), optionally filtered by status: generated|edited|approved|rejected|sent.",
            "parameters": {"type": "object", "properties": {"status": {"type": "string"}}}}},
        {"type": "function", "function": {
            "name": "followups_due",
            "description": "Sent drafts due for a follow-up nudge (already excludes professors who replied).",
            "parameters": {"type": "object", "properties": {"days_since": {"type": "integer"}}}}},
        {"type": "function", "function": {
            "name": "mark_reply",
            "description": "Record a professor's reply on a draft (excludes them from follow-ups). Reversible. outcome: replied | meeting | declined | '' (to clear).",
            "parameters": {"type": "object", "properties": {
                "draft_id": {"type": "integer"}, "outcome": {"type": "string"}},
                "required": ["draft_id", "outcome"]}}},
        {"type": "function", "function": {
            "name": "set_draft_status",
            "description": "Approve or reject a draft (reversible status change; does NOT send). action: approve | reject.",
            "parameters": {"type": "object", "properties": {
                "draft_id": {"type": "integer"}, "action": {"type": "string"}},
                "required": ["draft_id", "action"]}}},
        {"type": "function", "function": {
            "name": "generate_draft",
            "description": "Generate ONE outreach draft for a SAVED professor (get the id from list_faculty first). This spends the user's AI tokens, so the app pauses and asks the user to confirm before it actually runs — you don't need to ask separately, just call it. Optional variant: formal | enthusiastic | concise | research_focused.",
            "parameters": {"type": "object", "properties": {
                "professor_id": {"type": "integer"}, "variant": {"type": "string"}},
                "required": ["professor_id"]}}},
    ]

    # Actions the assistant may request but that must NOT auto-run — they spend
    # tokens, so the user confirms first via /api/chat/confirm.
    _CHAT_CONFIRM_ACTIONS = {"generate_draft"}

    def _run_chat_tool(name: str, args: dict, conn) -> dict:
        """Execute one read-only assistant tool, scoped to *conn*'s workspace."""
        try:
            if name == "search_faculty":
                from app.finder import find_professors
                profs, warns = find_professors(
                    query=str(args.get("query", "")),
                    universities=args.get("universities") or None,
                    max_scholar_results=min(int(args.get("limit", 8) or 8), 15),
                )
                return {"results": [{"name": p.name, "affiliation": p.university, "field": p.field}
                                    for p in profs[:15]], "warnings": warns[:2]}
            if name == "list_faculty":
                profs = get_professors(conn)[:40]
                drafted = set()
                if profs:
                    rows = conn.execute(
                        "SELECT DISTINCT professor_id FROM drafts WHERE workspace_id = ?",
                        (conn.workspace_id,),
                    ).fetchall()
                    drafted = {r["professor_id"] for r in rows}
                return {"faculty": [{"id": p.id, "name": p.name, "university": p.university,
                                     "field": p.field, "has_draft": p.id in drafted}
                                    for p in profs]}
            if name == "outreach_stats":
                return get_outreach_stats(conn)
            if name == "list_drafts":
                ds = get_drafts(conn, status=args.get("status") or None)[:30]
                pmap = get_professors_by_ids(conn, {d.professor_id for d in ds})
                return {"drafts": [{"id": d.id, "professor": (pmap.get(d.professor_id).name if pmap.get(d.professor_id) else "?"),
                                    "status": d.status, "score": round(d.overall_score, 1),
                                    "outcome": d.outcome or "awaiting"} for d in ds]}
            if name == "followups_due":
                from app.delivery import _eligible_followup_drafts
                ds = _eligible_followup_drafts(conn, int(args.get("days_since", 7) or 7), 30)
                pmap = get_professors_by_ids(conn, {d.professor_id for d in ds})
                return {"due": [{"id": d.id, "professor": (pmap.get(d.professor_id).name if pmap.get(d.professor_id) else "?")}
                                for d in ds], "count": len(ds)}
            # --- write tools (reversible only; no spend, no send) ---
            if name == "mark_reply":
                from app.database import VALID_OUTCOMES
                oc = str(args.get("outcome", ""))
                if oc not in VALID_OUTCOMES:
                    return {"error": "outcome must be one of: replied, meeting, declined, or empty"}
                d = get_draft(conn, int(args.get("draft_id", 0) or 0))
                if not d:
                    return {"error": "draft not found"}
                set_draft_outcome(conn, d.id, oc)
                return {"ok": True, "draft_id": d.id, "outcome": oc or "cleared"}
            if name == "set_draft_status":
                act = str(args.get("action", ""))
                if act not in ("approve", "reject"):
                    return {"error": "action must be 'approve' or 'reject'"}
                d = get_draft(conn, int(args.get("draft_id", 0) or 0))
                if not d:
                    return {"error": "draft not found"}
                new_status = "approved" if act == "approve" else "rejected"
                update_draft_status(conn, d.id, new_status)
                return {"ok": True, "draft_id": d.id, "status": new_status}
            if name == "generate_draft":
                # Valid calls are intercepted for user confirmation before they
                # reach here; reaching this branch means the target was invalid.
                pid = int(args.get("professor_id", 0) or 0)
                if not (pid and get_professor(conn, pid)):
                    return {"error": "professor not found — call list_faculty to get a valid saved-professor id"}
                return {"error": "generate_draft needs user confirmation; the app will prompt the user"}
            return {"error": f"unknown tool: {name}"}
        except Exception as exc:
            logger.warning("chat tool %s failed: %s", name, exc)
            return {"error": str(exc)[:200]}

    _VALID_VARIANTS = {"formal", "enthusiastic", "concise", "research_focused"}

    def _as_int(value, default: int = 0) -> int:
        """Coerce model/client-supplied values to int without raising."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _chat_confirm_payload(name: str, args: dict, conn):
        """Build the user-facing confirmation for a gated (token-spending) action.

        Returns ``("confirm", payload)`` when the target is valid and the app
        should pause for the user's explicit OK, or ``("error", {...})`` so the
        model can recover (e.g. by looking up a real professor id).
        """
        if name == "generate_draft":
            pid = _as_int(args.get("professor_id"))
            prof = get_professor(conn, pid) if pid else None
            if not prof:
                return ("error", {"error": "professor not found — call list_faculty to get a valid id"})
            variant = str(args.get("variant", "")).strip().lower()
            if variant not in _VALID_VARIANTS:
                variant = ""
            label = f"Generate a draft to {prof.name}"
            if variant:
                label += f" ({variant} style)"
            label += "?"
            return ("confirm", {
                "action": "generate_draft",
                "args": {"professor_id": prof.id, "variant": variant},
                "label": label,
                "note": "Uses your AI tokens. The draft is saved for your review — nothing is sent.",
                "_default_text": f"I can draft an email to {prof.name}. Generating uses your AI tokens — want me to go ahead?",
            })
        return ("error", {"error": f"unknown gated action: {name}"})

    @app.route("/api/chat", methods=["POST"])
    @login_required
    def chat_api():
        """AI assistant: answer questions + help write/critique outreach emails."""
        data = request.get_json(silent=True) or {}
        message = (data.get("message") or "").strip()
        history = data.get("history") or []
        if not message:
            return jsonify({"success": False, "error": "Empty message"})

        conn = _workspace_conn()
        try:
            cfg = _workspace_config(conn)
        finally:
            conn.close()
        provider = (cfg.llm_provider or "").strip()
        api_key = (cfg.llm_api_key or os.environ.get("LLM_API_KEY", "")).strip()
        if provider != "openrouter" or not api_key:
            # No model configured — tell the client to use its built-in canned help.
            return jsonify({"success": False, "error": "no_ai"})

        if _ai_chats_today() >= _AI_CHAT_DAILY_LIMIT:
            return jsonify({"success": True, "reply": (
                f"You've reached today's assistant limit ({_AI_CHAT_DAILY_LIMIT} messages) for this "
                "workspace — it resets at midnight UTC. You can still use Search, Drafts, and Delivery "
                "in the meantime.")})

        msgs = [{"role": "system", "content": _CHAT_SYSTEM}]
        for h in history[-8:]:
            if not isinstance(h, dict):
                continue
            role = "assistant" if h.get("role") == "bot" else "user"
            content = str(h.get("content", ""))[:2000]
            if content:
                msgs.append({"role": role, "content": content})
        msgs.append({"role": "user", "content": message[:2000]})

        from app.summarizer import chat_with_tools, chat_openrouter
        tool_conn = _workspace_conn()
        try:
            reply = ""
            for _ in range(4):  # cap tool rounds to bound tokens/latency
                try:
                    am = chat_with_tools(api_key, cfg.llm_model, msgs, _CHAT_TOOLS)
                except Exception as exc:
                    logger.warning("AI chat failed: %s", exc)
                    return jsonify({"success": False, "error": "ai_error"})
                tool_calls = am.get("tool_calls") or []
                if not tool_calls:
                    reply = (am.get("content") or "").strip()
                    break
                # Gated actions (token spend) pause for the user's explicit OK
                # instead of running inline.
                gated = next((tc for tc in tool_calls
                              if tc.get("function", {}).get("name") in _CHAT_CONFIRM_ACTIONS), None)
                if gated is not None:
                    gfn = gated.get("function", {})
                    try:
                        gargs = json.loads(gfn.get("arguments") or "{}")
                    except Exception:
                        gargs = {}
                    kind, payload = _chat_confirm_payload(gfn.get("name", ""), gargs, tool_conn)
                    if kind == "confirm":
                        lead = (am.get("content") or "").strip() or payload.get("_default_text", "")
                        payload.pop("_default_text", None)
                        return jsonify({"success": True, "reply": lead, "confirm": payload})
                    # invalid target -> fall through; model gets an error tool result
                msgs.append({"role": "assistant", "content": am.get("content") or "", "tool_calls": tool_calls})
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    try:
                        targs = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        targs = {}
                    result = _run_chat_tool(fn.get("name", ""), targs, tool_conn)
                    msgs.append({"role": "tool", "tool_call_id": tc.get("id"),
                                 "content": json.dumps(result)[:4000]})
            else:
                # Ran out of tool rounds without a text answer — force a summary.
                try:
                    reply = chat_openrouter(api_key, cfg.llm_model, msgs)
                except Exception:
                    reply = ""
        finally:
            tool_conn.close()
        if not reply:
            return jsonify({"success": False, "error": "empty"})

        conn = _auth_conn()
        try:
            ip = request.headers.get("X-Forwarded-For", request.remote_addr)
            if ip and "," in ip:
                ip = ip.split(",")[0].strip()
            conn.execute(
                """INSERT INTO chat_logs
                   (user_key_id, user_label, user_message, bot_response, prompt_key, ip_address, created_at)
                   VALUES (?, ?, ?, ?, 'ai', ?, ?)""",
                (session.get("key_id"), session.get("key_label", ""),
                 message[:2000], reply[:5000], ip, datetime.utcnow().isoformat()),
            )
            conn.commit()
        except Exception as exc:
            logger.warning("chat log failed: %s", exc)
        finally:
            conn.close()
        return jsonify({"success": True, "reply": reply})

    @app.route("/api/chat/confirm", methods=["POST"])
    @login_required
    def chat_confirm_api():
        """Run a gated assistant action after the user explicitly confirmed it in
        the chat widget. Only token-spending generation is gated here — sending
        email is never agent-driven; it stays manual on the Delivery page."""
        data = request.get_json(silent=True) or {}
        action = (data.get("action") or "").strip()
        args = data.get("args") or {}
        if action not in _CHAT_CONFIRM_ACTIONS:
            return jsonify({"success": False, "error": "Unsupported action"}), 400

        conn = _workspace_conn()
        try:
            cfg = _workspace_config(conn)
        finally:
            conn.close()
        provider = (cfg.llm_provider or "").strip()
        api_key = (cfg.llm_api_key or os.environ.get("LLM_API_KEY", "")).strip()
        if provider != "openrouter" or not api_key:
            return jsonify({"success": False, "error": "no_ai"})

        if action == "generate_draft":
            pid = _as_int(args.get("professor_id"))
            variant = str(args.get("variant", "")).strip().lower() or None
            if variant not in (None, "formal", "enthusiastic", "concise", "research_focused"):
                variant = None

            wsconn = _workspace_conn()
            try:
                prof = get_professor(wsconn, pid)
                profiles = get_sender_profiles(wsconn) if prof else []
                over_cap = _drafts_today(wsconn) >= _GENERATION_DAILY_LIMIT
            finally:
                wsconn.close()
            if over_cap:
                return jsonify({"success": True, "reply": (
                    f"You've hit today's generation limit ({_GENERATION_DAILY_LIMIT} drafts) for this "
                    "workspace — it resets at midnight UTC.")})
            if not prof:
                return jsonify({"success": False, "error": "Professor not found."})
            if not profiles:
                return jsonify({"success": True,
                                "reply": "Set up your sender identity first (Setup → sender details), "
                                         "then I can draft for you."})
            sender_profile_id = profiles[-1].id  # most recent sender profile

            try:
                summary = run_generation_pipeline(
                    db_path=cfg.db_path, config=cfg,
                    sender_profile_id=sender_profile_id, professor_ids=[pid],
                    variant=variant, workspace_id=_workspace_id(),
                )
            except Exception as exc:
                logger.warning("agent generate_draft failed: %s", exc)
                return jsonify({"success": False, "error": "generation_failed"})

            new_draft = None
            wsconn = _workspace_conn()
            try:
                row = wsconn.execute(
                    "SELECT id, overall_score FROM drafts WHERE workspace_id = ? AND session_id = ? "
                    "AND professor_id = ? ORDER BY id DESC LIMIT 1",
                    (wsconn.workspace_id, summary.session_id, pid),
                ).fetchone()
                if row:
                    new_draft = (int(row["id"]), float(row["overall_score"] or 0))
            finally:
                wsconn.close()

            _log_activity("draft_generate", category="drafts",
                          target_type="professor", target_id=str(pid),
                          details={"via": "assistant", "session": summary.session_id,
                                   "created": summary.created, "variant": variant or "auto"})

            if not new_draft:
                warn = summary.warnings[0] if summary.warnings else \
                    "the professor may need a richer research summary."
                reply = (f"I couldn't create a draft for {prof.name} — {warn} "
                         "You can try again from the Drafts page.")
                draft_url = None
            else:
                did, score = new_draft
                reply = (f"Drafted an email to {prof.name} (quality {round(score, 1)}/10). "
                         "Review and edit it on the Drafts page — nothing is sent until you "
                         "send it yourself from Delivery.")
                draft_url = url_for("draft_detail", draft_id=did)

            # Record the AI turn in chat_logs for the audit trail.
            try:
                logconn = _auth_conn()
                ip = request.headers.get("X-Forwarded-For", request.remote_addr)
                if ip and "," in ip:
                    ip = ip.split(",")[0].strip()
                logconn.execute(
                    """INSERT INTO chat_logs
                       (user_key_id, user_label, user_message, bot_response, prompt_key, ip_address, created_at)
                       VALUES (?, ?, ?, ?, 'ai', ?, ?)""",
                    (session.get("key_id"), session.get("key_label", ""),
                     f"[confirmed] generate_draft professor_id={pid}", reply[:5000], ip,
                     datetime.utcnow().isoformat()),
                )
                logconn.commit()
                logconn.close()
            except Exception as exc:
                logger.warning("chat confirm log failed: %s", exc)

            payload = {"success": True, "reply": reply}
            if draft_url:
                payload["draft_url"] = draft_url
            return jsonify(payload)

        return jsonify({"success": False, "error": "Unsupported action"}), 400

    @app.route("/api/bug-report", methods=["POST"])
    @login_required
    def bug_report():
        """Submit a bug report."""
        data = request.get_json(silent=True) or {}
        title = data.get("title", "").strip()
        details = data.get("details", "").strip()
        severity = data.get("severity", "medium")

        if not title:
            return jsonify({"success": False, "error": "Title is required"})
        if severity not in ("low", "medium", "high"):
            severity = "medium"

        conn = _auth_conn()
        try:
            ip = request.headers.get("X-Forwarded-For", request.remote_addr)
            if ip and "," in ip:
                ip = ip.split(",")[0].strip()
            cursor = conn.execute(
                """INSERT INTO bug_reports
                   (user_key_id, user_label, title, details, severity, status, ip_address, user_agent, created_at)
                   VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
                (session.get("key_id"), session.get("key_label", ""),
                 title[:500], details[:5000], severity, ip,
                 request.headers.get("User-Agent", "")[:500],
                 datetime.utcnow().isoformat()),
            )
            conn.commit()
            report_id = cursor.lastrowid
            # Log to admin activity
            _log_activity(
                "bug_report_submitted", category="support",
                target_type="bug_report", target_id=str(report_id),
                details={"title": title[:200], "severity": severity},
            )
        finally:
            conn.close()

        return jsonify({"success": True, "report_id": report_id})

    # ------------------------------------------------------------------
    # Admin Hub
    # ------------------------------------------------------------------
    @app.route("/admin")
    @admin_required
    def admin_hub():
        conn = _auth_conn()
        try:
            keys = get_access_keys(conn)
            # Key persists in session until dismissed
            new_key = session.get("last_created_key")
            new_key_label = session.get("last_created_key_label", "")
            stats = get_admin_activity_stats(conn)
            recent_activity = get_admin_activity_log(conn, limit=50)
            _log_activity("admin_view_hub", category="admin", is_admin=True)
            return render_template("admin.html", keys=keys, new_key=new_key,
                                   new_key_label=new_key_label,
                                   stats=stats, recent_activity=recent_activity)
        finally:
            conn.close()

    @app.route("/admin/backup")
    @admin_required
    def admin_backup():
        """Download a full, restorable JSON snapshot of all workspaces' data.

        Password hashes are redacted. Use it as a manual safety net, or hit it
        on a schedule (external cron) to keep off-box backups.
        """
        from app.database import dump_database
        conn = _auth_conn()  # unscoped -> all workspaces
        try:
            dump = dump_database(conn)
        finally:
            conn.close()
        _log_activity("backup_export", category="admin", is_admin=True,
                      details={"row_counts": dump["meta"]["row_counts"]})
        payload = json.dumps(dump, indent=2, default=str)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        return Response(
            payload, mimetype="application/json",
            headers={"Content-Disposition": f"attachment; filename=ao_backup_{ts}.json"},
        )

    @app.route("/admin/bugs")
    @admin_required
    def admin_bugs():
        """Inbox of user-submitted bug reports (read the full details, triage)."""
        from app.database import get_bug_reports, get_bug_report_stats
        status = request.args.get("status") or None
        if status not in (None, "open", "resolved"):
            status = None
        conn = _auth_conn()
        try:
            reports = get_bug_reports(conn, status=status)
            stats = get_bug_report_stats(conn)
        finally:
            conn.close()
        _log_activity("admin_view_bugs", category="admin", is_admin=True)
        return render_template("admin_bugs.html", reports=reports, stats=stats,
                               current_status=status or "")

    @app.route("/admin/bugs/<int:report_id>/status", methods=["POST"])
    @admin_required
    def admin_bug_status(report_id: int):
        from app.database import set_bug_report_status
        new_status = request.form.get("status", "")
        conn = _auth_conn()
        try:
            set_bug_report_status(conn, report_id, new_status)
            _log_activity("bug_report_status", category="support", is_admin=True,
                          target_type="bug_report", target_id=str(report_id),
                          details={"status": new_status})
            flash(f"Bug report #{report_id} marked {new_status}.", "success")
        except ValueError:
            flash("Invalid status.", "error")
        except Exception as exc:
            flash(f"Could not update report: {exc}", "error")
        finally:
            conn.close()
        return redirect(url_for("admin_bugs", status=request.args.get("status") or ""))

    @app.route("/admin/keys/dismiss", methods=["POST"])
    @admin_required
    def admin_dismiss_key():
        session.pop("last_created_key", None)
        session.pop("last_created_key_label", None)
        return jsonify({"success": True})

    @app.route("/admin/activity")
    @admin_required
    def admin_activity():
        conn = _auth_conn()
        try:
            category = request.args.get("category")
            actor = request.args.get("actor")
            action_filter = request.args.get("action")
            limit = min(int(request.args.get("limit", 200)), 1000)

            activities = get_admin_activity_log(
                conn, category=category, actor_label=actor,
                action=action_filter, limit=limit,
            )
            stats = get_admin_activity_stats(conn)

            # Get unique values for filters
            all_categories = conn.execute(
                "SELECT DISTINCT category FROM admin_activity_log ORDER BY category"
            ).fetchall()
            all_actors = conn.execute(
                "SELECT DISTINCT actor_label FROM admin_activity_log WHERE actor_label != '' ORDER BY actor_label"
            ).fetchall()
            all_actions = conn.execute(
                "SELECT DISTINCT action FROM admin_activity_log ORDER BY action"
            ).fetchall()

            return render_template(
                "admin_activity.html",
                activities=activities, stats=stats,
                categories=[r["category"] for r in all_categories],
                actors=[r["actor_label"] for r in all_actors],
                actions=[r["action"] for r in all_actions],
                current_category=category or "",
                current_actor=actor or "",
                current_action=action_filter or "",
            )
        finally:
            conn.close()

    @app.route("/admin/activity/api")
    @admin_required
    def admin_activity_api():
        """JSON API for activity log (for live refresh)."""
        conn = _auth_conn()
        try:
            category = request.args.get("category")
            limit = min(int(request.args.get("limit", 50)), 500)
            activities = get_admin_activity_log(conn, category=category, limit=limit)
            return jsonify({"success": True, "activities": activities})
        finally:
            conn.close()

    @app.route("/admin/logs")
    @admin_required
    def admin_logs():
        """Detailed per-request log (method, path, status, timing, payloads)."""
        conn = _auth_conn()
        try:
            method = request.args.get("method") or None
            path_like = request.args.get("path") or None
            actor = request.args.get("actor") or None
            errors_only = parse_bool(request.args.get("errors_only"), default=False)
            status_class = request.args.get("status_class", type=int)
            limit = min(int(request.args.get("limit", 200)), 1000)
            logs = get_request_logs(
                conn, method=method, status_class=status_class, path_like=path_like,
                actor=actor, errors_only=errors_only, limit=limit,
            )
            methods = [r["method"] for r in conn.execute(
                "SELECT DISTINCT method FROM request_log ORDER BY method").fetchall()]
            actors = [r["actor_label"] for r in conn.execute(
                "SELECT DISTINCT actor_label FROM request_log WHERE actor_label != '' ORDER BY actor_label").fetchall()]
            return render_template(
                "admin_logs.html", logs=logs, methods=methods, actors=actors,
                current_method=method or "", current_path=path_like or "",
                current_actor=actor or "", errors_only=errors_only,
                current_status_class=status_class or "", limit=limit,
            )
        finally:
            conn.close()

    @app.route("/admin/logs/api")
    @admin_required
    def admin_logs_api():
        conn = _auth_conn()
        try:
            limit = min(int(request.args.get("limit", 100)), 1000)
            errors_only = parse_bool(request.args.get("errors_only"), default=False)
            logs = get_request_logs(conn, errors_only=errors_only, limit=limit)
            return jsonify({"success": True, "logs": logs})
        finally:
            conn.close()

    @app.route("/admin/keys/create", methods=["POST"])
    @admin_required
    def admin_create_key():
        conn = _auth_conn()
        try:
            label = request.form.get("label", "").strip() or "Unlabeled"
            role = request.form.get("role", "user")
            if role not in ("user", "admin"):
                role = "user"

            key_value = "ao_" + secrets.token_hex(24)
            create_access_key(conn, key_value, label, role,
                              created_by=session.get("admin_key_label", "admin"))
            _log_activity("admin_create_key", category="admin", is_admin=True,
                          target_type="access_key",
                          details={"label": label, "role": role})
            session["last_created_key"] = key_value
            session["last_created_key_label"] = label
            flash(f"Access key created for '{label}'.", "success")
            return redirect(url_for("admin_hub"))
        except Exception as exc:
            flash(f"Failed to create key: {exc}", "error")
            return redirect(url_for("admin_hub"))
        finally:
            conn.close()

    @app.route("/admin/keys/<int:key_id>/revoke", methods=["POST"])
    @admin_required
    def admin_revoke_key(key_id: int):
        conn = _auth_conn()
        try:
            # Get key info before revoking for logging
            key_info = conn.execute("SELECT label, role FROM access_keys WHERE id = ?", (key_id,)).fetchone()
            revoke_access_key(conn, key_id)
            _log_activity("admin_revoke_key", category="admin", is_admin=True,
                          target_type="access_key", target_id=str(key_id),
                          details={"label": key_info["label"] if key_info else "unknown",
                                   "role": key_info["role"] if key_info else "unknown"})
            flash("Key revoked.", "success")
        except Exception as exc:
            flash(f"Failed to revoke key: {exc}", "error")
        finally:
            conn.close()
        return redirect(url_for("admin_hub"))

    @app.route("/admin/keys/<int:key_id>/delete", methods=["POST"])
    @admin_required
    def admin_delete_key(key_id: int):
        conn = _auth_conn()
        try:
            key_info = conn.execute("SELECT label, role FROM access_keys WHERE id = ?", (key_id,)).fetchone()
            delete_access_key(conn, key_id)
            _log_activity("admin_delete_key", category="admin", is_admin=True,
                          target_type="access_key", target_id=str(key_id),
                          details={"label": key_info["label"] if key_info else "unknown",
                                   "role": key_info["role"] if key_info else "unknown"})
            flash("Key deleted.", "success")
        except Exception as exc:
            flash(f"Failed to delete key: {exc}", "error")
        finally:
            conn.close()
        return redirect(url_for("admin_hub"))

    return app


# ---------------------------------------------------------------------------
# Auto-create default admin key on first run
# ---------------------------------------------------------------------------

def _ensure_default_admin_key(db_path: str) -> None:
    """Create admin key(s) if none exist.

    Priority:
    1. ADMIN_KEY env var — always ensured to exist (idempotent)
    2. Auto-generated random key if no keys exist at all
    """
    try:
        conn = get_connection(db_path)
        try:
            existing = get_access_keys(conn)

            # If ADMIN_KEY env var is set, ensure it exists in DB
            env_admin_key = os.environ.get("ADMIN_KEY", "").strip()
            if env_admin_key:
                # Check if it already exists
                found = any(k["key_value"] == env_admin_key for k in existing)
                if not found:
                    create_access_key(conn, env_admin_key, "Admin (env)", "admin", "system")
                    logger.info("Admin key from ADMIN_KEY env var registered.")

            # If still no keys at all, generate one
            if not existing and not env_admin_key:
                default_key = "ao_admin_" + secrets.token_hex(16)
                create_access_key(conn, default_key, "Default Admin", "admin", "system")
                logger.info("=" * 60)
                logger.info("DEFAULT ADMIN KEY CREATED: %s", default_key)
                logger.info("Save this key — use it to log in and create more keys.")
                logger.info("=" * 60)
        finally:
            conn.close()
    except Exception as exc:
        logger.error("Failed to create default admin key: %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the development server."""
    app = create_app()
    app.run(debug=True, host="127.0.0.1", port=5000)


if __name__ == "__main__":
    main()
