"""
Click-based CLI for the Academic Outreach Email System.

Orchestrates all pipeline modules: import, enrich, summarize, personalize,
render, score, review, send, and export. Every command initializes logging,
loads config, and bootstraps the database before executing.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table

from app.config import Config, ConfigError, DEFAULT_CONFIG_YAML, load_config
from app.database import (
    add_suppression,
    create_session,
    get_connection,
    get_drafts,
    get_professors,
    get_sender_profile,
    get_sender_profiles,
    get_suppression_list,
    init_db,
    insert_draft,
    insert_sender_profile,
    update_draft,
)
from app.logger import audit_log, get_logger, init_logging
from app.models import AuditEntry, Draft, Professor, SenderProfile

console: Console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> Config:
    """Load config, initialize logging and database. Returns Config."""
    try:
        config: Config = load_config()
    except ConfigError as exc:
        console.print(f"[bold red]Configuration error:[/bold red] {exc}")
        raise SystemExit(1) from exc

    init_logging(log_dir=config.log_dir)
    init_db(config.db_path)
    return config


def _require_profiles(config: Config) -> List[SenderProfile]:
    """Return all sender profiles, or exit with a helpful message if none exist."""
    conn: sqlite3.Connection = get_connection(config.db_path)
    try:
        profiles: List[SenderProfile] = get_sender_profiles(conn)
    finally:
        conn.close()

    if not profiles:
        console.print(
            "[bold yellow]No sender profiles found.[/bold yellow] "
            "Create one first with: [cyan]python main.py profile --add[/cyan]"
        )
        raise SystemExit(1)
    return profiles


def _pick_profile(config: Config, profile_id: Optional[int]) -> SenderProfile:
    """Resolve a sender profile by ID, or prompt if only one exists."""
    conn: sqlite3.Connection = get_connection(config.db_path)
    try:
        if profile_id is not None:
            profile: Optional[SenderProfile] = get_sender_profile(conn, profile_id)
            if profile is None:
                console.print(f"[bold red]Sender profile ID {profile_id} not found.[/bold red]")
                raise SystemExit(1)
            return profile

        profiles: List[SenderProfile] = get_sender_profiles(conn)
    finally:
        conn.close()

    if not profiles:
        console.print(
            "[bold yellow]No sender profiles found.[/bold yellow] "
            "Create one first with: [cyan]python main.py profile --add[/cyan]"
        )
        raise SystemExit(1)

    if len(profiles) == 1:
        console.print(f"[dim]Using sender profile:[/dim] {profiles[0].name} ({profiles[0].email})")
        return profiles[0]

    # Multiple profiles -- ask user to pick
    table: Table = Table(title="Available Sender Profiles")
    table.add_column("ID", style="cyan", justify="right")
    table.add_column("Name", style="green")
    table.add_column("School")
    table.add_column("Email", style="dim")
    for p in profiles:
        table.add_row(str(p.id), p.name, p.school, p.email)
    console.print(table)

    chosen: str = click.prompt("Select profile ID", type=str)
    try:
        chosen_id: int = int(chosen)
    except ValueError:
        console.print("[bold red]Invalid profile ID.[/bold red]")
        raise SystemExit(1)

    conn = get_connection(config.db_path)
    try:
        profile = get_sender_profile(conn, chosen_id)
    finally:
        conn.close()

    if profile is None:
        console.print(f"[bold red]Profile ID {chosen_id} not found.[/bold red]")
        raise SystemExit(1)
    return profile


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group()
def cli() -> None:
    """Academic Outreach Email System -- CLI interface."""
    pass


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

@cli.command("import")
@click.argument("csv_path", type=click.Path(exists=True))
def import_csv(csv_path: str) -> None:
    """Load professors from a CSV file into the database."""
    config: Config = _bootstrap()
    logger = get_logger(__name__)

    console.print(Panel(f"[bold]Importing professors from:[/bold] {csv_path}", style="blue"))

    from app.csv_loader import load_csv

    try:
        imported: int
        skipped: int
        errors: List[str]
        imported, skipped, errors = load_csv(csv_path, config.db_path, config)
    except Exception as exc:
        console.print(f"[bold red]Import failed:[/bold red] {exc}")
        logger.exception("CSV import failed")
        raise SystemExit(1) from exc

    # Summary table
    summary: Table = Table(title="Import Summary", show_header=False)
    summary.add_column("Metric", style="bold")
    summary.add_column("Value", justify="right")
    summary.add_row("Imported", f"[green]{imported}[/green]")
    summary.add_row("Skipped", f"[yellow]{skipped}[/yellow]")
    summary.add_row("Errors", f"[red]{len(errors)}[/red]")
    console.print(summary)

    if errors:
        console.print("\n[bold red]Errors:[/bold red]")
        for err in errors[:10]:
            console.print(f"  [red]- {err}[/red]")
        if len(errors) > 10:
            console.print(f"  [dim]... and {len(errors) - 10} more[/dim]")

    audit_log(
        action="csv_import",
        detail=f"Imported {imported} professors from {csv_path} ({skipped} skipped, {len(errors)} errors)",
        db_path=config.db_path,
    )


# ---------------------------------------------------------------------------
# enrich
# ---------------------------------------------------------------------------

@cli.command("enrich")
@click.option("--limit", type=int, default=None, help="Max professors to enrich")
def enrich(limit: Optional[int]) -> None:
    """Run the enrichment pipeline on professors with status 'new'."""
    config: Config = _bootstrap()
    logger = get_logger(__name__)

    console.print(Panel("[bold]Running enrichment pipeline[/bold]", style="blue"))

    from app.enricher import enrich_all

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            progress.add_task("Enriching professors...", total=None)
            enriched: int
            failed: int
            enriched, failed = enrich_all(config.db_path, config, limit=limit)
    except Exception as exc:
        console.print(f"[bold red]Enrichment failed:[/bold red] {exc}")
        logger.exception("Enrichment pipeline failed")
        raise SystemExit(1) from exc

    summary: Table = Table(title="Enrichment Summary", show_header=False)
    summary.add_column("Metric", style="bold")
    summary.add_column("Value", justify="right")
    summary.add_row("Enriched", f"[green]{enriched}[/green]")
    summary.add_row("Failed", f"[red]{failed}[/red]")
    console.print(summary)

    audit_log(
        action="enrichment",
        detail=f"Enriched {enriched} professors ({failed} failed)",
        db_path=config.db_path,
    )


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------

@cli.command("generate")
@click.option("--all", "gen_all", is_flag=True, help="Generate for all ready professors")
@click.option("--profile", type=int, default=None, help="Sender profile ID")
@click.option("--variant", type=str, default=None, help="Template variant to use")
@click.option("--professor-id", type=int, default=None, help="Generate for a single professor")
def generate(
    gen_all: bool,
    profile: Optional[int],
    variant: Optional[str],
    professor_id: Optional[int],
) -> None:
    """Full pipeline: summarize, personalize, render, score, and similarity check."""
    config: Config = _bootstrap()
    logger = get_logger(__name__)

    from app.csv_loader import load_csv
    from app.database import get_connection, insert_draft, update_draft
    from app.enricher import enrich_all
    from app.personalizer import personalize_all
    from app.scorer import score_all_drafts
    from app.similarity import compute_session_similarity
    from app.summarizer import summarize_all
    from app.template_engine import get_available_variants, render_email

    console.print(Panel("[bold]Email Generation Pipeline[/bold]", style="blue"))

    # Resolve sender profile
    sender: SenderProfile = _pick_profile(config, profile)
    assert sender.id is not None

    # Validate variant if specified
    available_variants: List[str] = get_available_variants()
    if variant is not None and variant not in available_variants:
        console.print(
            f"[bold red]Unknown variant:[/bold red] {variant}\n"
            f"Available: {', '.join(available_variants) if available_variants else 'none (using inline templates)'}"
        )
        raise SystemExit(1)

    # Step 1: Summarize
    console.print("\n[bold cyan]Step 1/5:[/bold cyan] Summarizing enriched professors...")
    try:
        summarized: int
        sum_failed: int
        summarized, sum_failed = summarize_all(config.db_path, config)
        console.print(f"  Summarized: [green]{summarized}[/green]  Failed: [red]{sum_failed}[/red]")
    except Exception as exc:
        console.print(f"  [bold red]Summarization failed:[/bold red] {exc}")
        logger.exception("Summarization failed during generate")

    # Step 2: Personalize
    console.print("\n[bold cyan]Step 2/5:[/bold cyan] Personalizing for sender profile...")
    try:
        personalized: int
        pers_failed: int
        personalized, pers_failed = personalize_all(config.db_path, sender.id, config)
        console.print(f"  Personalized: [green]{personalized}[/green]  Failed: [red]{pers_failed}[/red]")
    except Exception as exc:
        console.print(f"  [bold red]Personalization failed:[/bold red] {exc}")
        logger.exception("Personalization failed during generate")

    # Step 3: Render emails
    console.print("\n[bold cyan]Step 3/5:[/bold cyan] Rendering email drafts...")
    conn: sqlite3.Connection = get_connection(config.db_path)
    try:
        # Get ready professors
        if professor_id is not None:
            from app.database import get_professor
            prof: Optional[Professor] = get_professor(conn, professor_id)
            if prof is None:
                console.print(f"[bold red]Professor ID {professor_id} not found.[/bold red]")
                raise SystemExit(1)
            professors: List[Professor] = [prof]
        else:
            professors = get_professors(conn, status="ready")

        if not professors:
            console.print("  [yellow]No professors with status 'ready'. Run enrich first.[/yellow]")
            return

        # Create session
        session_id: int = create_session(conn, sender.id, notes=f"CLI generate at {datetime.now(tz=timezone.utc).isoformat()}")
        console.print(f"  Created session: [cyan]{session_id}[/cyan]")

        drafts_created: int = 0
        draft_errors: int = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Rendering...", total=len(professors))
            for prof in professors:
                try:
                    draft: Draft = render_email(
                        prof, sender, config, session_id, variant=variant,
                    )
                    draft_id: int = insert_draft(conn, draft)
                    drafts_created += 1
                except Exception as exc:
                    draft_errors += 1
                    logger.error("Failed to render email for professor %s: %s", prof.name, exc)
                progress.advance(task)

        console.print(f"  Created: [green]{drafts_created}[/green]  Errors: [red]{draft_errors}[/red]")
    finally:
        conn.close()

    # Step 4: Score
    console.print("\n[bold cyan]Step 4/5:[/bold cyan] Scoring drafts...")
    try:
        scored: int = score_all_drafts(config.db_path, session_id, config)
        console.print(f"  Scored: [green]{scored}[/green] drafts")
    except Exception as exc:
        console.print(f"  [bold red]Scoring failed:[/bold red] {exc}")
        logger.exception("Scoring failed during generate")

    # Step 5: Similarity
    console.print("\n[bold cyan]Step 5/5:[/bold cyan] Computing cross-draft similarity...")
    try:
        flagged: int = compute_session_similarity(config.db_path, session_id, config)
        console.print(f"  Flagged: [yellow]{flagged}[/yellow] high-similarity pairs")
    except Exception as exc:
        console.print(f"  [bold red]Similarity check failed:[/bold red] {exc}")
        logger.exception("Similarity check failed during generate")

    # Display summary table
    conn = get_connection(config.db_path)
    try:
        session_drafts: List[Draft] = get_drafts(conn, session_id=session_id)
    finally:
        conn.close()

    if session_drafts:
        results_table: Table = Table(title=f"Session {session_id} -- Draft Summary")
        results_table.add_column("ID", style="cyan", justify="right")
        results_table.add_column("Professor ID", justify="right")
        results_table.add_column("Variant", style="dim")
        results_table.add_column("Overall", justify="right")
        results_table.add_column("Similarity", justify="right")
        results_table.add_column("Warnings")
        results_table.add_column("Status")

        for d in session_drafts:
            score_style: str = "green" if d.overall_score >= config.scoring.thresholds.high_quality else (
                "yellow" if d.overall_score >= config.scoring.thresholds.minimum_score else "red"
            )
            sim_str: str = f"{d.similarity_score:.2f}" if d.similarity_score is not None else "-"
            sim_style: str = "red" if (d.similarity_score or 0) > config.generation.similarity_threshold else "green"
            warnings_list: List[str] = d.warnings_list
            warn_str: str = f"[yellow]{len(warnings_list)}[/yellow]" if warnings_list else "[green]0[/green]"
            results_table.add_row(
                str(d.id),
                str(d.professor_id),
                d.template_variant or "-",
                f"[{score_style}]{d.overall_score:.1f}[/{score_style}]",
                f"[{sim_style}]{sim_str}[/{sim_style}]",
                warn_str,
                d.status,
            )
        console.print(results_table)

    audit_log(
        action="generate",
        detail=f"Session {session_id}: {drafts_created} drafts created for profile {sender.name}",
        db_path=config.db_path,
    )


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

@cli.command("review")
@click.option("--session", "session_id", type=int, default=None, help="Session ID to review")
def review(session_id: Optional[int]) -> None:
    """Launch interactive review of generated drafts."""
    config: Config = _bootstrap()
    logger = get_logger(__name__)

    from app.reviewer import interactive_review

    console.print(Panel("[bold]Interactive Draft Review[/bold]", style="blue"))

    try:
        interactive_review(config.db_path, session_id=session_id)
    except Exception as exc:
        console.print(f"[bold red]Review failed:[/bold red] {exc}")
        logger.exception("Interactive review failed")
        raise SystemExit(1) from exc

    audit_log(
        action="review",
        detail=f"Interactive review completed for session {session_id or 'all'}",
        db_path=config.db_path,
    )


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------

@cli.command("approve")
@click.argument("draft_ids", nargs=-1, type=int)
def approve(draft_ids: Tuple[int, ...]) -> None:
    """Bulk approve drafts by their IDs."""
    config: Config = _bootstrap()
    logger = get_logger(__name__)

    if not draft_ids:
        console.print("[yellow]No draft IDs provided.[/yellow]")
        return

    from app.reviewer import bulk_approve

    console.print(Panel(f"[bold]Approving {len(draft_ids)} draft(s)[/bold]", style="blue"))

    try:
        approved: int = bulk_approve(config.db_path, list(draft_ids))
        console.print(f"[green]Approved {approved} draft(s).[/green]")
    except Exception as exc:
        console.print(f"[bold red]Approval failed:[/bold red] {exc}")
        logger.exception("Bulk approve failed")
        raise SystemExit(1) from exc

    audit_log(
        action="bulk_approve",
        detail=f"Approved draft IDs: {list(draft_ids)}",
        db_path=config.db_path,
    )


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------

@cli.command("send")
@click.option("--dry-run", is_flag=True, help="Simulate sending without side effects")
@click.option("--draft-only", is_flag=True, default=True, help="Create Gmail drafts only (default)")
@click.option("--execute", is_flag=True, help="Actually send emails")
@click.option("--limit", type=int, default=None, help="Max emails to send")
@click.option(
    "--method",
    type=click.Choice(["gmail_draft", "gmail_send", "smtp"]),
    default="gmail_draft",
    help="Sending method",
)
def send(
    dry_run: bool,
    draft_only: bool,
    execute: bool,
    limit: Optional[int],
    method: str,
) -> None:
    """Send approved drafts with safety controls."""
    config: Config = _bootstrap()
    logger = get_logger(__name__)

    from app.sender import SafeSender

    # Safety gate: require explicit --execute to actually send
    if not execute and not dry_run:
        console.print(
            Panel(
                "[bold yellow]DRAFT-ONLY MODE[/bold yellow]\n"
                "Emails will be created as Gmail drafts, not sent.\n"
                "Use [cyan]--execute[/cyan] to actually send emails.\n"
                "Use [cyan]--dry-run[/cyan] to simulate without any side effects.",
                style="yellow",
            )
        )
        method = "gmail_draft"

    if dry_run:
        console.print(Panel("[bold cyan]DRY RUN MODE[/bold cyan] -- No emails will be sent or drafted.", style="cyan"))

    # Load approved drafts
    conn: sqlite3.Connection = get_connection(config.db_path)
    try:
        approved_drafts: List[Draft] = get_drafts(conn, status="approved")
    finally:
        conn.close()

    if not approved_drafts:
        console.print("[yellow]No approved drafts to send. Run review/approve first.[/yellow]")
        return

    if limit is not None:
        approved_drafts = approved_drafts[:limit]

    console.print(f"Found [cyan]{len(approved_drafts)}[/cyan] approved draft(s) to process.\n")

    if dry_run:
        # Show what would be sent
        dry_table: Table = Table(title="Dry Run -- Drafts to Send")
        dry_table.add_column("Draft ID", style="cyan", justify="right")
        dry_table.add_column("Professor ID", justify="right")
        dry_table.add_column("Score", justify="right")
        dry_table.add_column("Method")
        for d in approved_drafts:
            dry_table.add_row(str(d.id), str(d.professor_id), f"{d.overall_score:.1f}", method)
        console.print(dry_table)
        console.print(f"\n[dim]Would send {len(approved_drafts)} email(s) via {method}.[/dim]")
        return

    # Actual sending
    try:
        sender_instance: SafeSender = SafeSender(config)
        sent_count: int = 0
        fail_count: int = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Sending...", total=len(approved_drafts))
            for draft in approved_drafts:
                try:
                    sender_instance.send(draft, method=method)
                    sent_count += 1
                except Exception as exc:
                    fail_count += 1
                    logger.error("Failed to send draft %s: %s", draft.id, exc)
                progress.advance(task)

        # Summary
        send_table: Table = Table(title="Send Summary", show_header=False)
        send_table.add_column("Metric", style="bold")
        send_table.add_column("Value", justify="right")
        send_table.add_row("Method", method)
        send_table.add_row("Sent", f"[green]{sent_count}[/green]")
        send_table.add_row("Failed", f"[red]{fail_count}[/red]")
        console.print(send_table)

        audit_log(
            action="send",
            detail=f"Sent {sent_count} emails via {method} ({fail_count} failed)",
            db_path=config.db_path,
        )
    except Exception as exc:
        console.print(f"[bold red]Sending failed:[/bold red] {exc}")
        logger.exception("Send command failed")
        raise SystemExit(1) from exc


# ---------------------------------------------------------------------------
# followup
# ---------------------------------------------------------------------------

@cli.command("followup")
@click.option("--days-since", type=int, default=7, help="Days since original send")
def followup(days_since: int) -> None:
    """Generate follow-up emails for sent drafts older than N days."""
    config: Config = _bootstrap()
    logger = get_logger(__name__)

    from app.database import get_professor
    from app.template_engine import render_followup

    console.print(
        Panel(
            f"[bold]Generating Follow-ups[/bold]\n"
            f"For drafts sent more than {days_since} day(s) ago",
            style="blue",
        )
    )

    cutoff: datetime = datetime.now(tz=timezone.utc) - timedelta(days=days_since)
    cutoff_iso: str = cutoff.isoformat()

    conn: sqlite3.Connection = get_connection(config.db_path)
    try:
        # Find sent drafts older than the cutoff
        sent_drafts: List[Draft] = get_drafts(conn, status="sent")
        eligible: List[Draft] = []
        for d in sent_drafts:
            if d.created_at and d.created_at < cutoff_iso:
                eligible.append(d)

        if not eligible:
            console.print("[yellow]No eligible drafts for follow-up.[/yellow]")
            return

        console.print(f"Found [cyan]{len(eligible)}[/cyan] eligible draft(s).\n")

        from app.database import insert_followup

        followup_count: int = 0
        followup_errors: int = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Generating follow-ups...", total=len(eligible))
            for draft in eligible:
                try:
                    prof: Optional[Professor] = get_professor(conn, draft.professor_id)
                    sender_prof: Optional[SenderProfile] = get_sender_profile(conn, draft.sender_profile_id)
                    if prof is None or sender_prof is None:
                        followup_errors += 1
                        progress.advance(task)
                        continue

                    fu = render_followup(prof, sender_prof, draft, config)
                    insert_followup(conn, fu)
                    followup_count += 1
                except Exception as exc:
                    followup_errors += 1
                    logger.error("Failed to generate follow-up for draft %s: %s", draft.id, exc)
                progress.advance(task)
    finally:
        conn.close()

    summary: Table = Table(title="Follow-up Summary", show_header=False)
    summary.add_column("Metric", style="bold")
    summary.add_column("Value", justify="right")
    summary.add_row("Generated", f"[green]{followup_count}[/green]")
    summary.add_row("Errors", f"[red]{followup_errors}[/red]")
    console.print(summary)

    audit_log(
        action="followup_generate",
        detail=f"Generated {followup_count} follow-ups (days_since={days_since})",
        db_path=config.db_path,
    )


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

@cli.command("export")
@click.option(
    "--format", "fmt",
    type=click.Choice(["csv", "json", "txt", "tracking"]),
    default="csv",
    help="Export format",
)
@click.option("--session", "session_id", type=int, default=None, help="Session ID to export")
def export(fmt: str, session_id: Optional[int]) -> None:
    """Export drafts in the specified format."""
    config: Config = _bootstrap()
    logger = get_logger(__name__)

    from app.storage import (
        export_audit_log,
        export_drafts_csv,
        export_drafts_json,
        export_all_txt,
        export_tracking_csv,
    )

    console.print(Panel(f"[bold]Exporting drafts as {fmt.upper()}[/bold]", style="blue"))

    try:
        output_path: str
        if fmt == "csv":
            output_path = export_drafts_csv(config.db_path, config.output_dir, session_id=session_id)
        elif fmt == "json":
            output_path = export_drafts_json(config.db_path, config.output_dir, session_id=session_id)
        elif fmt == "txt":
            output_path = export_all_txt(config.db_path, config.output_dir, session_id=session_id)
        elif fmt == "tracking":
            output_path = export_tracking_csv(config.db_path, config.output_dir, session_id=session_id)
        else:
            console.print(f"[bold red]Unknown format: {fmt}[/bold red]")
            raise SystemExit(1)

        console.print(f"[green]Exported to:[/green] {output_path}")
    except Exception as exc:
        console.print(f"[bold red]Export failed:[/bold red] {exc}")
        logger.exception("Export command failed")
        raise SystemExit(1) from exc

    audit_log(
        action="export",
        detail=f"Exported {fmt} to {output_path} (session={session_id})",
        db_path=config.db_path,
    )


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@cli.command("status")
def status() -> None:
    """Show a dashboard with system status and statistics."""
    config: Config = _bootstrap()

    conn: sqlite3.Connection = get_connection(config.db_path)
    try:
        # Professor stats
        all_professors: List[Professor] = get_professors(conn)
        prof_by_status: Dict[str, int] = {}
        for p in all_professors:
            prof_by_status[p.status] = prof_by_status.get(p.status, 0) + 1

        # Draft stats
        all_drafts: List[Draft] = get_drafts(conn)
        draft_by_status: Dict[str, int] = {}
        for d in all_drafts:
            draft_by_status[d.status] = draft_by_status.get(d.status, 0) + 1

        # Sends today
        today_str: str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        sends_today_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM send_log WHERE sent_at LIKE ?",
            (f"{today_str}%",),
        ).fetchone()
        sends_today: int = sends_today_row["cnt"] if sends_today_row else 0

        # Session count
        session_row = conn.execute("SELECT COUNT(*) as cnt FROM sessions").fetchone()
        total_sessions: int = session_row["cnt"] if session_row else 0

        # Suppression count
        suppression_row = conn.execute("SELECT COUNT(*) as cnt FROM suppression_list").fetchone()
        total_suppressed: int = suppression_row["cnt"] if suppression_row else 0

        # Sender profiles count
        profiles: List[SenderProfile] = get_sender_profiles(conn)
    finally:
        conn.close()

    # Dashboard
    console.print(Panel("[bold]Academic Outreach Email System -- Dashboard[/bold]", style="blue"))

    # Professors table
    prof_table: Table = Table(title="Professors")
    prof_table.add_column("Status", style="bold")
    prof_table.add_column("Count", justify="right")
    for s in ["new", "enriched", "ready", "skip", "error"]:
        count: int = prof_by_status.get(s, 0)
        style: str = "green" if s == "ready" else ("yellow" if s in ("new", "enriched") else "red")
        prof_table.add_row(s, f"[{style}]{count}[/{style}]")
    prof_table.add_row("[bold]Total[/bold]", f"[bold]{len(all_professors)}[/bold]")
    console.print(prof_table)

    # Drafts table
    draft_table: Table = Table(title="Drafts")
    draft_table.add_column("Status", style="bold")
    draft_table.add_column("Count", justify="right")
    for s in ["generated", "approved", "rejected", "edited", "sent", "failed"]:
        count = draft_by_status.get(s, 0)
        style = "green" if s in ("approved", "sent") else ("yellow" if s == "generated" else "red")
        draft_table.add_row(s, f"[{style}]{count}[/{style}]")
    draft_table.add_row("[bold]Total[/bold]", f"[bold]{len(all_drafts)}[/bold]")
    console.print(draft_table)

    # System info
    info_table: Table = Table(title="System Info", show_header=False)
    info_table.add_column("Key", style="bold")
    info_table.add_column("Value")
    info_table.add_row("Sends Today", f"[cyan]{sends_today}[/cyan]")
    info_table.add_row("Total Sessions", str(total_sessions))
    info_table.add_row("Sender Profiles", str(len(profiles)))
    info_table.add_row("Suppressed Emails", str(total_suppressed))
    info_table.add_row("Active Model", config.llm_model)
    info_table.add_row("Email Provider", config.email_provider)
    info_table.add_row("Database", config.db_path)
    console.print(info_table)


# ---------------------------------------------------------------------------
# suppress
# ---------------------------------------------------------------------------

@cli.command("suppress")
@click.argument("email")
@click.option("--reason", default="manual", help="Reason for suppression")
def suppress(email: str, reason: str) -> None:
    """Add an email to the suppression list."""
    config: Config = _bootstrap()
    logger = get_logger(__name__)

    conn: sqlite3.Connection = get_connection(config.db_path)
    try:
        add_suppression(conn, email, reason)
    finally:
        conn.close()

    console.print(f"[green]Suppressed:[/green] {email} (reason: {reason})")

    audit_log(
        action="suppress",
        detail=f"Suppressed {email} (reason: {reason})",
        db_path=config.db_path,
    )


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------

@cli.command("profile")
@click.option("--add", is_flag=True, help="Add a new sender profile interactively")
@click.option("--list", "list_profiles", is_flag=True, help="List all sender profiles")
def profile(add: bool, list_profiles: bool) -> None:
    """Manage sender profiles."""
    config: Config = _bootstrap()
    logger = get_logger(__name__)

    if add:
        console.print(Panel("[bold]Create New Sender Profile[/bold]", style="blue"))

        name: str = click.prompt("Full name")
        school: str = click.prompt("School")
        grade: str = click.prompt("Grade (e.g. 11th, Junior)")
        email: str = click.prompt("Email address")
        interests: str = click.prompt("Research interests (comma-separated)")
        background: str = click.prompt("Background / experience summary")
        graduation_year: str = click.prompt("Graduation year", default="")

        new_profile: SenderProfile = SenderProfile(
            name=name,
            school=school,
            grade=grade,
            email=email,
            interests=interests,
            background=background,
            graduation_year=graduation_year if graduation_year else None,
        )

        conn: sqlite3.Connection = get_connection(config.db_path)
        try:
            profile_id: int = insert_sender_profile(conn, new_profile)
        finally:
            conn.close()

        console.print(f"\n[green]Created sender profile ID:[/green] [bold]{profile_id}[/bold]")

        audit_log(
            action="profile_create",
            detail=f"Created sender profile '{name}' (ID: {profile_id})",
            db_path=config.db_path,
        )
        return

    if list_profiles:
        conn = get_connection(config.db_path)
        try:
            profiles: List[SenderProfile] = get_sender_profiles(conn)
        finally:
            conn.close()

        if not profiles:
            console.print("[yellow]No sender profiles found. Use --add to create one.[/yellow]")
            return

        table: Table = Table(title="Sender Profiles")
        table.add_column("ID", style="cyan", justify="right")
        table.add_column("Name", style="green")
        table.add_column("School")
        table.add_column("Grade")
        table.add_column("Email", style="dim")
        table.add_column("Interests", max_width=40)
        table.add_column("Created", style="dim")
        for p in profiles:
            table.add_row(
                str(p.id),
                p.name,
                p.school,
                p.grade,
                p.email,
                p.interests[:40] + ("..." if len(p.interests) > 40 else ""),
                p.created_at[:10] if p.created_at else "-",
            )
        console.print(table)
        return

    # No flag provided -- show help
    console.print("[yellow]Use --add to create a profile or --list to view existing profiles.[/yellow]")


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------

@cli.command("model")
@click.option("--list", "list_models", is_flag=True, help="List available LLM models")
@click.option("--set", "set_model", type=str, default=None, help="Set the active LLM model")
def model(list_models: bool, set_model: Optional[str]) -> None:
    """List available LLM models or set the active one."""
    config: Config = _bootstrap()
    logger = get_logger(__name__)

    llm_models: Dict[str, Any] = DEFAULT_CONFIG_YAML.get("llm_models", {})
    openrouter_models: Dict[str, str] = llm_models.get("openrouter", {})
    default_model: str = llm_models.get("default_model", "")

    if list_models:
        table: Table = Table(title="Available LLM Models")
        table.add_column("Alias", style="cyan")
        table.add_column("Model ID", style="green")
        table.add_column("Active", justify="center")

        for alias, model_id in openrouter_models.items():
            is_active: str = "[bold green]Yes[/bold green]" if model_id == config.llm_model else ""
            table.add_row(alias, model_id, is_active)

        console.print(table)
        console.print(f"\n[dim]Current model:[/dim] {config.llm_model}")
        console.print("[dim]Set via: LLM_MODEL env var or --set flag[/dim]")
        return

    if set_model is not None:
        # Resolve alias to full model ID if needed
        resolved: str = openrouter_models.get(set_model, set_model)

        # Verify it is a known model
        known_ids: List[str] = list(openrouter_models.values())
        if resolved not in known_ids and set_model not in openrouter_models:
            console.print(
                f"[bold yellow]Warning:[/bold yellow] '{set_model}' is not a recognized model alias or ID.\n"
                f"Known aliases: {', '.join(openrouter_models.keys())}"
            )
            if not click.confirm("Set it anyway?"):
                return

        # Write to .env file
        project_root: Path = Path(config.db_path).parent.parent
        env_path: Path = project_root / ".env"

        # Read existing .env content
        env_lines: List[str] = []
        if env_path.exists():
            env_lines = env_path.read_text(encoding="utf-8").splitlines()

        # Update or add LLM_MODEL
        found: bool = False
        for i, line in enumerate(env_lines):
            if line.startswith("LLM_MODEL="):
                env_lines[i] = f"LLM_MODEL={resolved}"
                found = True
                break

        if not found:
            env_lines.append(f"LLM_MODEL={resolved}")

        env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")

        console.print(f"[green]Model set to:[/green] {resolved}")
        console.print("[dim]Restart the application for changes to take effect.[/dim]")

        audit_log(
            action="model_change",
            detail=f"LLM model changed to {resolved}",
            db_path=config.db_path,
        )
        return

    # No flag -- show current
    console.print(f"[bold]Current model:[/bold] {config.llm_model}")
    console.print("[dim]Use --list to see all models or --set to change.[/dim]")


# ---------------------------------------------------------------------------
# web
# ---------------------------------------------------------------------------

@cli.command("web")
@click.option("--port", type=int, default=5000, help="Port for the web UI")
def web(port: int) -> None:
    """Launch the Flask web UI."""
    config: Config = _bootstrap()
    logger = get_logger(__name__)

    console.print(
        Panel(
            f"[bold]Starting Web UI[/bold]\n"
            f"URL: [cyan]http://localhost:{port}[/cyan]",
            style="blue",
        )
    )

    audit_log(
        action="web_start",
        detail=f"Web UI started on port {port}",
        db_path=config.db_path,
    )

    try:
        from app.web.app import create_app

        app = create_app()
        app.run(host="0.0.0.0", port=port, debug=False)
    except ImportError:
        console.print(
            "[bold red]Flask web module not found.[/bold red]\n"
            "Ensure app/web/app.py exists and Flask is installed."
        )
        raise SystemExit(1)
    except Exception as exc:
        console.print(f"[bold red]Web UI failed to start:[/bold red] {exc}")
        logger.exception("Web UI startup failed")
        raise SystemExit(1) from exc


# ---------------------------------------------------------------------------
# find
# ---------------------------------------------------------------------------

@cli.command("find")
@click.option("--query", "-q", type=str, default=None, help="Search query (e.g. 'blockchain fintech AI')")
@click.option("--university", "-u", multiple=True, help="Filter by university (can specify multiple)")
@click.option("--field", type=str, default="", help="Research field to tag professors with")
@click.option("--max-results", type=int, default=25, help="Max results to return")
@click.option("--list-universities", is_flag=True, help="List top universities for filtering")
@click.option("--save/--no-save", default=True, help="Save found professors to database (default: save)")
def find(
    query: Optional[str],
    university: Tuple[str, ...],
    field: str,
    max_results: int,
    list_universities: bool,
    save: bool,
) -> None:
    """Find professors via OpenAlex academic database (free, no API key needed)."""
    config: Config = _bootstrap()
    logger = get_logger(__name__)

    from app.finder import find_professors, list_known_universities

    if list_universities:
        unis = list_known_universities()
        table: Table = Table(title="Top Universities for Filtering")
        table.add_column("#", style="dim", justify="right")
        table.add_column("University", style="cyan")
        for i, u in enumerate(unis, 1):
            table.add_row(str(i), u)
        console.print(table)
        console.print(f"\n[dim]Use: python main.py find -q \"blockchain AI\" -u \"Stanford University\"[/dim]")
        return

    if not query:
        console.print(
            "[bold yellow]Provide a search query:[/bold yellow]\n"
            "  [cyan]-q[/cyan] \"query\"              Search academic papers\n"
            "  [cyan]-u[/cyan] \"University\"          Filter by university (optional)\n"
            "  [cyan]--list-universities[/cyan]       Show top universities\n\n"
            "[dim]Example: python main.py find -q \"blockchain fintech\" -u \"MIT\"[/dim]"
        )
        return

    console.print(
        Panel(
            "[bold]Professor Finder[/bold] (via OpenAlex)\n"
            + f"Query: [cyan]{query}[/cyan]\n"
            + (f"Universities: [cyan]{', '.join(university)}[/cyan]\n" if university else "")
            + (f"Field: [cyan]{field}[/cyan]\n" if field else ""),
            style="blue",
        )
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        progress.add_task("Searching OpenAlex academic database...", total=None)
        professors, warnings = find_professors(
            query=query,
            universities=list(university) if university else None,
            field=field,
            max_scholar_results=max_results,
        )

    if warnings:
        console.print("\n[bold yellow]Warnings:[/bold yellow]")
        for w in warnings:
            console.print(f"  [yellow]{w}[/yellow]")

    if not professors:
        console.print("\n[yellow]No professors found. Try different or broader search terms.[/yellow]")
        return

    results_table: Table = Table(title=f"Found {len(professors)} Professor(s)")
    results_table.add_column("#", style="dim", justify="right")
    results_table.add_column("Name", style="green")
    results_table.add_column("University")
    results_table.add_column("Field", max_width=30)
    results_table.add_column("Research", max_width=50, style="dim")

    for i, prof in enumerate(professors, 1):
        results_table.add_row(
            str(i),
            prof.name,
            prof.university or "-",
            (prof.field or "-")[:30],
            (prof.research_summary or "-")[:50],
        )
    console.print(results_table)

    if save:
        from app.database import get_connection, upsert_professor

        conn: sqlite3.Connection = get_connection(config.db_path)
        saved: int = 0
        skipped: int = 0
        try:
            for prof in professors:
                try:
                    upsert_professor(conn, prof)
                    saved += 1
                except Exception as exc:
                    skipped += 1
                    logger.warning("Failed to save professor %s: %s", prof.name, exc)
        finally:
            conn.close()

        console.print(f"\n[green]Saved {saved} professor(s) to database.[/green]", end="")
        if skipped:
            console.print(f" [yellow]({skipped} skipped due to errors)[/yellow]")
        else:
            console.print()

        audit_log(
            action="find_professors",
            detail=f"Found {len(professors)} professors (query={query}, universities={list(university)}, saved={saved})",
            db_path=config.db_path,
        )
    else:
        console.print("\n[dim]Use --save to import these professors into the database.[/dim]")
