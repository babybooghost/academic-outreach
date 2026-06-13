"""Vercel serverless entry point for the Flask web UI."""

import os
import sys
import traceback
from pathlib import Path

# Add project root to path so app/ imports work
_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

# Vercel requires a top-level `app` variable — always define it
app = None
_boot_error = ""

try:
    from app.web.app import create_app
    app = create_app()
except Exception:
    _boot_error = traceback.format_exc()
    # Always log the real traceback to the server logs (Vercel captures stderr).
    print(_boot_error, file=sys.stderr)

# Fallback: if the real app failed, serve a generic page. The traceback is only
# echoed to the browser when SHOW_BOOT_ERRORS is explicitly enabled.
if app is None:
    from flask import Flask

    app = Flask(__name__)
    _show_detail = os.environ.get("SHOW_BOOT_ERRORS", "").strip().lower() in {"1", "true", "yes"}

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def error_page(path):
        if _show_detail:
            return (
                "<h1>Startup Error</h1>"
                f"<pre style='white-space:pre-wrap'>{_boot_error}</pre>"
                f"<hr><p>Python {sys.version}</p>"
            ), 500
        return (
            "<h1>Service temporarily unavailable</h1>"
            "<p>The application failed to start. The error has been logged. "
            "Please try again shortly.</p>"
        ), 500
