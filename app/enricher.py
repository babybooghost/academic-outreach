"""
Web scraping enrichment pipeline for the Academic Outreach Email System.

Fetches professor profile pages, extracts meaningful text content, and stores
it as enrichment_text for downstream summarization.
"""

from __future__ import annotations

import random
import sqlite3
import time
import urllib.robotparser
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from app.config import Config
from app.database import get_connection, get_professors, update_professor
from app.logger import get_logger, audit_log
from app.models import Professor

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_USER_AGENT: str = (
    "AcademicOutreachBot/1.0 (educational research; polite crawler; "
    "respects robots.txt)"
)
_REQUEST_TIMEOUT: int = 10
_MAX_TEXT_LENGTH: int = 5000
_STRIP_TAGS: tuple[str, ...] = (
    "nav", "footer", "sidebar", "script", "style", "header", "aside",
    "noscript", "form", "iframe",
)
_DEFAULT_DELAY_MIN: float = 2.0
_DEFAULT_DELAY_MAX: float = 3.0


# ---------------------------------------------------------------------------
# Robots.txt checker
# ---------------------------------------------------------------------------

def _is_allowed_by_robots(
    url: str,
    user_agent: str,
) -> bool:
    """
    Check robots.txt for the given URL.

    Returns True if fetching is allowed or if robots.txt cannot be retrieved
    (fail-open to avoid blocking on misconfigured servers, but we log a warning).
    """
    try:
        parsed = urlparse(url)
        robots_url: str = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(robots_url)
        rp.read()
        return rp.can_fetch(user_agent, url)
    except Exception as exc:
        logger.warning(
            "Could not fetch robots.txt for %s: %s -- allowing fetch",
            url, exc,
        )
        return True


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def _extract_text(html: str) -> str:
    """
    Parse HTML and return cleaned, truncated plain text.

    Strips navigation, footer, sidebar, script, style, and header elements
    before extracting visible text.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove unwanted tags
    for tag_name in _STRIP_TAGS:
        for element in soup.find_all(tag_name):
            element.decompose()

    # Extract visible text
    text: str = soup.get_text(separator=" ", strip=True)

    # Normalize whitespace
    text = " ".join(text.split())

    # Truncate to max length
    if len(text) > _MAX_TEXT_LENGTH:
        text = text[:_MAX_TEXT_LENGTH]

    return text


# ---------------------------------------------------------------------------
# Single professor enrichment
# ---------------------------------------------------------------------------

def enrich_professor(
    prof: Professor,
    config: Config,
) -> Professor:
    """
    Fetch the professor's profile_url and extract text content.

    Updates prof.enrichment_text and prof.status. On failure, sets
    status='skip' and records the reason in prof.notes.

    Parameters
    ----------
    prof : Professor
        The professor to enrich. Must have a valid profile_url.
    config : Config
        Application configuration.

    Returns
    -------
    Professor
        The updated professor (same object, mutated in place).
    """
    url: Optional[str] = prof.profile_url

    if not url or url.strip() in ("", "#"):
        prof.status = "skip"
        prof.notes = _append_note(
            prof.notes, "No valid profile URL available for enrichment"
        )
        logger.info(
            "Skipping enrichment for %s: no valid profile URL", prof.name
        )
        return prof

    user_agent: str = _DEFAULT_USER_AGENT

    # Robots.txt check
    if not _is_allowed_by_robots(url, user_agent):
        prof.status = "skip"
        prof.notes = _append_note(
            prof.notes, f"Blocked by robots.txt: {url}"
        )
        logger.info(
            "Skipping enrichment for %s: blocked by robots.txt", prof.name
        )
        return prof

    # Fetch page
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})

    try:
        response: requests.Response = session.get(url, timeout=_REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        status_code: int = exc.response.status_code if exc.response is not None else 0
        reason: str = f"HTTP {status_code} when fetching {url}"
        prof.status = "skip"
        prof.notes = _append_note(prof.notes, reason)
        logger.warning("Enrichment failed for %s: %s", prof.name, reason)
        return prof
    except requests.exceptions.Timeout:
        reason = f"Timeout after {_REQUEST_TIMEOUT}s when fetching {url}"
        prof.status = "skip"
        prof.notes = _append_note(prof.notes, reason)
        logger.warning("Enrichment failed for %s: %s", prof.name, reason)
        return prof
    except requests.exceptions.SSLError as exc:
        reason = f"SSL error when fetching {url}: {exc}"
        prof.status = "skip"
        prof.notes = _append_note(prof.notes, reason)
        logger.warning("Enrichment failed for %s: %s", prof.name, reason)
        return prof
    except requests.exceptions.ConnectionError as exc:
        reason = f"Connection error when fetching {url}: {exc}"
        prof.status = "skip"
        prof.notes = _append_note(prof.notes, reason)
        logger.warning("Enrichment failed for %s: %s", prof.name, reason)
        return prof
    except requests.exceptions.RequestException as exc:
        reason = f"Request error when fetching {url}: {exc}"
        prof.status = "skip"
        prof.notes = _append_note(prof.notes, reason)
        logger.warning("Enrichment failed for %s: %s", prof.name, reason)
        return prof
    finally:
        session.close()

    # Extract text
    extracted: str = _extract_text(response.text)

    if not extracted.strip():
        prof.status = "skip"
        prof.notes = _append_note(
            prof.notes, f"No meaningful text extracted from {url}"
        )
        logger.info(
            "No text extracted for %s from %s", prof.name, url
        )
        return prof

    prof.enrichment_text = extracted
    prof.status = "enriched"
    logger.info(
        "Enriched %s: extracted %d chars from %s",
        prof.name, len(extracted), url,
    )
    return prof


# ---------------------------------------------------------------------------
# Batch enrichment
# ---------------------------------------------------------------------------

def enrich_all(
    db_path: str,
    config: Config,
    limit: int | None = None,
) -> tuple[int, int]:
    """
    Enrich all professors with status='new' that have a profile URL.

    Parameters
    ----------
    db_path : str
        Path to the SQLite database.
    config : Config
        Application configuration.
    limit : int or None
        Maximum number of professors to enrich. None means no limit.

    Returns
    -------
    tuple[int, int]
        (enriched_count, failed_count)
    """
    enriched: int = 0
    failed: int = 0

    conn: sqlite3.Connection = get_connection(db_path)
    try:
        professors: list[Professor] = get_professors(conn, status="new")

        if limit is not None:
            professors = professors[:limit]

        total: int = len(professors)
        logger.info("Starting enrichment for %d professor(s)", total)

        for idx, prof in enumerate(professors):
            try:
                enrich_professor(prof, config)

                # Persist to database
                if prof.id is not None:
                    update_professor(conn, prof)

                if prof.status == "enriched":
                    enriched += 1
                    audit_log(
                        action="enrichment_success",
                        detail=(
                            f"Enriched professor '{prof.name}' "
                            f"({len(prof.enrichment_text or '')} chars)"
                        ),
                        metadata={
                            "professor_id": prof.id,
                            "professor_email": prof.email,
                            "url": prof.profile_url,
                            "text_length": len(prof.enrichment_text or ""),
                        },
                        db_path=db_path,
                    )
                else:
                    failed += 1
                    audit_log(
                        action="enrichment_skip",
                        detail=(
                            f"Skipped enrichment for '{prof.name}': "
                            f"{prof.notes or 'unknown reason'}"
                        ),
                        metadata={
                            "professor_id": prof.id,
                            "professor_email": prof.email,
                            "status": prof.status,
                        },
                        db_path=db_path,
                    )

            except Exception as exc:
                failed += 1
                logger.error(
                    "Unexpected error enriching %s: %s", prof.name, exc
                )
                audit_log(
                    action="enrichment_error",
                    detail=f"Error enriching '{prof.name}': {exc}",
                    metadata={
                        "professor_id": prof.id,
                        "professor_email": prof.email,
                    },
                    db_path=db_path,
                )

            # Polite delay between requests (skip after last item)
            if idx < total - 1:
                delay: float = random.uniform(_DEFAULT_DELAY_MIN, _DEFAULT_DELAY_MAX)
                logger.debug("Sleeping %.1fs between requests", delay)
                time.sleep(delay)

    finally:
        conn.close()

    summary: str = (
        f"Enrichment complete: {enriched} enriched, {failed} failed/skipped "
        f"out of {total} total"
    )
    audit_log(
        action="enrichment_batch_complete",
        detail=summary,
        metadata={"enriched": enriched, "failed": failed, "total": total},
        db_path=db_path,
    )
    logger.info(summary)

    return enriched, failed


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _append_note(existing: Optional[str], new_note: str) -> str:
    """Append a note to the existing notes string, separated by ' | '."""
    if existing and existing.strip():
        return f"{existing.strip()} | {new_note}"
    return new_note
