"""
CSV import module for the Academic Outreach Email System.

Loads professor data from CSV files, validates rows, checks suppression lists,
and upserts into the professors table via the database layer.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from app.config import Config
from app.database import get_connection, is_suppressed, upsert_professor, log_audit
from app.logger import get_logger, audit_log
from app.models import AuditEntry, Professor

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXPECTED_COLUMNS: tuple[str, ...] = (
    "name", "email", "university", "department", "field",
    "profile_url", "research_summary", "recent_work", "notes",
    "title", "lab_name",
)

REQUIRED_COLUMNS: tuple[str, ...] = ("name", "email")

_EMAIL_RE: re.Pattern[str] = re.compile(
    r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_email(email: str) -> bool:
    """Return True if the email matches a basic RFC-style pattern."""
    return bool(_EMAIL_RE.match(email.strip()))


def _read_csv_rows(csv_path: str) -> tuple[list[dict[str, str]], str]:
    """
    Read CSV rows, trying utf-8 first, then falling back to latin-1.

    Returns (rows, encoding_used).
    """
    path: Path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    for encoding in ("utf-8", "latin-1"):
        try:
            with open(path, "r", encoding=encoding, newline="") as fh:
                reader = csv.DictReader(fh)
                rows: list[dict[str, str]] = list(reader)
                return rows, encoding
        except UnicodeDecodeError:
            continue

    raise ValueError(f"Unable to decode CSV file with utf-8 or latin-1: {csv_path}")


def _row_to_professor(row: dict[str, str]) -> Professor:
    """Convert a cleaned CSV row dict into a Professor dataclass."""
    return Professor(
        name=row.get("name", "").strip(),
        email=row.get("email", "").strip().lower(),
        university=row.get("university", "").strip(),
        department=row.get("department", "").strip(),
        field=row.get("field", "").strip(),
        profile_url=row.get("profile_url", "").strip() or None,
        research_summary=row.get("research_summary", "").strip() or None,
        recent_work=row.get("recent_work", "").strip() or None,
        notes=row.get("notes", "").strip() or None,
        title=row.get("title", "").strip() or None,
        lab_name=row.get("lab_name", "").strip() or None,
        status="new",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_csv(
    csv_path: str,
    db_path: str,
    config: Config,
) -> tuple[int, int, list[str]]:
    """
    Import professors from a CSV file into the database.

    Parameters
    ----------
    csv_path : str
        Path to the CSV file.
    db_path : str
        Path to the SQLite database.
    config : Config
        Application configuration.

    Returns
    -------
    tuple[int, int, list[str]]
        (imported_count, skipped_count, warning_messages)
    """
    imported: int = 0
    skipped: int = 0
    warnings: list[str] = []

    # --- Read CSV ---
    try:
        rows, encoding_used = _read_csv_rows(csv_path)
    except (FileNotFoundError, ValueError) as exc:
        error_msg: str = f"Failed to read CSV: {exc}"
        logger.error(error_msg)
        audit_log(
            action="csv_load_error",
            detail=error_msg,
            db_path=db_path,
        )
        return 0, 0, [error_msg]

    if encoding_used != "utf-8":
        fallback_msg: str = (
            f"CSV file decoded with {encoding_used} fallback "
            f"(utf-8 failed): {csv_path}"
        )
        warnings.append(fallback_msg)
        logger.warning(fallback_msg)

    if not rows:
        empty_msg: str = f"CSV file is empty or has no data rows: {csv_path}"
        warnings.append(empty_msg)
        logger.warning(empty_msg)
        return 0, 0, warnings

    # --- Validate header columns ---
    first_row_keys: set[str] = set(rows[0].keys())
    missing_required: set[str] = set(REQUIRED_COLUMNS) - first_row_keys
    if missing_required:
        header_msg: str = (
            f"CSV is missing required column(s): {', '.join(sorted(missing_required))}"
        )
        logger.error(header_msg)
        return 0, 0, [header_msg]

    missing_optional: set[str] = set(EXPECTED_COLUMNS) - first_row_keys
    if missing_optional:
        opt_msg: str = (
            f"CSV is missing optional column(s) (will use defaults): "
            f"{', '.join(sorted(missing_optional))}"
        )
        warnings.append(opt_msg)
        logger.info(opt_msg)

    # --- Process rows ---
    conn: sqlite3.Connection = get_connection(db_path)
    try:
        for row_num, row in enumerate(rows, start=2):  # row 1 is header
            name: str = row.get("name", "").strip()
            email: str = row.get("email", "").strip().lower()

            # Required field check
            if not name:
                msg: str = f"Row {row_num}: missing 'name' -- skipped"
                warnings.append(msg)
                logger.warning(msg)
                skipped += 1
                continue

            if not email:
                msg = f"Row {row_num}: missing 'email' for '{name}' -- skipped"
                warnings.append(msg)
                logger.warning(msg)
                skipped += 1
                continue

            # Email format validation
            if not _validate_email(email):
                msg = f"Row {row_num}: invalid email '{email}' for '{name}' -- skipped"
                warnings.append(msg)
                logger.warning(msg)
                skipped += 1
                continue

            # Suppression check
            try:
                if is_suppressed(conn, email):
                    msg = (
                        f"Row {row_num}: email '{email}' is on the suppression list "
                        f"-- skipped"
                    )
                    warnings.append(msg)
                    logger.info(msg)
                    skipped += 1
                    continue
            except Exception as exc:
                msg = (
                    f"Row {row_num}: suppression check failed for '{email}': "
                    f"{exc} -- skipped"
                )
                warnings.append(msg)
                logger.error(msg)
                skipped += 1
                continue

            # Build Professor and upsert
            prof: Professor = _row_to_professor(row)
            try:
                prof_id: int = upsert_professor(conn, prof)
                imported += 1

                audit_log(
                    action="csv_import",
                    detail=f"Imported professor '{name}' <{email}> (id={prof_id})",
                    metadata={
                        "professor_email": email,
                        "professor_id": prof_id,
                        "source_file": csv_path,
                        "row_number": row_num,
                    },
                    db_path=db_path,
                )
            except Exception as exc:
                msg = f"Row {row_num}: database upsert failed for '{email}': {exc}"
                warnings.append(msg)
                logger.error(msg)
                skipped += 1

    finally:
        conn.close()

    # --- Summary audit entry ---
    summary_detail: str = (
        f"CSV load complete: {imported} imported, {skipped} skipped, "
        f"{len(warnings)} warning(s) from '{csv_path}'"
    )
    audit_log(
        action="csv_load_complete",
        detail=summary_detail,
        metadata={
            "imported": imported,
            "skipped": skipped,
            "warnings_count": len(warnings),
            "source_file": csv_path,
        },
        db_path=db_path,
    )
    logger.info(summary_detail)

    return imported, skipped, warnings
