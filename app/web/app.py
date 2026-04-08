"""Flask web UI for the Academic Outreach Email System.

Provides a local review interface for managing professors, drafts,
sending, and exports.
"""

from __future__ import annotations

import json
import os
import traceback
from datetime import datetime
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
    url_for,
)

from app.config import load_config
from app.database import (
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
    set_settings_bulk,
    update_draft_status,
)
from app.logger import get_logger
from app.models import Draft, Professor, SenderProfile

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

logger = get_logger(__name__)

_APP_VERSION = "0.1.0"


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
        # Fallback: let routes fail gracefully
        cfg = None
    app.config["APP_CFG"] = cfg
    app.config["APP_VERSION"] = _APP_VERSION

    # Ensure database exists
    if cfg:
        init_db(cfg.db_path)

    # Register context processor for templates
    @app.context_processor
    def inject_globals() -> dict[str, Any]:
        return {"app_version": _APP_VERSION, "now": datetime.utcnow()}

    # ------------------------------------------------------------------
    # Helper to get a DB connection
    # ------------------------------------------------------------------
    def _conn():
        """Return a database connection using the app config."""
        c = app.config.get("APP_CFG")
        if c is None:
            raise RuntimeError("Application config not loaded")
        return get_connection(c.db_path)

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @app.route("/")
    def dashboard():
        """Dashboard with summary stats."""
        conn = _conn()
        try:
            # Professor counts by status
            prof_rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM professors GROUP BY status"
            ).fetchall()
            prof_counts: dict[str, int] = {r["status"]: r["cnt"] for r in prof_rows}
            total_professors = sum(prof_counts.values())

            # Draft counts by status
            draft_rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM drafts GROUP BY status"
            ).fetchall()
            draft_counts: dict[str, int] = {r["status"]: r["cnt"] for r in draft_rows}
            total_drafts = sum(draft_counts.values())

            # Recent sends
            recent_sends = conn.execute(
                """SELECT sl.*, p.name as professor_name
                   FROM send_log sl
                   JOIN professors p ON sl.professor_id = p.id
                   ORDER BY sl.sent_at DESC LIMIT 10"""
            ).fetchall()

            # Recent audit entries
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

    @app.route("/professors")
    def professors_list():
        """List all professors with optional filters."""
        conn = _conn()
        try:
            status_filter = request.args.get("status")
            field_filter = request.args.get("field")
            profs = get_professors(
                conn,
                status=status_filter if status_filter else None,
                field=field_filter if field_filter else None,
            )

            # Gather unique fields and statuses for filter dropdowns
            all_profs = get_professors(conn)
            fields = sorted({p.field for p in all_profs if p.field})
            statuses = sorted({p.status for p in all_profs if p.status})

            return render_template(
                "professors.html",
                professors=profs,
                fields=fields,
                statuses=statuses,
                current_status=status_filter or "",
                current_field=field_filter or "",
            )
        finally:
            conn.close()

    @app.route("/professors/<int:prof_id>")
    def professor_detail(prof_id: int):
        """Professor detail page with associated drafts."""
        conn = _conn()
        try:
            prof = get_professor(conn, prof_id)
            if prof is None:
                flash("Professor not found.", "error")
                return redirect(url_for("professors_list"))

            # Fetch drafts for this professor
            drafts = conn.execute(
                "SELECT * FROM drafts WHERE professor_id = ? ORDER BY id DESC",
                (prof_id,),
            ).fetchall()
            draft_objs = [Draft.from_row(r) for r in drafts]

            return render_template(
                "professor_detail.html",
                professor=prof,
                drafts=draft_objs,
            )
        finally:
            conn.close()

    @app.route("/drafts")
    def drafts_list():
        """List all drafts with optional filters."""
        conn = _conn()
        try:
            session_filter = request.args.get("session", type=int)
            status_filter = request.args.get("status")

            drafts = get_drafts(
                conn,
                session_id=session_filter,
                status=status_filter if status_filter else None,
            )

            # Build a professor name lookup
            prof_ids = {d.professor_id for d in drafts}
            prof_map: dict[int, str] = {}
            for pid in prof_ids:
                p = get_professor(conn, pid)
                if p:
                    prof_map[pid] = p.name

            # Gather unique sessions and statuses for filters
            all_drafts = get_drafts(conn)
            sessions = sorted({d.session_id for d in all_drafts})
            statuses = sorted({d.status for d in all_drafts})

            return render_template(
                "drafts.html",
                drafts=drafts,
                prof_map=prof_map,
                sessions=sessions,
                statuses=statuses,
                current_session=session_filter or "",
                current_status=status_filter or "",
            )
        finally:
            conn.close()

    @app.route("/drafts/<int:draft_id>")
    def draft_detail(draft_id: int):
        """Full draft detail view."""
        conn = _conn()
        try:
            draft = get_draft(conn, draft_id)
            if draft is None:
                flash("Draft not found.", "error")
                return redirect(url_for("drafts_list"))

            prof = get_professor(conn, draft.professor_id)

            return render_template(
                "draft_detail.html",
                draft=draft,
                professor=prof,
            )
        finally:
            conn.close()

    @app.route("/drafts/<int:draft_id>/approve", methods=["POST"])
    def approve_draft_route(draft_id: int):
        """Approve a draft (AJAX)."""
        conn = _conn()
        try:
            draft = get_draft(conn, draft_id)
            if draft is None:
                return jsonify({"error": "Draft not found"}), 404
            update_draft_status(conn, draft_id, "approved")
            return jsonify({"success": True, "status": "approved"})
        except Exception as exc:
            logger.error("Error approving draft %d: %s", draft_id, exc)
            return jsonify({"error": str(exc)}), 500
        finally:
            conn.close()

    @app.route("/drafts/<int:draft_id>/reject", methods=["POST"])
    def reject_draft_route(draft_id: int):
        """Reject a draft (AJAX)."""
        conn = _conn()
        try:
            draft = get_draft(conn, draft_id)
            if draft is None:
                return jsonify({"error": "Draft not found"}), 404
            notes = None
            if request.is_json:
                notes = request.json.get("notes")
            update_draft_status(conn, draft_id, "rejected", notes=notes)
            return jsonify({"success": True, "status": "rejected"})
        except Exception as exc:
            logger.error("Error rejecting draft %d: %s", draft_id, exc)
            return jsonify({"error": str(exc)}), 500
        finally:
            conn.close()

    @app.route("/drafts/<int:draft_id>/edit", methods=["POST"])
    def edit_draft_route(draft_id: int):
        """Edit draft body/subject (AJAX, accepts JSON)."""
        conn = _conn()
        try:
            draft = get_draft(conn, draft_id)
            if draft is None:
                return jsonify({"error": "Draft not found"}), 404

            data = request.get_json(silent=True) or {}
            new_body = data.get("body")
            new_subject = data.get("subject")

            if new_body is not None:
                conn.execute(
                    "UPDATE drafts SET body = ? WHERE id = ?",
                    (new_body, draft_id),
                )
            if new_subject is not None:
                # Replace the first subject line
                subjects = draft.subject_lines_list
                if subjects:
                    subjects[0] = new_subject
                else:
                    subjects = [new_subject]
                conn.execute(
                    "UPDATE drafts SET subject_lines = ? WHERE id = ?",
                    (json.dumps(subjects), draft_id),
                )
            update_draft_status(conn, draft_id, "edited")
            return jsonify({"success": True, "status": "edited"})
        except Exception as exc:
            logger.error("Error editing draft %d: %s", draft_id, exc)
            return jsonify({"error": str(exc)}), 500
        finally:
            conn.close()

    @app.route("/send")
    def send_page():
        """Send queue: list approved drafts ready to send."""
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

            return render_template(
                "send.html",
                send_queue=send_queue,
                prof_map=prof_map,
            )
        finally:
            conn.close()

    @app.route("/send", methods=["POST"])
    def send_trigger():
        """Trigger sending approved drafts."""
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
                return jsonify({
                    "success": True,
                    "dry_run": True,
                    "count": len(results),
                    "results": results,
                })

            # Actual sending via SafeSender
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
                        result = sender.send(
                            draft=d,
                            professor=p,
                            method=method,
                            conn=conn,
                        )
                        results.append({
                            "draft_id": d.id,
                            "professor": p.name,
                            "status": "sent",
                        })
                    except Exception as send_exc:
                        results.append({
                            "draft_id": d.id,
                            "professor": p.name,
                            "status": "failed",
                            "error": str(send_exc),
                        })

                return jsonify({
                    "success": True,
                    "dry_run": False,
                    "count": len(results),
                    "results": results,
                })
            except ImportError:
                return jsonify({
                    "error": "Sender module not available",
                }), 500
        except Exception as exc:
            logger.error("Send trigger failed: %s", exc)
            return jsonify({"error": str(exc)}), 500
        finally:
            conn.close()

    @app.route("/export")
    def export_page():
        """Export page with format options."""
        return render_template("export.html")

    @app.route("/export", methods=["POST"])
    def export_trigger():
        """Trigger CSV export and return download link."""
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
                # Fallback: manual CSV export
                import csv

                drafts = get_drafts(conn)
                with open(filepath, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        "id", "professor_id", "session_id", "subject",
                        "body", "overall_score", "status", "warnings",
                        "created_at",
                    ])
                    for d in drafts:
                        subj = d.subject_lines_list[0] if d.subject_lines_list else ""
                        writer.writerow([
                            d.id, d.professor_id, d.session_id, subj,
                            d.body, d.overall_score, d.status,
                            ", ".join(d.warnings_list), d.created_at,
                        ])

            return jsonify({
                "success": True,
                "filename": filename,
                "download_url": url_for("download_export", filename=filename),
            })
        except Exception as exc:
            logger.error("Export failed: %s", exc)
            return jsonify({"error": str(exc)}), 500
        finally:
            conn.close()

    @app.route("/export/download/<filename>")
    def download_export(filename: str):
        """Serve an exported file for download."""
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
    # Available LLM models (for dropdown)
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
    def settings_page():
        """Show current config and sender profiles — everything editable."""
        conn = _conn()
        try:
            cfg = app.config.get("APP_CFG")
            profiles = get_sender_profiles(conn)
            suppression = get_suppression_list(conn)

            # Load saved settings from DB (fall back to env vars / config)
            saved = get_all_settings(conn)

            # Current effective values: DB setting → env var → config default
            effective: dict[str, str] = {
                "sender_email": saved.get("sender_email", os.environ.get("SENDER_EMAIL", cfg.sender_email if cfg else "")),
                "llm_provider": saved.get("llm_provider", os.environ.get("LLM_PROVIDER", cfg.llm_provider if cfg else "")),
                "llm_api_key_set": bool(os.environ.get("LLM_API_KEY", cfg.llm_api_key if cfg else "")),
                "llm_model": saved.get("llm_model", os.environ.get("LLM_MODEL", cfg.llm_model if cfg else "google/gemini-2.5-flash-preview")),
                "email_provider": saved.get("email_provider", os.environ.get("EMAIL_PROVIDER", cfg.email_provider if cfg else "gmail")),
                "smtp_user": saved.get("smtp_user", os.environ.get("SMTP_USER", cfg.smtp_user if cfg else "")),
                "smtp_password": saved.get("smtp_password", os.environ.get("SMTP_PASSWORD", cfg.smtp_password if cfg else "")),
            }

            return render_template(
                "settings.html",
                effective=effective,
                llm_models=_LLM_MODELS,
                email_providers=_EMAIL_PROVIDERS,
                profiles=profiles,
                suppression=suppression,
            )
        finally:
            conn.close()

    @app.route("/settings", methods=["POST"])
    def settings_save():
        """Save runtime settings to the database."""
        conn = _conn()
        try:
            # Collect form fields
            new_settings: dict[str, str] = {}
            for key in ("sender_email", "llm_provider", "llm_model",
                        "email_provider", "smtp_user", "smtp_password"):
                val = request.form.get(key, "").strip()
                new_settings[key] = val

            set_settings_bulk(conn, new_settings)

            # Also update the live app config's env so the rest of the app
            # picks up changes this session without restart
            for key, val in new_settings.items():
                if val:
                    os.environ[key.upper()] = val

            # Reload config so changes take effect immediately
            try:
                app.config["APP_CFG"] = load_config()
            except Exception:
                pass  # Non-fatal; settings are saved in DB regardless

            flash("Settings saved successfully.", "success")
            return redirect(url_for("settings_page"))
        except Exception as exc:
            logger.error("Settings save failed: %s", exc)
            flash(f"Failed to save settings: {exc}", "error")
            return redirect(url_for("settings_page"))
        finally:
            conn.close()

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the development server."""
    app = create_app()
    app.run(debug=True, host="127.0.0.1", port=5000)


if __name__ == "__main__":
    main()
