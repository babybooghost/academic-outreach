"""Vercel serverless entry point for the Flask web UI."""

import sys
import traceback
from pathlib import Path

# Add project root to path so app/ imports work
_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

try:
    from app.web.app import create_app
    app = create_app()
except Exception as exc:
    # If the real app fails to import, serve a debug page
    from flask import Flask
    app = Flask(__name__)

    _error_detail = traceback.format_exc()

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def error_page(path):
        return (
            f"<h1>Import Error</h1><pre>{_error_detail}</pre>"
            f"<hr><p>Python: {sys.version}</p>"
            f"<p>Path: {sys.path}</p>"
        ), 500
