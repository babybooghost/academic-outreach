"""Vercel serverless entry point for the Flask web UI."""

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

# Fallback: if the real app failed, serve a diagnostic page
if app is None:
    from flask import Flask

    app = Flask(__name__)

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def error_page(path):
        return (
            f"<h1>Startup Error</h1>"
            f"<pre style='white-space:pre-wrap'>{_boot_error}</pre>"
            f"<hr><p>Python {sys.version}</p>"
        ), 500
