"""Flask web UI for the Academic Outreach Email System.

Provides a review interface with access-key authentication,
admin hub, and dark-themed dashboard.
"""

from __future__ import annotations

import hashlib
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
    get_sender_profiles,
    get_setting,
    get_suppression_list,
    init_db,
    log_admin_activity,
    revoke_access_key,
    set_settings_bulk,
    update_draft_status,
    upsert_professor,
    validate_access_key,
)
from app.logger import get_logger
from app.models import Draft, Professor, SenderProfile

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

logger = get_logger(__name__)

_APP_VERSION = "1.0.0"


def create_app() -> Flask:
    """Create and configure the Flask application."""
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).resolve().parent / "templates"),
        static_folder=str(Path(__file__).resolve().parent / "static"),
    )
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "outreach-local-dev-key")

    # Load config once and store on app
    try:
        cfg = load_config()
    except Exception:
        cfg = None
    app.config["APP_CFG"] = cfg
    app.config["APP_VERSION"] = _APP_VERSION

    # Admin password from env (for initial admin creation)
    app.config["ADMIN_PASSWORD"] = os.environ.get("ADMIN_PASSWORD", "")

    # Ensure database exists
    if cfg:
        init_db(cfg.db_path)
        # Auto-create default admin key if none exist
        _ensure_default_admin_key(cfg.db_path)

    # Context processor
    @app.context_processor
    def inject_globals() -> dict[str, Any]:
        return {"app_version": _APP_VERSION, "now": datetime.utcnow()}

    # ------------------------------------------------------------------
    # DB helper
    # ------------------------------------------------------------------
    def _conn():
        c = app.config.get("APP_CFG")
        if c is None:
            raise RuntimeError("Application config not loaded")
        return get_connection(c.db_path)

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
            conn = _conn()
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
    # Error handler
    # ------------------------------------------------------------------
    @app.errorhandler(Exception)
    def handle_exception(e):
        import traceback
        tb = traceback.format_exc()
        logger.error("Unhandled exception: %s\n%s", e, tb)
        return (
            f"<h1>Error</h1><pre style='white-space:pre-wrap; background:#111; color:#e44; padding:1rem;'>{tb}</pre>"
        ), 500

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
                conn = _conn()
                try:
                    key_data = validate_access_key(conn, key_value)
                    if key_data:
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

        if request.method == "POST":
            email = request.form.get("email", "").strip()
            display_name = request.form.get("display_name", "").strip()
            password = request.form.get("password", "")
            password_confirm = request.form.get("password_confirm", "")

            if not email or not display_name:
                error = "Email and display name are required."
            elif len(password) < 6:
                error = "Password must be at least 6 characters."
            elif password != password_confirm:
                error = "Passwords do not match."
            else:
                conn = _conn()
                try:
                    # Check if email already registered
                    existing = conn.execute(
                        "SELECT id FROM user_signups WHERE email = ?", (email,)
                    ).fetchone()
                    if existing:
                        error = "This email is already registered. Use your existing key to log in."
                    else:
                        # Hash password
                        pw_hash = hashlib.sha256(
                            (password + app.secret_key).encode()
                        ).hexdigest()

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
                        create_access_key(
                            conn, key_value, display_name, "user",
                            created_by=f"signup:{email}",
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

        return render_template("signup.html", error=error, access_key=access_key)

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
                conn = _conn()
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
        return jsonify({
            "status": "ok",
            "config_loaded": cfg is not None,
            "db_path": cfg.db_path if cfg else None,
        })

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------
    @app.route("/dashboard")
    @login_required
    def dashboard():
        conn = _conn()
        try:
            prof_rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM professors GROUP BY status"
            ).fetchall()
            prof_counts: dict[str, int] = {r["status"]: r["cnt"] for r in prof_rows}
            total_professors = sum(prof_counts.values())

            draft_rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM drafts GROUP BY status"
            ).fetchall()
            draft_counts: dict[str, int] = {r["status"]: r["cnt"] for r in draft_rows}
            total_drafts = sum(draft_counts.values())

            recent_sends = conn.execute(
                """SELECT sl.*, p.name as professor_name
                   FROM send_log sl
                   JOIN professors p ON sl.professor_id = p.id
                   ORDER BY sl.sent_at DESC LIMIT 10"""
            ).fetchall()

            recent_activity = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT 15"
            ).fetchall()

            return render_template(
                "dashboard.html",
                prof_counts=prof_counts,
                total_professors=total_professors,
                draft_counts=draft_counts,
                total_drafts=total_drafts,
                recent_sends=[dict(r) for r in recent_sends],
                recent_activity=[dict(r) for r in recent_activity],
            )
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Professors
    # ------------------------------------------------------------------
    @app.route("/professors")
    @login_required
    def professors_list():
        conn = _conn()
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

    @app.route("/professors/<int:prof_id>")
    @login_required
    def professor_detail(prof_id: int):
        conn = _conn()
        try:
            prof = get_professor(conn, prof_id)
            if prof is None:
                flash("Professor not found.", "error")
                return redirect(url_for("professors_list"))

            drafts = conn.execute(
                "SELECT * FROM drafts WHERE professor_id = ? ORDER BY id DESC",
                (prof_id,),
            ).fetchall()
            draft_objs = [Draft.from_row(r) for r in drafts]

            return render_template("professor_detail.html", professor=prof, drafts=draft_objs)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Drafts
    # ------------------------------------------------------------------
    @app.route("/drafts")
    @login_required
    def drafts_list():
        conn = _conn()
        try:
            session_filter = request.args.get("session", type=int)
            status_filter = request.args.get("status")

            drafts = get_drafts(conn, session_id=session_filter,
                                status=status_filter if status_filter else None)

            prof_ids = {d.professor_id for d in drafts}
            prof_map: dict[int, str] = {}
            for pid in prof_ids:
                p = get_professor(conn, pid)
                if p:
                    prof_map[pid] = p.name

            all_drafts = get_drafts(conn)
            sessions = sorted({d.session_id for d in all_drafts})
            statuses = sorted({d.status for d in all_drafts})

            return render_template(
                "drafts.html", drafts=drafts, prof_map=prof_map,
                sessions=sessions, statuses=statuses,
                current_session=session_filter or "",
                current_status=status_filter or "",
            )
        finally:
            conn.close()

    @app.route("/drafts/<int:draft_id>")
    @login_required
    def draft_detail(draft_id: int):
        conn = _conn()
        try:
            draft = get_draft(conn, draft_id)
            if draft is None:
                flash("Draft not found.", "error")
                return redirect(url_for("drafts_list"))
            prof = get_professor(conn, draft.professor_id)
            return render_template("draft_detail.html", draft=draft, professor=prof)
        finally:
            conn.close()

    @app.route("/drafts/<int:draft_id>/approve", methods=["POST"])
    @login_required
    def approve_draft_route(draft_id: int):
        conn = _conn()
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
        conn = _conn()
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

    @app.route("/drafts/<int:draft_id>/edit", methods=["POST"])
    @login_required
    def edit_draft_route(draft_id: int):
        conn = _conn()
        try:
            draft = get_draft(conn, draft_id)
            if draft is None:
                return jsonify({"error": "Draft not found"}), 404
            data = request.get_json(silent=True) or {}
            new_body = data.get("body")
            new_subject = data.get("subject")
            if new_body is not None:
                conn.execute("UPDATE drafts SET body = ? WHERE id = ?", (new_body, draft_id))
            if new_subject is not None:
                subjects = draft.subject_lines_list
                if subjects:
                    subjects[0] = new_subject
                else:
                    subjects = [new_subject]
                conn.execute("UPDATE drafts SET subject_lines = ? WHERE id = ?",
                             (json.dumps(subjects), draft_id))
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
        conn = _conn()
        try:
            approved = get_drafts(conn, status="approved")
            edited = get_drafts(conn, status="edited")
            send_queue = approved + edited

            prof_map: dict[int, Professor] = {}
            for d in send_queue:
                if d.professor_id not in prof_map:
                    p = get_professor(conn, d.professor_id)
                    if p:
                        prof_map[d.professor_id] = p

            return render_template("send.html", send_queue=send_queue, prof_map=prof_map)
        finally:
            conn.close()

    @app.route("/send", methods=["POST"])
    @login_required
    def send_trigger():
        conn = _conn()
        try:
            data = request.get_json(silent=True) or {}
            dry_run = data.get("dry_run", True)
            method = data.get("method", "gmail_draft")
            limit = data.get("limit", 10)

            approved = get_drafts(conn, status="approved")
            edited = get_drafts(conn, status="edited")
            send_queue = (approved + edited)[:limit]

            if dry_run:
                results = []
                for d in send_queue:
                    p = get_professor(conn, d.professor_id)
                    results.append({
                        "draft_id": d.id,
                        "professor": p.name if p else "Unknown",
                        "email": p.email if p else "Unknown",
                        "subject": d.subject_lines_list[0] if d.subject_lines_list else "(no subject)",
                        "status": "dry_run",
                    })
                _log_activity("send_dry_run", category="send",
                              details={"count": len(results), "method": method})
                return jsonify({"success": True, "dry_run": True, "count": len(results), "results": results})

            try:
                from app.sender import SafeSender
                cfg = app.config["APP_CFG"]
                sender = SafeSender(cfg)
                results = []
                for d in send_queue:
                    p = get_professor(conn, d.professor_id)
                    if not p:
                        continue
                    try:
                        sender.send(draft=d, professor=p, method=method, conn=conn)
                        results.append({"draft_id": d.id, "professor": p.name, "status": "sent"})
                    except Exception as send_exc:
                        results.append({"draft_id": d.id, "professor": p.name, "status": "failed", "error": str(send_exc)})
                _log_activity("send_execute", category="send",
                              details={"count": len(results), "method": method,
                                       "sent": sum(1 for r in results if r["status"] == "sent"),
                                       "failed": sum(1 for r in results if r["status"] == "failed")})
                return jsonify({"success": True, "dry_run": False, "count": len(results), "results": results})
            except ImportError:
                return jsonify({"error": "Sender module not available"}), 500
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
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
        conn = _conn()
        try:
            cfg = app.config["APP_CFG"]
            if not cfg:
                return jsonify({"error": "Config not loaded"}), 500

            output_dir = Path(cfg.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            filename = f"drafts_export_{timestamp}.csv"
            filepath = output_dir / filename

            try:
                from app.storage import export_drafts_csv
                export_drafts_csv(conn, str(filepath))
            except ImportError:
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
        cfg = app.config.get("APP_CFG")
        if not cfg:
            flash("Config not loaded.", "error")
            return redirect(url_for("export_page"))
        filepath = Path(cfg.output_dir) / filename
        if not filepath.exists():
            flash("File not found.", "error")
            return redirect(url_for("export_page"))
        return send_file(str(filepath), as_attachment=True)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    _LLM_MODELS: dict[str, str] = {
        "google/gemini-2.5-flash-preview": "Gemini 2.5 Flash",
        "google/gemini-2.5-pro-preview": "Gemini 2.5 Pro",
        "anthropic/claude-haiku-4-5-20251001": "Claude Haiku",
        "anthropic/claude-sonnet-4-6": "Claude Sonnet",
        "anthropic/claude-opus-4-6": "Claude Opus",
    }

    _EMAIL_PROVIDERS: dict[str, str] = {
        "gmail": "Gmail",
        "outlook": "Outlook",
        "hotmail": "Hotmail",
    }

    @app.route("/settings")
    @login_required
    def settings_page():
        conn = _conn()
        try:
            cfg = app.config.get("APP_CFG")
            profiles = get_sender_profiles(conn)
            suppression = get_suppression_list(conn)
            saved = get_all_settings(conn)

            effective: dict[str, Any] = {
                "sender_email": saved.get("sender_email", os.environ.get("SENDER_EMAIL", cfg.sender_email if cfg else "")),
                "llm_provider": saved.get("llm_provider", os.environ.get("LLM_PROVIDER", cfg.llm_provider if cfg else "")),
                "llm_api_key_set": bool(os.environ.get("LLM_API_KEY", cfg.llm_api_key if cfg else "")),
                "llm_model": saved.get("llm_model", os.environ.get("LLM_MODEL", cfg.llm_model if cfg else "google/gemini-2.5-flash-preview")),
                "email_provider": saved.get("email_provider", os.environ.get("EMAIL_PROVIDER", cfg.email_provider if cfg else "gmail")),
                "smtp_user": saved.get("smtp_user", os.environ.get("SMTP_USER", cfg.smtp_user if cfg else "")),
                "smtp_password": saved.get("smtp_password", os.environ.get("SMTP_PASSWORD", cfg.smtp_password if cfg else "")),
            }

            return render_template(
                "settings.html", effective=effective,
                llm_models=_LLM_MODELS, email_providers=_EMAIL_PROVIDERS,
                profiles=profiles, suppression=suppression,
            )
        finally:
            conn.close()

    @app.route("/settings", methods=["POST"])
    @login_required
    def settings_save():
        conn = _conn()
        try:
            new_settings: dict[str, str] = {}
            for key in ("sender_email", "llm_provider", "llm_model",
                        "email_provider", "smtp_user", "smtp_password"):
                val = request.form.get(key, "").strip()
                new_settings[key] = val

            set_settings_bulk(conn, new_settings)

            for key, val in new_settings.items():
                if val:
                    os.environ[key.upper()] = val

            try:
                app.config["APP_CFG"] = load_config()
            except Exception:
                pass

            _log_activity("settings_update", category="settings",
                          details={k: v for k, v in new_settings.items() if k != "smtp_password"})
            flash("Settings saved successfully.", "success")
            return redirect(url_for("settings_page"))
        except Exception as exc:
            flash(f"Failed to save settings: {exc}", "error")
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

    @app.route("/finder/save", methods=["POST"])
    @login_required
    def finder_save():
        from app.database import upsert_professor
        data = request.get_json() or {}
        professors_data = data.get("professors", [])

        if not professors_data:
            return jsonify({"success": False, "error": "No professors to save."})

        conn = _conn()
        saved = 0
        errors = 0
        try:
            for pd in professors_data:
                try:
                    prof = Professor(
                        name=pd.get("name", ""),
                        title=pd.get("title"),
                        email=pd.get("email", ""),
                        university=pd.get("university", ""),
                        department=pd.get("department", "Computer Science"),
                        field=pd.get("field", ""),
                        profile_url=pd.get("profile_url"),
                        research_summary=pd.get("research_summary"),
                        notes=pd.get("notes"),
                        status="new",
                    )
                    upsert_professor(conn, prof)
                    saved += 1
                except Exception as exc:
                    errors += 1
                    logger.warning("Failed to save professor %s: %s", pd.get("name"), exc)
        finally:
            conn.close()

        _log_activity("finder_save_professors", category="finder",
                      details={"saved": saved, "errors": errors,
                               "names": [p.get("name") for p in professors_data[:10]]})
        return jsonify({
            "success": True,
            "saved": saved,
            "errors": errors,
        })

    # ------------------------------------------------------------------
    # Admin Hub
    # ------------------------------------------------------------------
    @app.route("/admin")
    @admin_required
    def admin_hub():
        conn = _conn()
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
        conn = _conn()
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
        conn = _conn()
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
        conn = _conn()
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
        conn = _conn()
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
        conn = _conn()
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
