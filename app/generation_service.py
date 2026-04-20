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
) -> GenerationSummary:
    """Generate drafts for the selected professors inside one DB workspace."""

    conn = get_connection(db_path)
    try:
        sender = get_sender_profile(conn, sender_profile_id)
        if sender is None:
            raise ValueError(f"Sender profile {sender_profile_id} was not found.")

        professors = _load_target_professors(conn, professor_ids=professor_ids)
        if not professors:
            raise ValueError("No professors are available to generate drafts for.")

        session_id = create_session(
            conn,
            sender_profile_id,
            notes=f"Web generate at {datetime.now(tz=timezone.utc).isoformat()}",
        )
        summary = GenerationSummary(session_id=session_id)

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
        summary.scored = score_all_drafts(db_path, summary.session_id, config)
        try:
            from app.similarity import compute_session_similarity

            summary.flagged_similarity = compute_session_similarity(
                db_path, summary.session_id, config
            )
        except ModuleNotFoundError as exc:
            summary.warnings.append(
                f"Similarity analysis skipped because a dependency is missing: {exc.name}"
            )

    return summary
