"""Vercel serverless entry point for the Flask web UI."""

import sys
from pathlib import Path

# Add project root to path so app/ imports work
_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from app.web.app import create_app

app = create_app()
