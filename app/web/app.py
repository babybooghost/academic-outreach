"""Flask web UI for the Academic Outreach Email System.

Provides a review interface with access-key authentication,
admin hub, and dark-themed dashboard.
"""

from __future__ import annotations

import json
import os
import secrets
import traceback
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Optional

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import generate_password_hash

from app.config import load_config
from app.database import (
    create_access_key,
    delete_access_key,
    get_access_keys,
    get_admin_activity_log,
    get_admin_activity_stats,
    get_all_settings,
    get_connection,
    get_draft,
    get_drafts,
    get_professor,
    get_professors,
    get_professors_by_ids,
    get_sender_profiles,
    get_setting,
    get_suppression_list,
    init_db,
    insert_sender_profile,
    log_admin_activity,
    revoke_access_key,
    set_settings_bulk,
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
    seed_person_workspace_identity,
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

    # Context processor
    @app.context_processor
    def inject_globals() -> dict[str, Any]:
        return {
            "app_version": _APP_VERSION,
            "now": datetime.utcnow(),
            "storage_status": _storage_status(),
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
    # Error handler
    # ------------------------------------------------------------------
    @app.errorhandler(Exception)
    def handle_exception(e):
        import traceback
        tb = traceback.format_exc()
        logger.error("Unhandled exception: %s\n%s", e, tb)
        # Never leak stack traces to users. Show the full traceback only when
        # explicitly debugging locally.
        if app.debug or app.config.get("SHOW_TRACEBACKS"):
            return (
                "<h1>Error</h1>"
                f"<pre style='white-space:pre-wrap; background:#111; color:#e44; padding:1rem;'>{tb}</pre>"
            ), 500
        return render_template("error.html"), 500

    # ------------------------------------------------------------------
    # Homepage (public landing page)
    # ------------------------------------------------------------------
    @app.route("/")
    def homepage():
        if session.get("authenticated"):
            return redirect(url_for("dashboard"))
        return render_template("homepage.html")

    # ------------------------------------------------------------------
    # Auth routes
    # ------------------------------------------------------------------
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

        # Optional invite gate so only people with the shared code can register
        # (signups consume the workspace owner's LLM API key).
        required_invite = os.environ.get("SIGNUP_INVITE_CODE", "").strip()

        if request.method == "POST":
            email = request.form.get("email", "").strip()
            display_name = request.form.get("display_name", "").strip()
            password = request.form.get("password", "")
            password_confirm = request.form.get("password_confirm", "")
            invite_code = request.form.get("invite_code", "").strip()

            if required_invite and not secrets.compare_digest(invite_code, required_invite):
                error = "That invite code is not valid. Ask the workspace owner for the current code."
                _log_activity(
                    "signup_invalid_invite", category="auth",
                    details={"email": email[:80]},
                )
            elif not email or not display_name:
                error = "Email and display name are required."
            elif len(password) < 6:
                error = "Password must be at least 6 characters."
            elif password != password_confirm:
                error = "Passwords do not match."
            else:
                conn = _auth_conn()
                try:
                    # Check if email already registered
                    existing = conn.execute(
                        "SELECT id FROM user_signups WHERE email = ?", (email,)
                    ).fetchone()
                    if existing:
                        error = "This email is already registered. Use your existing key to log in."
                    else:
                        # Salted, slow password hash. pbkdf2:sha256 is portable
                        # (Werkzeug's newer scrypt default needs OpenSSL 1.1+,
                        # which some runtimes lack). Replaces the old SHA-256.
                        pw_hash = generate_password_hash(
                            password, method="pbkdf2:sha256"
                        )

                        # Generate access key
                        key_value = "ao_" + secrets.token_hex(24)

                        # Store signup record
                        conn.execute(
                            """INSERT INTO user_signups
                               (email, display_name, password_hash, key_value, created_at)
                               VALUES (?, ?, ?, ?, ?)""",
                            (email, display_name, pw_hash, key_value,
                             datetime.utcnow().isoformat()),
                        )
                        conn.commit()

                        # Create the actual access key
                        key_id = create_access_key(
                            conn, key_value, display_name, "user",
                            created_by=f"signup:{email}",
                        )
                        _seed_workspace_identity(
                            key_id,
                            email=email,
                            display_name=display_name,
                        )

                        # Log signup in admin activity
                        ip = request.headers.get("X-Forwarded-For", request.remote_addr)
                        if ip and "," in ip:
                            ip = ip.split(",")[0].strip()
                        log_admin_activity(
                            conn,
                            actor_key_id=None,
                            actor_label=display_name,
                            actor_role="user",
                            action="user_signup",
                            category="auth",
                            target_type="access_key",
                            target_id=None,
                            details={
                                "email": email,
                                "display_name": display_name,
                            },
                            ip_address=ip,
                            user_agent=request.headers.get("User-Agent", "")[:500],
                            request_method=request.method,
                            request_path=request.path,
                            session_id=None,
                            response_code=None,
                        )

                        access_key = key_value
                        logger.info("New signup: %s (%s)", display_name, email)
                except Exception as exc:
                    logger.exception("Signup failed")
                    error = f"Signup failed: {exc}"
                finally:
                    conn.close()

        return render_template(
            "signup.html", error=error, access_key=access_key,
            invite_required=bool(required_invite),
        )

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
    @app.route("/dashboard")
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

            return render_template(
                "dashboard.html",
                prof_counts=prof_counts,
                total_professors=total_professors,
                draft_counts=draft_counts,
                total_drafts=total_drafts,
                recent_sends=[dict(r) for r in recent_sends],
            )
        finally:
            workspace_conn.close()

    # ------------------------------------------------------------------
    # Professors
    # ------------------------------------------------------------------
    @app.route("/professors")
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
            cfg = _workspace_config(conn)
        finally:
            conn.close()

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
            cfg = _workspace_config(conn)
        finally:
            conn.close()

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
            notes = None
            if request.is_json:
                notes = request.json.get("notes")
            update_draft_status(conn, draft_id, "rejected", notes=notes)
            _log_activity("draft_reject", category="drafts",
                          target_type="draft", target_id=str(draft_id),
                          details={"notes": notes} if notes else None)
            return jsonify({"success": True, "status": "rejected"})
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
    @app.route("/send")
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
                status_code = 200 if result.get("success") or result.get("results") else 400
                return jsonify(result), status_code
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
            sent_at = sent_map.get(d.id) or d.reviewed_at or d.created_at
            if sent_at and sent_at <= cutoff:
                eligible.append(d)
        return followups, eligible

    @app.route("/followups")
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

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    _LLM_MODELS: dict[str, str] = {
        "google/gemini-2.5-flash": "Gemini 2.5 Flash",
        "google/gemini-2.5-pro": "Gemini 2.5 Pro",
        "anthropic/claude-haiku-4.5": "Claude Haiku 4.5",
        "anthropic/claude-sonnet-4.6": "Claude Sonnet 4.6",
        "anthropic/claude-opus-4.6": "Claude Opus 4.6",
    }

    _EMAIL_PROVIDERS: dict[str, str] = {
        "gmail": "Gmail",
        "outlook": "Outlook",
        "hotmail": "Hotmail",
    }

    @app.route("/settings")
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
        filename = os.path.basename(upload.filename)[:160]
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

            from app.summarizer import LLMSummarizer
            client = LLMSummarizer(provider=provider, api_key=api_key, model=model)
            reply = client._call_llm(
                "Reply with exactly the word: ready"
            )
            ok = "ready" in (reply or "").lower()
            _log_activity(
                "ai_test", category="settings",
                details={"provider": provider, "model": model, "ok": ok},
            )
            if ok or reply:
                flash(f"AI is working — {model} responded successfully.", "success")
            else:
                flash(f"AI call to {model} returned an empty response.", "error")
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
            )
            if not profile.name or not profile.school or not profile.grade or not profile.email:
                flash("Name, school, grade, and email are required for a sender profile.", "error")
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
            flash(f"Failed to create sender profile: {exc}", "error")
            return redirect(url_for("settings_page"))
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Professor Finder
    # ------------------------------------------------------------------

    @app.route("/finder")
    @login_required
    def finder_page():
        from app.finder import list_known_universities
        universities = list_known_universities()
        return render_template("finder.html", universities=universities)

    @app.route("/finder/search", methods=["POST"])
    @login_required
    def finder_search():
        from app.finder import find_professors
        data = request.get_json() or {}
        query = data.get("scholar_query", "").strip()
        universities = data.get("universities", [])
        field = data.get("field", "").strip()
        max_results = min(int(data.get("max_results", 25)), 50)

        if not query:
            return jsonify({"success": False, "error": "Enter a search query (e.g. 'blockchain fintech AI')."})

        try:
            professors, warnings = find_professors(
                query=query,
                universities=universities if universities else None,
                field=field,
                max_scholar_results=max_results,
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
        data = request.get_json() or {}
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
