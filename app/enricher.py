"""
Web scraping enrichment pipeline for the Academic Outreach Email System.

Fetches professor profile pages, extracts meaningful text content, and stores
it as enrichment_text for downstream summarization.
"""

from __future__ import annotations

import random
import re
import sqlite3
import time
import urllib.robotparser
from typing import Optional
from urllib.parse import urljoin, urlparse

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
# Email extraction
# ---------------------------------------------------------------------------

# Matches a normal email address.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Local parts that indicate a department/role mailbox rather than the professor.
_GENERIC_LOCALPARTS: frozenset[str] = frozenset({
    "info", "admin", "webmaster", "contact", "support", "help", "office",
    "department", "dept", "hr", "jobs", "careers", "press", "media", "noreply",
    "no-reply", "donotreply", "postmaster", "enquiries", "inquiries", "general",
    "sales", "marketing", "privacy", "security", "abuse", "webadmin", "it",
})

# File extensions that the email regex can falsely match (e.g. "logo@2x.png").
_IMAGE_EXTENSIONS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")


def _deobfuscate(text: str) -> str:
    """Turn common email obfuscations into a parseable form.

    Handles "name [at] domain [dot] edu", "(at)", " AT ", "&#64;", etc.
    """
    out = text
    out = out.replace("&#64;", "@").replace("&commat;", "@").replace("&#46;", ".")
    # " at "/"[at]"/"(at)" -> "@"   and  " dot "/"[dot]"/"(dot)" -> "."
    out = re.sub(r"\s*[\(\[\{]\s*at\s*[\)\]\}]\s*", "@", out, flags=re.IGNORECASE)
    out = re.sub(r"\s+at\s+", "@", out, flags=re.IGNORECASE)
    out = re.sub(r"\s*[\(\[\{]\s*dot\s*[\)\]\}]\s*", ".", out, flags=re.IGNORECASE)
    out = re.sub(r"\s+dot\s+", ".", out, flags=re.IGNORECASE)
    return out


def _looks_like_email(candidate: str) -> bool:
    candidate = candidate.strip().strip(".").lower()
    if not candidate or candidate.endswith(_IMAGE_EXTENSIONS):
        return False
    local = candidate.split("@", 1)[0]
    if local in _GENERIC_LOCALPARTS:
        return False
    return bool(_EMAIL_RE.fullmatch(candidate))


def _name_tokens(name: str) -> list[str]:
    return [t.lower() for t in re.split(r"[^A-Za-z]+", name or "") if len(t) > 1]


def _score_email_candidate(email: str, name: str, university: str) -> int:
    """Rank a candidate email. Higher is a more confident match for the professor."""
    email = email.lower()
    local, _, domain = email.partition("@")
    score = 0
    if domain.endswith(".edu") or ".edu." in domain or ".ac." in domain:
        score += 3
    tokens = _name_tokens(name)
    if tokens:
        last = tokens[-1]
        first = tokens[0]
        if last and last in local:
            score += 3
        if first and first in local:
            score += 1
        if first and last and (first[0] + last) in local:  # e.g. jsmith
            score += 2
    # Domain echoing the university name is a mild positive signal.
    uni_tokens = _name_tokens(university)
    if any(tok in domain for tok in uni_tokens if len(tok) > 3):
        score += 1
    return score


def extract_email_from_html(
    html: str,
    name: str = "",
    university: str = "",
) -> Optional[str]:
    """Extract the most likely professor email from a page's HTML.

    Prefers ``mailto:`` links and addresses that match the professor's name on a
    ``.edu`` domain. Handles common text obfuscations. Returns None when no
    plausible address is found. No address is ever fabricated or guessed.
    """
    candidates: list[str] = []

    try:
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.find_all("a", href=True):
            href = link["href"].strip()
            if href.lower().startswith("mailto:"):
                addr = href[7:].split("?", 1)[0].strip()
                if addr:
                    candidates.append(addr)
        text_source = soup.get_text(separator=" ", strip=True)
    except Exception:
        text_source = html

    # Plain and de-obfuscated text matches.
    for blob in (text_source, _deobfuscate(text_source)):
        candidates.extend(_EMAIL_RE.findall(blob))

    seen: set[str] = set()
    valid: list[str] = []
    for raw in candidates:
        addr = raw.strip().strip(".").lower()
        if addr in seen:
            continue
        seen.add(addr)
        if _looks_like_email(addr):
            valid.append(addr)

    if not valid:
        return None

    best = max(valid, key=lambda e: _score_email_candidate(e, name, university))
    # Require at least a weak signal (a .edu domain or a name match) before
    # trusting an address scraped off a page.
    if _score_email_candidate(best, name, university) <= 0:
        return None
    return best


def find_professor_email(
    profile_url: str,
    name: str = "",
    university: str = "",
    timeout: int = _REQUEST_TIMEOUT,
) -> Optional[str]:
    """Fetch a faculty/profile page and extract a published email, best-effort.

    Returns None on any network/parse failure or when no plausible address is
    found, so callers can fall back to manual entry.
    """
    url = (profile_url or "").strip()
    if not url or url.split("#", 1)[0] in ("", "http://", "https://"):
        return None
    if not _is_allowed_by_robots(url, _DEFAULT_USER_AGENT):
        return None
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _DEFAULT_USER_AGENT},
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException:
        return None

    email = extract_email_from_html(resp.text, name=name, university=university)
    if email:
        return email

    # Many faculty profiles hide the address one click away (a "Contact" or
    # "People"/"Directory" page). Follow one such link and try once more.
    follow = _find_contact_link(resp.text, url)
    if follow and follow != url and _is_allowed_by_robots(follow, _DEFAULT_USER_AGENT):
        try:
            r2 = requests.get(follow, headers={"User-Agent": _DEFAULT_USER_AGENT}, timeout=timeout)
            r2.raise_for_status()
            return extract_email_from_html(r2.text, name=name, university=university)
        except requests.exceptions.RequestException:
            return None
    return None


# Link text/href hints that usually lead to a page where an email is published.
_CONTACT_HINTS = ("contact", "people", "directory", "faculty", "email", "profile", "members", "staff")


def _find_contact_link(html: str, base_url: str) -> Optional[str]:
    """Return an absolute URL of the most likely contact/people page, or None."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "javascript:", "#")):
            continue
        haystack = (href + " " + a.get_text(" ", strip=True)).lower()
        if any(h in haystack for h in _CONTACT_HINTS):
            absolute = urljoin(base_url, href)
            if absolute.startswith(("http://", "https://")):
                return absolute
    return None


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

    # Opportunistically recover a real email from the same page when the
    # professor has none (e.g. discovered via the finder). Never overwrite an
    # existing real address, and never invent one.
    if _needs_email(prof.email):
        found = extract_email_from_html(
            response.text, name=prof.name, university=prof.university or "",
        )
        if found:
            prof.email = found
            logger.info("Recovered email for %s: %s", prof.name, found)

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


def _needs_email(value: Optional[str]) -> bool:
    """True when a professor has no real, sendable email yet."""
    email = (value or "").strip().lower()
    return not email or email.endswith(".placeholder")
