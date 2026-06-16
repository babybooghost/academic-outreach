"""Shared draft-generation service used by the CLI and web app."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.config import Config
from app.database import (
    create_session,
    get_connection,
    get_professor,
    get_professors,
    get_sender_profile,
    insert_draft,
    update_professor,
)
from app.models import Professor
from app.personalizer import personalize_professor
from app.scorer import score_all_drafts
from app.summarizer import summarize_professor
from app.template_engine import render_email


@dataclass
class GenerationSummary:
    """Outcome of a single draft-generation run."""

    session_id: int
    created: int = 0
    skipped: int = 0
    failed: int = 0
    scored: int = 0
    flagged_similarity: int = 0
    remaining: int = 0   # professors left for a follow-up run (batch was capped)
    warnings: list[str] = field(default_factory=list)


def _load_target_professors(
    conn: sqlite3.Connection,
    professor_ids: Optional[list[int]] = None,
) -> list[Professor]:
    if professor_ids:
        professors: list[Professor] = []
        for professor_id in professor_ids:
            prof = get_professor(conn, professor_id)
            if prof is not None:
                professors.append(prof)
        return professors
    return get_professors(conn)


def run_generation_pipeline(
    db_path: str,
    config: Config,
    sender_profile_id: int,
    professor_ids: Optional[list[int]] = None,
    variant: Optional[str] = None,
    workspace_id: int = 0,
    skip_existing_drafts: bool = False,
    max_professors: Optional[int] = None,
) -> GenerationSummary:
    """Generate drafts for the selected professors inside one DB workspace.

    ``skip_existing_drafts`` excludes professors that already have a draft (so a
    re-run doesn't duplicate work). ``max_professors`` caps how many are drafted
    in one call — important on serverless, where LLM-per-draft would otherwise
    exceed the function timeout; the leftover count is reported in ``remaining``.
    """

    conn = get_connection(db_path, workspace_id=workspace_id)
    try:
        sender = get_sender_profile(conn, sender_profile_id)
        if sender is None:
            raise ValueError(f"Sender profile {sender_profile_id} was not found.")

        professors = _load_target_professors(conn, professor_ids=professor_ids)
        had_targets = len(professors)
        if skip_existing_drafts and professors:
            drafted = {
                r["professor_id"] for r in conn.execute(
                    "SELECT DISTINCT professor_id FROM drafts WHERE workspace_id = ?",
                    (conn.workspace_id,),
                ).fetchall()
            }
            professors = [p for p in professors if p.id not in drafted]
        if not professors:
            # "All already drafted" is a normal end-state, not an error.
            if skip_existing_drafts and had_targets:
                done = GenerationSummary(session_id=0)
                done.warnings.append(
                    "Every saved professor already has a draft. Delete a draft to regenerate, "
                    "or save more faculty."
                )
                return done
            raise ValueError("No professors are available to generate drafts for.")

        remaining = 0
        if max_professors and len(professors) > max_professors:
            remaining = len(professors) - max_professors
            professors = professors[:max_professors]

        session_id = create_session(
            conn,
            sender_profile_id,
            notes=f"Web generate at {datetime.now(tz=timezone.utc).isoformat()}",
        )
        summary = GenerationSummary(session_id=session_id)
        summary.remaining = remaining

        for prof in professors:
            source_text = " ".join(
                part.strip()
                for part in (
                    prof.enrichment_text or "",
                    prof.research_summary or "",
                    prof.recent_work or "",
                )
                if part and part.strip()
            )
            if not source_text and not (prof.summary and prof.summary.strip()):
                summary.skipped += 1
                summary.warnings.append(
                    f"{prof.name}: missing research context, so no draft was created."
                )
                continue

            try:
                if not prof.summary or not prof.keywords_list:
                    summarize_professor(prof, config)

                if not prof.summary and not prof.keywords_list:
                    summary.skipped += 1
                    summary.warnings.append(
                        f"{prof.name}: summarization did not produce usable material."
                    )
                    continue

                personalize_professor(prof, sender, config)
                update_professor(conn, prof)

                draft = render_email(
                    prof=prof,
                    sender=sender,
                    config=config,
                    session_id=session_id,
                    variant=variant,
                )
                insert_draft(conn, draft)
                summary.created += 1
            except Exception as exc:  # pragma: no cover - exercised in web tests via route
                summary.failed += 1
                summary.warnings.append(f"{prof.name}: {exc}")

    finally:
        conn.close()

    if summary.created:
        summary.scored = score_all_drafts(
            db_path, summary.session_id, config, workspace_id=workspace_id
        )
        try:
            from app.similarity import compute_session_similarity

            summary.flagged_similarity = compute_session_similarity(
                db_path, summary.session_id, config, workspace_id=workspace_id
            )
        except ModuleNotFoundError as exc:
            summary.warnings.append(
                f"Similarity analysis skipped because a dependency is missing: {exc.name}"
            )

    return summary
