"""Review workflow state machine and interactive review logic.

Provides functions to build a review queue, approve/reject/edit drafts, and
an interactive CLI-based review loop powered by the rich library.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from app.database import (
    get_connection,
    get_draft,
    get_drafts,
    get_professor,
    log_audit,
    update_draft,
    update_draft_status,
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


def _build_review_item(draft: Draft, professor: Professor) -> dict[str, Any]:
    """Combine draft and professor data into a single review dict."""
    return {
        "draft_id": draft.id,
        "professor_name": professor.name,
        "professor_email": professor.email,
        "university": professor.university,
        "department": professor.department,
        "field": professor.field,
        "research_summary": professor.research_summary or "",
        "talking_points": professor.talking_points_list,
        "subject_lines": draft.subject_lines_list,
        "body": draft.body,
        "template_variant": draft.template_variant,
        "specificity_score": draft.specificity_score,
        "authenticity_score": draft.authenticity_score,
        "relevance_score": draft.relevance_score,
        "conciseness_score": draft.conciseness_score,
        "completeness_score": draft.completeness_score,
        "overall_score": draft.overall_score,
        "warnings": draft.warnings_list,
        "status": draft.status,
        "similarity_score": draft.similarity_score,
    }


# ---------------------------------------------------------------------------
# Review queue
# ---------------------------------------------------------------------------

def get_review_queue(
    db_path: str,
    session_id: int | None = None,
) -> list[dict[str, Any]]:
    """Return a list of review dicts for all drafts pending review.

    Each dict contains draft info, professor info, scores, and warnings.
    Only drafts with status ``"generated"`` or ``"edited"`` are included.
    """
    conn = get_connection(db_path)
    try:
        queue: list[dict[str, Any]] = []
        for status in ("generated", "edited"):
            drafts: list[Draft] = get_drafts(conn, session_id=session_id, status=status)
            for draft in drafts:
                professor: Professor | None = get_professor(conn, draft.professor_id)
                if professor is None:
                    logger.warning(
                        "Draft %d references missing professor_id %d -- skipping",
                        draft.id,
                        draft.professor_id,
                    )
                    continue
                queue.append(_build_review_item(draft, professor))
        return queue
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Single-draft actions
# ---------------------------------------------------------------------------

def approve_draft(
    db_path: str,
    draft_id: int,
    notes: str | None = None,
) -> bool:
    """Approve a draft, setting its status to ``"approved"``.

    Returns ``True`` on success, ``False`` if the draft was not found.
    """
    conn = get_connection(db_path)
    try:
        draft: Draft | None = get_draft(conn, draft_id)
        if draft is None:
            logger.warning("approve_draft: draft %d not found", draft_id)
            return False

        update_draft_status(conn, draft_id, "approved", notes=notes)

        log_audit(
            conn,
            AuditEntry(
                action="draft_approved",
                entity_type="draft",
                entity_id=draft_id,
                details=json.dumps({"notes": notes or ""}),
            ),
        )
        audit_log(
            action="draft_approved",
            detail=f"Draft {draft_id} approved",
            metadata={"draft_id": draft_id, "notes": notes},
            db_path=db_path,
        )
        logger.info("Draft %d approved", draft_id)
        return True
    finally:
        conn.close()


def reject_draft(
    db_path: str,
    draft_id: int,
    notes: str | None = None,
) -> bool:
    """Reject a draft, setting its status to ``"rejected"``.

    Returns ``True`` on success, ``False`` if the draft was not found.
    """
    conn = get_connection(db_path)
    try:
        draft: Draft | None = get_draft(conn, draft_id)
        if draft is None:
            logger.warning("reject_draft: draft %d not found", draft_id)
            return False

        update_draft_status(conn, draft_id, "rejected", notes=notes)

        log_audit(
            conn,
            AuditEntry(
                action="draft_rejected",
                entity_type="draft",
                entity_id=draft_id,
                details=json.dumps({"notes": notes or ""}),
            ),
        )
        audit_log(
            action="draft_rejected",
            detail=f"Draft {draft_id} rejected",
            metadata={"draft_id": draft_id, "notes": notes},
            db_path=db_path,
        )
        logger.info("Draft %d rejected", draft_id)
        return True
    finally:
        conn.close()


def edit_draft(
    db_path: str,
    draft_id: int,
    new_body: str,
    new_subject: str | None = None,
) -> bool:
    """Edit a draft's body (and optionally subject line), setting status to ``"edited"``.

    Returns ``True`` on success, ``False`` if the draft was not found.
    """
    conn = get_connection(db_path)
    try:
        draft: Draft | None = get_draft(conn, draft_id)
        if draft is None:
            logger.warning("edit_draft: draft %d not found", draft_id)
            return False

        draft.body = new_body
        if new_subject is not None:
            # Replace the first subject line, keeping others as alternatives
            existing: list[str] = draft.subject_lines_list
            if existing:
                existing[0] = new_subject
            else:
                existing = [new_subject]
            draft.subject_lines_list = existing
        draft.status = "edited"
        draft.reviewed_at = _now_iso()
        update_draft(conn, draft)

        log_audit(
            conn,
            AuditEntry(
                action="draft_edited",
                entity_type="draft",
                entity_id=draft_id,
                details=json.dumps({
                    "new_subject": new_subject,
                    "body_length": len(new_body),
                }),
            ),
        )
        audit_log(
            action="draft_edited",
            detail=f"Draft {draft_id} edited",
            metadata={"draft_id": draft_id, "new_subject": new_subject},
            db_path=db_path,
        )
        logger.info("Draft %d edited", draft_id)
        return True
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bulk actions
# ---------------------------------------------------------------------------

def bulk_approve(db_path: str, draft_ids: list[int]) -> int:
    """Approve multiple drafts at once. Returns the count of successfully approved drafts."""
    approved: int = 0
    for draft_id in draft_ids:
        if approve_draft(db_path, draft_id):
            approved += 1
    return approved


def bulk_reject(db_path: str, draft_ids: list[int]) -> int:
    """Reject multiple drafts at once. Returns the count of successfully rejected drafts."""
    rejected: int = 0
    for draft_id in draft_ids:
        if reject_draft(db_path, draft_id):
            rejected += 1
    return rejected


# ---------------------------------------------------------------------------
# Interactive CLI review
# ---------------------------------------------------------------------------

def _render_score_color(score: float) -> str:
    """Return a rich color tag based on the score value."""
    if score >= 7.5:
        return "green"
    if score >= 5.0:
        return "yellow"
    return "red"


def _display_review_item(console: Console, item: dict[str, Any], index: int, total: int) -> None:
    """Render a single draft for interactive review."""
    # Header
    console.print()
    console.rule(
        f"[bold cyan]Draft {item['draft_id']}[/bold cyan]  "
        f"({index}/{total})",
        style="cyan",
    )

    # Professor info
    prof_table = Table(title="Professor Info", show_header=False, border_style="blue")
    prof_table.add_column("Field", style="bold")
    prof_table.add_column("Value")
    prof_table.add_row("Name", item["professor_name"])
    prof_table.add_row("Email", item["professor_email"])
    prof_table.add_row("University", item["university"])
    prof_table.add_row("Department", item["department"])
    prof_table.add_row("Field", item["field"])
    console.print(prof_table)

    # Research summary
    if item.get("research_summary"):
        console.print(Panel(
            item["research_summary"],
            title="Research Summary",
            border_style="blue",
        ))

    # Talking points
    talking_points: list[str] = item.get("talking_points", [])
    if talking_points:
        tp_text = "\n".join(f"  - {tp}" for tp in talking_points)
        console.print(Panel(tp_text, title="Talking Points", border_style="blue"))

    # Subject lines
    console.print()
    console.print("[bold]Subject Lines:[/bold]")
    for i, subj in enumerate(item.get("subject_lines", []), start=1):
        console.print(f"  {i}. {subj}")

    # Email body
    console.print()
    console.print(Panel(
        item["body"],
        title="Email Body",
        border_style="green",
        padding=(1, 2),
    ))

    # Scores
    score_table = Table(title="Scores", border_style="magenta")
    score_table.add_column("Dimension", style="bold")
    score_table.add_column("Score", justify="right")
    dimensions: list[tuple[str, str]] = [
        ("Specificity", "specificity_score"),
        ("Authenticity", "authenticity_score"),
        ("Relevance", "relevance_score"),
        ("Conciseness", "conciseness_score"),
        ("Completeness", "completeness_score"),
    ]
    for label, key in dimensions:
        val: float = item.get(key, 0.0)
        color: str = _render_score_color(val)
        score_table.add_row(label, f"[{color}]{val:.1f}[/{color}]")

    overall: float = item.get("overall_score", 0.0)
    overall_color: str = _render_score_color(overall)
    score_table.add_row(
        "[bold]Overall[/bold]",
        f"[bold {overall_color}]{overall:.1f}[/bold {overall_color}]",
    )
    console.print(score_table)

    # Similarity score
    sim: float | None = item.get("similarity_score")
    if sim is not None:
        sim_color: str = "red" if sim > 0.85 else ("yellow" if sim > 0.70 else "green")
        console.print(f"  Similarity: [{sim_color}]{sim:.2f}[/{sim_color}]")

    # Warnings
    warnings: list[str] = item.get("warnings", [])
    if warnings:
        console.print()
        for warning in warnings:
            console.print(f"  [bold yellow]WARNING:[/bold yellow] {warning}")
    else:
        console.print("  [dim]No warnings.[/dim]")

    # Template variant
    console.print(f"  Template: [dim]{item.get('template_variant', 'N/A')}[/dim]")
    console.print()


def interactive_review(
    db_path: str,
    session_id: int | None = None,
) -> dict[str, int]:
    """Run a CLI-based interactive review session.

    For each draft in the review queue the user is prompted to:
    - **[a]pprove** the draft
    - **[r]eject** the draft
    - **[e]dit** the draft (prompts for new body / subject)
    - **[s]kip** (leave unchanged)
    - **[q]uit** (stop reviewing, remaining drafts are skipped)

    Returns a summary dict with counts: ``approved``, ``rejected``,
    ``edited``, ``skipped``.
    """
    console = Console()
    summary: dict[str, int] = {
        "approved": 0,
        "rejected": 0,
        "edited": 0,
        "skipped": 0,
    }

    queue: list[dict[str, Any]] = get_review_queue(db_path, session_id=session_id)
    total: int = len(queue)

    if total == 0:
        console.print("[bold green]No drafts pending review.[/bold green]")
        return summary

    console.print(f"\n[bold]Review queue: {total} draft(s) to review.[/bold]\n")

    for idx, item in enumerate(queue, start=1):
        _display_review_item(console, item, idx, total)

        valid_choices: set[str] = {"a", "r", "e", "s", "q"}
        choice: str = ""
        while choice not in valid_choices:
            choice = Prompt.ask(
                "[bold][a]pprove  [r]eject  [e]dit  [s]kip  [q]uit[/bold]",
                default="s",
            ).strip().lower()
            if choice and choice[0] in valid_choices:
                choice = choice[0]

        draft_id: int = item["draft_id"]

        if choice == "a":
            notes: str = Prompt.ask("Notes (optional)", default="").strip()
            approve_draft(db_path, draft_id, notes=notes or None)
            summary["approved"] += 1
            console.print("[green]Approved.[/green]")

        elif choice == "r":
            notes = Prompt.ask("Rejection reason (optional)", default="").strip()
            reject_draft(db_path, draft_id, notes=notes or None)
            summary["rejected"] += 1
            console.print("[red]Rejected.[/red]")

        elif choice == "e":
            console.print("[dim]Enter new email body (press Enter twice to finish):[/dim]")
            lines: list[str] = []
            empty_count: int = 0
            while True:
                try:
                    line: str = input()
                except EOFError:
                    break
                if line == "":
                    empty_count += 1
                    if empty_count >= 2:
                        break
                    lines.append(line)
                else:
                    empty_count = 0
                    lines.append(line)
            new_body: str = "\n".join(lines).strip()

            new_subject_input: str = Prompt.ask(
                "New subject line (leave blank to keep current)",
                default="",
            ).strip()
            new_subject: str | None = new_subject_input if new_subject_input else None

            if new_body:
                edit_draft(db_path, draft_id, new_body, new_subject)
                summary["edited"] += 1
                console.print("[yellow]Edited.[/yellow]")
            else:
                console.print("[dim]No body entered -- skipping edit.[/dim]")
                summary["skipped"] += 1

        elif choice == "s":
            summary["skipped"] += 1
            console.print("[dim]Skipped.[/dim]")

        elif choice == "q":
            remaining: int = total - idx
            summary["skipped"] += remaining + 1  # current + remaining
            console.print(f"[bold]Quitting. {remaining + 1} draft(s) skipped.[/bold]")
            break

    # Summary
    console.print()
    summary_table = Table(title="Review Summary", border_style="cyan")
    summary_table.add_column("Action", style="bold")
    summary_table.add_column("Count", justify="right")
    summary_table.add_row("[green]Approved[/green]", str(summary["approved"]))
    summary_table.add_row("[red]Rejected[/red]", str(summary["rejected"]))
    summary_table.add_row("[yellow]Edited[/yellow]", str(summary["edited"]))
    summary_table.add_row("[dim]Skipped[/dim]", str(summary["skipped"]))
    console.print(summary_table)

    audit_log(
        action="interactive_review_completed",
        detail=f"Review session completed: {json.dumps(summary)}",
        metadata=summary,
        db_path=db_path,
    )

    return summary
