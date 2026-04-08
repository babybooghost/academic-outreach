"""Export functionality for the Academic Outreach Email System.

Provides functions to export drafts, audit logs, and tracking data to CSV,
JSON, and plain-text formats with timestamped filenames.
"""

from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.database import (
    get_audit_log,
    get_connection,
    get_draft,
    get_drafts,
    get_professor,
)
from app.logger import audit_log, get_logger
from app.models import AuditEntry, Draft, Professor

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(tz=timezone.utc).isoformat()


def _timestamp_str() -> str:
    """Return a filesystem-safe timestamp string like ``2026-04-06_143022``."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d_%H%M%S")


def _safe_filename(name: str) -> str:
    """Sanitize a string for use as a filename component."""
    cleaned: str = re.sub(r"[^\w\s-]", "", name).strip()
    return re.sub(r"[\s]+", "_", cleaned)


def _ensure_dir(path: str) -> None:
    """Create directory (and parents) if it does not exist."""
    Path(path).mkdir(parents=True, exist_ok=True)


def _draft_with_professor(
    conn: Any,
    draft: Draft,
) -> tuple[Draft, Professor | None]:
    """Fetch the professor associated with a draft."""
    professor: Professor | None = get_professor(conn, draft.professor_id)
    return draft, professor


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

_DRAFT_CSV_COLUMNS: list[str] = [
    "professor_name",
    "email",
    "university",
    "department",
    "field",
    "subject_line",
    "body",
    "overall_score",
    "warnings",
    "status",
    "similarity_score",
    "template_variant",
]


def export_drafts_csv(
    db_path: str,
    output_path: str,
    session_id: int | None = None,
) -> str:
    """Export all drafts (with professor info) to a CSV file.

    Parameters
    ----------
    db_path : str
        Path to the SQLite database.
    output_path : str
        Directory to write the CSV into.
    session_id : int, optional
        If provided, only export drafts for this session.

    Returns
    -------
    str
        Absolute path to the generated CSV file.
    """
    _ensure_dir(output_path)
    filename: str = f"drafts_{_timestamp_str()}.csv"
    filepath: str = os.path.join(output_path, filename)

    conn = get_connection(db_path)
    try:
        drafts: list[Draft] = get_drafts(conn, session_id=session_id)

        with open(filepath, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=_DRAFT_CSV_COLUMNS)
            writer.writeheader()

            for draft in drafts:
                professor: Professor | None = get_professor(conn, draft.professor_id)
                subject_lines: list[str] = draft.subject_lines_list
                subject_line: str = subject_lines[0] if subject_lines else ""

                row: dict[str, Any] = {
                    "professor_name": professor.name if professor else "",
                    "email": professor.email if professor else "",
                    "university": professor.university if professor else "",
                    "department": professor.department if professor else "",
                    "field": professor.field if professor else "",
                    "subject_line": subject_line,
                    "body": draft.body,
                    "overall_score": draft.overall_score,
                    "warnings": json.dumps(draft.warnings_list),
                    "status": draft.status,
                    "similarity_score": draft.similarity_score if draft.similarity_score is not None else "",
                    "template_variant": draft.template_variant,
                }
                writer.writerow(row)

        logger.info("Exported %d drafts to %s", len(drafts), filepath)
        audit_log(
            action="export_drafts_csv",
            detail=f"Exported {len(drafts)} drafts to CSV",
            metadata={"filepath": filepath, "count": len(drafts)},
            db_path=db_path,
        )
        return filepath
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# JSON export
# ---------------------------------------------------------------------------

def export_drafts_json(
    db_path: str,
    output_path: str,
    session_id: int | None = None,
) -> str:
    """Full JSON export of all drafts with all fields and professor info.

    Parameters
    ----------
    db_path : str
        Path to the SQLite database.
    output_path : str
        Directory to write the JSON file into.
    session_id : int, optional
        If provided, only export drafts for this session.

    Returns
    -------
    str
        Absolute path to the generated JSON file.
    """
    _ensure_dir(output_path)
    filename: str = f"drafts_{_timestamp_str()}.json"
    filepath: str = os.path.join(output_path, filename)

    conn = get_connection(db_path)
    try:
        drafts: list[Draft] = get_drafts(conn, session_id=session_id)
        records: list[dict[str, Any]] = []

        for draft in drafts:
            professor: Professor | None = get_professor(conn, draft.professor_id)
            entry: dict[str, Any] = {
                "draft": draft.to_dict(),
                "professor": professor.to_dict() if professor else None,
            }
            # Expand JSON-encoded fields for readability
            entry["draft"]["subject_lines_parsed"] = draft.subject_lines_list
            entry["draft"]["warnings_parsed"] = draft.warnings_list
            if professor:
                entry["professor"]["keywords_parsed"] = professor.keywords_list
                entry["professor"]["talking_points_parsed"] = professor.talking_points_list
            records.append(entry)

        with open(filepath, "w", encoding="utf-8") as jsonfile:
            json.dump(
                {
                    "exported_at": _now_iso(),
                    "count": len(records),
                    "session_id": session_id,
                    "drafts": records,
                },
                jsonfile,
                indent=2,
                default=str,
            )

        logger.info("Exported %d drafts to %s", len(records), filepath)
        audit_log(
            action="export_drafts_json",
            detail=f"Exported {len(records)} drafts to JSON",
            metadata={"filepath": filepath, "count": len(records)},
            db_path=db_path,
        )
        return filepath
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Single-draft TXT export
# ---------------------------------------------------------------------------

def export_draft_txt(
    draft: Draft,
    professor: Professor,
    output_dir: str,
) -> str:
    """Export a single draft as a ``.txt`` file.

    Filename format: ``{professor_name}_{university}.txt``

    Parameters
    ----------
    draft : Draft
        The draft to export.
    professor : Professor
        The associated professor.
    output_dir : str
        Directory to write into.

    Returns
    -------
    str
        Absolute path to the generated file.
    """
    _ensure_dir(output_dir)

    safe_name: str = _safe_filename(professor.name)
    safe_uni: str = _safe_filename(professor.university)
    filename: str = f"{safe_name}_{safe_uni}.txt"
    filepath: str = os.path.join(output_dir, filename)

    subject_lines: list[str] = draft.subject_lines_list
    warnings: list[str] = draft.warnings_list

    lines: list[str] = [
        f"To: {professor.name} <{professor.email}>",
        f"University: {professor.university}",
        f"Department: {professor.department}",
        f"Field: {professor.field}",
        "",
        "--- Subject Lines ---",
    ]
    for i, subj in enumerate(subject_lines, start=1):
        lines.append(f"  {i}. {subj}")

    lines.extend([
        "",
        "--- Email Body ---",
        "",
        draft.body,
        "",
        "--- Scores ---",
        f"  Overall:       {draft.overall_score:.1f}",
        f"  Specificity:   {draft.specificity_score:.1f}",
        f"  Authenticity:  {draft.authenticity_score:.1f}",
        f"  Relevance:     {draft.relevance_score:.1f}",
        f"  Conciseness:   {draft.conciseness_score:.1f}",
        f"  Completeness:  {draft.completeness_score:.1f}",
    ])

    if draft.similarity_score is not None:
        lines.append(f"  Similarity:    {draft.similarity_score:.2f}")

    lines.append(f"  Status:        {draft.status}")
    lines.append(f"  Template:      {draft.template_variant}")

    if warnings:
        lines.extend(["", "--- Warnings ---"])
        for warning in warnings:
            lines.append(f"  - {warning}")

    lines.append("")  # trailing newline

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    logger.info("Exported draft %d to %s", draft.id, filepath)
    return filepath


# ---------------------------------------------------------------------------
# Bulk TXT export
# ---------------------------------------------------------------------------

def export_all_txt(
    db_path: str,
    output_dir: str,
    session_id: int | None = None,
) -> int:
    """Export all drafts as individual ``.txt`` files.

    Parameters
    ----------
    db_path : str
        Path to the SQLite database.
    output_dir : str
        Directory to write the files into.
    session_id : int, optional
        If provided, only export drafts for this session.

    Returns
    -------
    int
        Number of files exported.
    """
    _ensure_dir(output_dir)
    conn = get_connection(db_path)
    try:
        drafts: list[Draft] = get_drafts(conn, session_id=session_id)
        count: int = 0

        for draft in drafts:
            professor: Professor | None = get_professor(conn, draft.professor_id)
            if professor is None:
                logger.warning(
                    "Draft %d: professor_id %d not found -- skipping export",
                    draft.id,
                    draft.professor_id,
                )
                continue
            export_draft_txt(draft, professor, output_dir)
            count += 1

        logger.info("Exported %d drafts as TXT to %s", count, output_dir)
        audit_log(
            action="export_all_txt",
            detail=f"Exported {count} drafts as TXT files",
            metadata={"output_dir": output_dir, "count": count},
            db_path=db_path,
        )
        return count
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Audit log export
# ---------------------------------------------------------------------------

def export_audit_log(
    db_path: str,
    output_path: str,
) -> str:
    """Export the full audit log to a JSON file.

    Parameters
    ----------
    db_path : str
        Path to the SQLite database.
    output_path : str
        Directory to write the JSON file into.

    Returns
    -------
    str
        Absolute path to the generated JSON file.
    """
    _ensure_dir(output_path)
    filename: str = f"audit_log_{_timestamp_str()}.json"
    filepath: str = os.path.join(output_path, filename)

    conn = get_connection(db_path)
    try:
        # Fetch all audit entries (large limit to get everything)
        entries: list[AuditEntry] = get_audit_log(conn, limit=100_000)
        records: list[dict[str, Any]] = [entry.to_dict() for entry in entries]

        with open(filepath, "w", encoding="utf-8") as jsonfile:
            json.dump(
                {
                    "exported_at": _now_iso(),
                    "count": len(records),
                    "entries": records,
                },
                jsonfile,
                indent=2,
                default=str,
            )

        logger.info("Exported %d audit entries to %s", len(records), filepath)
        return filepath
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tracking CSV export
# ---------------------------------------------------------------------------

_TRACKING_COLUMNS: list[str] = [
    "Professor Name",
    "University",
    "Department",
    "Email",
    "Field",
    "Profile URL",
    "Research Keywords",
    "Specific Hook Used",
    "Subject Line",
    "Date First Sent",
    "Date Follow-Up Sent",
    "Status",
    "Response Summary",
    "Next Action",
    "Priority",
    "Notes",
]


def export_tracking_csv(
    db_path: str,
    output_path: str,
) -> str:
    """Export a tracking sheet with comprehensive outreach data.

    Parameters
    ----------
    db_path : str
        Path to the SQLite database.
    output_path : str
        Directory to write the CSV file into.

    Returns
    -------
    str
        Absolute path to the generated CSV file.
    """
    _ensure_dir(output_path)
    filename: str = f"tracking_{_timestamp_str()}.csv"
    filepath: str = os.path.join(output_path, filename)

    conn = get_connection(db_path)
    try:
        drafts: list[Draft] = get_drafts(conn)

        # Build a mapping of professor_id -> first send date and follow-up date
        send_dates: dict[int, str] = {}
        followup_dates: dict[int, str] = {}
        try:
            rows = conn.execute(
                "SELECT professor_id, MIN(sent_at) as first_sent "
                "FROM send_log WHERE status = 'success' GROUP BY professor_id"
            ).fetchall()
            for row in rows:
                send_dates[row["professor_id"]] = row["first_sent"]
        except Exception as exc:
            logger.warning("Could not fetch send dates: %s", exc)

        try:
            rows = conn.execute(
                "SELECT professor_id, MIN(created_at) as followup_date "
                "FROM followups WHERE status = 'sent' GROUP BY professor_id"
            ).fetchall()
            for row in rows:
                followup_dates[row["professor_id"]] = row["followup_date"]
        except Exception as exc:
            logger.warning("Could not fetch follow-up dates: %s", exc)

        # Deduplicate: keep the best draft per professor (highest overall_score)
        best_drafts: dict[int, Draft] = {}
        for draft in drafts:
            pid: int = draft.professor_id
            if pid not in best_drafts or draft.overall_score > best_drafts[pid].overall_score:
                best_drafts[pid] = draft

        with open(filepath, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=_TRACKING_COLUMNS)
            writer.writeheader()

            for pid, draft in sorted(best_drafts.items()):
                professor: Professor | None = get_professor(conn, pid)
                if professor is None:
                    continue

                subject_lines: list[str] = draft.subject_lines_list
                subject_line: str = subject_lines[0] if subject_lines else ""
                keywords: list[str] = professor.keywords_list
                talking_points: list[str] = professor.talking_points_list
                hook: str = talking_points[0] if talking_points else ""

                # Determine priority based on score
                priority: str = "High" if draft.overall_score >= 7.5 else (
                    "Medium" if draft.overall_score >= 5.0 else "Low"
                )

                # Determine next action
                if draft.status == "sent":
                    next_action: str = "Follow-up" if pid not in followup_dates else "Monitor"
                elif draft.status == "approved":
                    next_action = "Send"
                elif draft.status in ("generated", "edited"):
                    next_action = "Review"
                elif draft.status == "rejected":
                    next_action = "Regenerate"
                else:
                    next_action = ""

                row: dict[str, Any] = {
                    "Professor Name": professor.name,
                    "University": professor.university,
                    "Department": professor.department,
                    "Email": professor.email,
                    "Field": professor.field,
                    "Profile URL": professor.profile_url or "",
                    "Research Keywords": "; ".join(keywords),
                    "Specific Hook Used": hook,
                    "Subject Line": subject_line,
                    "Date First Sent": send_dates.get(pid, ""),
                    "Date Follow-Up Sent": followup_dates.get(pid, ""),
                    "Status": draft.status,
                    "Response Summary": "",
                    "Next Action": next_action,
                    "Priority": priority,
                    "Notes": professor.notes or "",
                }
                writer.writerow(row)

        row_count: int = len(best_drafts)
        logger.info("Exported tracking sheet with %d entries to %s", row_count, filepath)
        audit_log(
            action="export_tracking_csv",
            detail=f"Exported tracking sheet with {row_count} entries",
            metadata={"filepath": filepath, "count": row_count},
            db_path=db_path,
        )
        return filepath
    finally:
        conn.close()
