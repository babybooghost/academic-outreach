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
from urllib.parse import parse_qs, unquote, urljoin, urlparse

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


def _best_email_from_candidates(
    candidates: list[str], name: str, university: str
) -> Optional[str]:
    """Dedupe, validate, and pick the best-scoring candidate email (or None)."""
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
    # Require a weak signal (.edu domain or a name match) before trusting it.
    if _score_email_candidate(best, name, university) <= 0:
        return None
    return best


def extract_email_from_text(text: str, name: str = "", university: str = "") -> Optional[str]:
    """Pick the most likely professor email out of a plain-text blob (e.g. PDF text)."""
    candidates: list[str] = []
    for blob in (text or "", _deobfuscate(text or "")):
        candidates.extend(_EMAIL_RE.findall(blob))
    return _best_email_from_candidates(candidates, name, university)


_ARXIV_ID_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([\w.\-/]+?)(?:v\d+)?(?:\.pdf)?/?$", re.IGNORECASE)


def extract_email_from_arxiv(
    abs_url: str, name: str = "", university: str = "", timeout: int = _REQUEST_TIMEOUT
) -> Optional[str]:
    """Pull a corresponding-author email from an arXiv paper's PDF header.

    arXiv strips emails from the HTML abstract page but they're in the PDF's
    author block. Open-access, so this is a clean scrape. Best-effort: any
    network/parse failure returns None.
    """
    m = _ARXIV_ID_RE.search((abs_url or "").strip())
    if not m:
        return None
    pdf_url = f"https://arxiv.org/pdf/{m.group(1)}"
    try:
        resp = requests.get(pdf_url, headers={"User-Agent": _DEFAULT_USER_AGENT}, timeout=timeout)
        resp.raise_for_status()
    except requests.exceptions.RequestException:
        return None
    if len(resp.content) > 20 * 1024 * 1024:  # don't load huge PDFs into memory
        return None
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(resp.content))
        text = " ".join(
            (reader.pages[i].extract_text() or "") for i in range(min(2, len(reader.pages)))
        )
    except Exception:
        return None
    return extract_email_from_text(text, name=name, university=university)


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

    return _best_email_from_candidates(candidates, name, university)


def _search_faculty_page(name: str, university: str, timeout: int = _REQUEST_TIMEOUT) -> Optional[str]:
    """Best-effort: find a professor's faculty/lab page via a keyless web search
    (DuckDuckGo HTML). Returns the most plausible URL, or None. Used only when no
    profile URL is known, so the email scraper has somewhere to look.
    """
    name = (name or "").strip()
    if not name:
        return None
    query = " ".join(p for p in [name, university, "faculty"] if p).strip()
    # DuckDuckGo's HTML endpoint serves bot UAs an empty page; use a browser UA
    # for the search query only (the faculty page itself is fetched politely).
    browser_ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
    try:
        resp = requests.post(
            "https://html.duckduckgo.com/html/", data={"q": query},
            headers={"User-Agent": browser_ua}, timeout=timeout,
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException:
        return None

    tokens = _name_tokens(name)
    best_url, best_score = None, 0
    soup = BeautifulSoup(resp.text, "html.parser")
    for a in soup.select("a.result__a")[:10]:
        href = a.get("href") or ""
        # DDG wraps results in a redirect: /l/?uddg=<urlencoded target>
        if "uddg=" in href:
            try:
                href = unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
            except Exception:
                continue
        if not href.startswith("http"):
            continue
        low = (href + " " + a.get_text(" ")).lower()
        score = 0
        host = urlparse(href).netloc.lower()
        if host.endswith(".edu") or ".edu." in host or ".ac." in host:
            score += 4
        if any(h in low for h in _CONTACT_HINTS):
            score += 2
        score += sum(1 for t in tokens if t in low)
        if score > best_score:
            best_url, best_score = href, score
    # Require a minimal match so we don't scrape an unrelated page.
    return best_url if best_score >= 3 else None


def find_professor_email(
    profile_url: str,
    name: str = "",
    university: str = "",
    timeout: int = _REQUEST_TIMEOUT,
    allow_search: bool = False,
) -> Optional[str]:
    """Fetch a faculty/profile page and extract a published email, best-effort.

    When ``allow_search`` is set and no usable profile URL is given (or it yields
    nothing), fall back to finding the professor's page via a web search and
    scraping that. Returns None on any failure, so callers can prompt for manual
    entry. Never guesses an address.
    """
    url = (profile_url or "").strip()
    if not url or url.split("#", 1)[0] in ("", "http://", "https://"):
        if allow_search:
            searched = _search_faculty_page(name, university, timeout=timeout)
            if searched:
                found = find_professor_email(searched, name, university, timeout=timeout)
                if found:
                    return found
        return None

    # arXiv: the email lives in the PDF header, not the HTML abstract page.
    if "arxiv.org" in url.lower():
        return extract_email_from_arxiv(url, name=name, university=university, timeout=timeout)

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
            follow_email = extract_email_from_html(r2.text, name=name, university=university)
            if follow_email:
                return follow_email
        except requests.exceptions.RequestException:
            pass

    # The page we were given had nothing useful — optionally search for a better one.
    if allow_search:
        searched = _search_faculty_page(name, university, timeout=timeout)
        if searched and searched != url:
            found = find_professor_email(searched, name, university, timeout=timeout)
            if found:
                return found
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
