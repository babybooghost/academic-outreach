"""
Entry point for the Academic Outreach Email System CLI.

Usage:
    python main.py <command> [options]

Run ``python main.py --help`` for a full list of commands.
"""

from __future__ import annotations

from app.cli import cli

if __name__ == "__main__":
    cli()
