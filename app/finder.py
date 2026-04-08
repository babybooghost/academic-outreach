"""
Professor finder module for the Academic Outreach Email System.

Two discovery strategies:
    1. Faculty directory scraper — scrapes university department pages
    2. Google Scholar scraper — finds professors publishing in target fields

Both strategies extract professor data and return Professor dataclass
instances ready for upsert into the database.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from app.models import Professor

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_HEADERS: Dict[str, str] = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_TIMEOUT: int = 15
_POLITE_DELAY: float = 2.0

_EMAIL_RE: re.Pattern = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
)

_TITLE_PATTERNS: list[re.Pattern] = [
    re.compile(r"(Assistant|Associate|Full|Adjunct|Research|Clinical|Emeritus)\s+Professor", re.IGNORECASE),
    re.compile(r"Professor\s+of\s+\w+", re.IGNORECASE),
    re.compile(r"Lecturer|Instructor|Fellow|Reader", re.IGNORECASE),
]

# Well-known CS department URLs for top universities
_KNOWN_DIRECTORIES: Dict[str, str] = {
    "MIT": "https://www.eecs.mit.edu/people/?fwp_role=faculty",
    "Stanford": "https://cs.stanford.edu/people/faculty",
    "UC Berkeley": "https://www2.eecs.berkeley.edu/Faculty/Lists/list.html",
    "CMU": "https://www.cs.cmu.edu/people/faculty",
    "Georgia Tech": "https://www.cc.gatech.edu/people/faculty",
    "Princeton": "https://www.cs.princeton.edu/people/faculty",
    "Cornell": "https://www.cs.cornell.edu/people/faculty",
    "Columbia": "https://www.cs.columbia.edu/people/faculty/",
    "Michigan": "https://cse.engin.umich.edu/people/faculty/",
    "UChicago": "https://cs.uchicago.edu/people/?position=faculty",
    "Harvard": "https://seas.harvard.edu/computer-science/people?role=Faculty",
    "Yale": "https://cpsc.yale.edu/people/faculty",
    "UPenn": "https://www.cis.upenn.edu/people/faculty/",
    "Northwestern": "https://www.cs.northwestern.edu/people/faculty/",
    "NYU": "https://cs.nyu.edu/people/faculty.html",
    "UCLA": "https://www.cs.ucla.edu/people/faculty/",
    "UIUC": "https://cs.illinois.edu/about/people/faculty",
    "UW": "https://www.cs.washington.edu/people/faculty",
    "Caltech": "https://www.cms.caltech.edu/people?type=faculty",
    "Duke": "https://cs.duke.edu/people/faculty",
}


# ---------------------------------------------------------------------------
# Helper: fetch page
# ---------------------------------------------------------------------------

def _fetch_page(url: str, delay: float = _POLITE_DELAY) -> Optional[BeautifulSoup]:
    """Fetch a URL and return parsed BeautifulSoup, or None on error."""
    try:
        time.sleep(delay)
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return None


def _extract_emails(text: str) -> list[str]:
    """Extract email addresses from text."""
    return list(set(_EMAIL_RE.findall(text)))


def _extract_title(text: str) -> Optional[str]:
    """Try to extract an academic title from text."""
    for pattern in _TITLE_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0).strip()
    return None


def _clean_name(raw: str) -> str:
    """Clean up a professor name string."""
    # Remove titles, extra whitespace
    name = re.sub(r"^(Dr\.?|Prof\.?|Professor)\s+", "", raw.strip(), flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip()
    # Remove any trailing credentials
    name = re.sub(r",\s*(Ph\.?D\.?|M\.?D\.?|Jr\.?|Sr\.?|III|II).*$", "", name)
    return name


# ---------------------------------------------------------------------------
# Strategy 1: Faculty Directory Scraper
# ---------------------------------------------------------------------------

def scrape_faculty_directory(
    url: str,
    university: str,
    department: str = "Computer Science",
    field: str = "",
) -> list[Professor]:
    """
    Scrape a university faculty directory page for professor info.

    Returns a list of Professor objects with whatever data could be extracted.
    """
    logger.info("Scraping faculty directory: %s (%s)", university, url)
    soup = _fetch_page(url)
    if soup is None:
        return []

    professors: list[Professor] = []
    page_text = soup.get_text(separator=" ")

    # Strategy: find links/cards that look like faculty profiles
    # Look for common patterns: divs with person cards, list items, table rows
    candidates: list[Tag] = []

    # Try common CSS class patterns for faculty listings
    for selector in [
        "div.person", "div.faculty", "div.people-card", "div.profile",
        "div.views-row", "div.faculty-member", "div.card",
        "li.person", "li.faculty", "tr.faculty",
        "article.person", "article.faculty",
        "[class*='faculty']", "[class*='person']", "[class*='profile']",
        "[class*='member']", "[class*='people']",
    ]:
        found = soup.select(selector)
        if found:
            candidates = found
            break

    # Fallback: look for links within the main content area
    if not candidates:
        main = soup.find("main") or soup.find("div", {"id": "content"}) or soup.find("div", {"role": "main"}) or soup
        # Find all links that might be professor profile links
        links = main.find_all("a", href=True)
        for link in links:
            text = link.get_text(strip=True)
            # Heuristic: professor names are 2-4 words, link text isn't too long
            words = text.split()
            if 2 <= len(words) <= 5 and len(text) < 60:
                if not any(skip in text.lower() for skip in [
                    "home", "about", "contact", "news", "events", "research",
                    "courses", "apply", "admissions", "back", "more", "view",
                    "read", "learn", "click", "here", "all", "department",
                ]):
                    candidates.append(link)

    for card in candidates:
        prof = _parse_faculty_card(card, university, department, field, url)
        if prof and prof.name and len(prof.name.split()) >= 2:
            professors.append(prof)

    logger.info("Found %d professors from %s", len(professors), university)
    return professors


def _parse_faculty_card(
    card: Tag,
    university: str,
    department: str,
    field: str,
    base_url: str,
) -> Optional[Professor]:
    """Parse a single faculty card/element into a Professor."""
    text = card.get_text(separator=" ", strip=True)
    if not text or len(text) < 3:
        return None

    # Try to find name
    name = ""
    name_tag = card.find(["h2", "h3", "h4", "a", "strong", "b"])
    if name_tag:
        name = _clean_name(name_tag.get_text(strip=True))
    elif card.name == "a":
        name = _clean_name(card.get_text(strip=True))

    if not name or len(name) < 3:
        return None

    # Try to find email
    emails = _extract_emails(text)
    email = emails[0] if emails else ""

    # If no email in card text, check for mailto links
    if not email:
        mailto = card.find("a", href=re.compile(r"^mailto:"))
        if mailto:
            email = mailto["href"].replace("mailto:", "").split("?")[0].strip()

    # Try to find title
    title = _extract_title(text)

    # Try to find profile URL
    profile_url = ""
    link = card.find("a", href=True) if card.name != "a" else card
    if link and link.get("href"):
        href = link["href"]
        if href.startswith("http"):
            profile_url = href
        elif href.startswith("/"):
            profile_url = urljoin(base_url, href)

    # Try to find research interests / field
    detected_field = field
    for keyword_tag in card.find_all(["span", "p", "div"]):
        kt = keyword_tag.get_text(strip=True).lower()
        if any(kw in kt for kw in ["interest", "research", "area", "focus"]):
            detected_field = keyword_tag.get_text(strip=True)[:100]
            break

    return Professor(
        name=name,
        title=title,
        email=email,
        university=university,
        department=department,
        field=detected_field or field or "Computer Science",
        profile_url=profile_url,
        status="new",
    )


def find_from_directories(
    universities: list[str],
    department: str = "Computer Science",
    field: str = "",
    custom_urls: Optional[dict[str, str]] = None,
) -> Tuple[list[Professor], list[str]]:
    """
    Scrape faculty directories for multiple universities.

    Parameters
    ----------
    universities : list of str
        University names (e.g. ["MIT", "Stanford"]).
    department : str
        Department name for context.
    field : str
        Research field to tag professors with.
    custom_urls : dict, optional
        Map of university name -> faculty page URL (overrides built-in).

    Returns
    -------
    (professors, warnings) : tuple
    """
    all_urls = dict(_KNOWN_DIRECTORIES)
    if custom_urls:
        all_urls.update(custom_urls)

    professors: list[Professor] = []
    warnings: list[str] = []

    for uni in universities:
        url = all_urls.get(uni)
        if not url:
            warnings.append(f"No known directory URL for '{uni}'. Use --url to provide one.")
            continue

        try:
            found = scrape_faculty_directory(url, uni, department, field)
            professors.extend(found)
        except Exception as exc:
            warnings.append(f"Error scraping {uni}: {exc}")
            logger.error("Error scraping %s: %s", uni, exc)

    return professors, warnings


# ---------------------------------------------------------------------------
# Strategy 2: Google Scholar Scraper
# ---------------------------------------------------------------------------

def search_scholar(
    query: str,
    field: str = "",
    max_results: int = 20,
) -> list[Professor]:
    """
    Search Google Scholar for professors publishing in a field.

    Extracts author profiles from search results.

    Parameters
    ----------
    query : str
        Search query (e.g. "blockchain fintech").
    field : str
        Field to tag professors with.
    max_results : int
        Maximum number of professors to return.

    Returns
    -------
    list of Professor objects.
    """
    logger.info("Searching Google Scholar for: %s", query)
    professors: list[Professor] = []
    seen_names: set[str] = set()

    # Search Google Scholar author profiles
    base_url = "https://scholar.google.com/citations"
    params = {
        "view_op": "search_authors",
        "mauthors": query,
        "hl": "en",
    }

    try:
        time.sleep(_POLITE_DELAY)
        resp = requests.get(
            base_url, params=params, headers=_HEADERS, timeout=_TIMEOUT
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        logger.error("Google Scholar search failed: %s", exc)
        return []

    # Parse author cards
    author_cards = soup.select("div.gsc_1usr")
    if not author_cards:
        # Fallback selectors
        author_cards = soup.select("div[class*='gs_ai']") or soup.select("div.gs_ai_t")

    for card in author_cards[:max_results]:
        prof = _parse_scholar_card(card, field)
        if prof and prof.name not in seen_names:
            seen_names.add(prof.name)
            professors.append(prof)

    # If author search didn't work, try regular article search
    if not professors:
        professors = _search_scholar_articles(query, field, max_results)

    logger.info("Found %d professors from Google Scholar", len(professors))
    return professors


def _parse_scholar_card(card: Tag, field: str) -> Optional[Professor]:
    """Parse a Google Scholar author card."""
    # Author name
    name_tag = card.find("h3") or card.find(class_=re.compile(r"gs_ai_name|gsc_1usr_name"))
    if not name_tag:
        name_link = card.find("a")
        if name_link:
            name_tag = name_link

    if not name_tag:
        return None

    name = _clean_name(name_tag.get_text(strip=True))
    if not name or len(name.split()) < 2:
        return None

    # Profile URL
    profile_url = ""
    link = name_tag.find("a") if name_tag.name != "a" else name_tag
    if link and link.get("href"):
        href = link["href"]
        if href.startswith("/"):
            profile_url = f"https://scholar.google.com{href}"
        else:
            profile_url = href

    # Affiliation (university)
    university = ""
    aff_tag = card.find(class_=re.compile(r"gs_ai_aff|gsc_1usr_aff"))
    if aff_tag:
        university = aff_tag.get_text(strip=True)
    if not university:
        aff_tag = card.find("div", class_=re.compile(r"aff"))
        if aff_tag:
            university = aff_tag.get_text(strip=True)

    # Email domain hint
    email_tag = card.find(class_=re.compile(r"gs_ai_eml|gsc_1usr_eml"))
    email_hint = ""
    if email_tag:
        email_hint = email_tag.get_text(strip=True)

    # Research interests
    interests: list[str] = []
    int_tags = card.find_all(class_=re.compile(r"gs_ai_one_int|gsc_1usr_int"))
    for tag in int_tags:
        interests.append(tag.get_text(strip=True))
    if not interests:
        int_div = card.find("div", class_=re.compile(r"int"))
        if int_div:
            for a in int_div.find_all("a"):
                interests.append(a.get_text(strip=True))

    detected_field = ", ".join(interests[:3]) if interests else field

    return Professor(
        name=name,
        title=None,
        email="",  # Scholar doesn't show emails directly
        university=_clean_university(university),
        department="",
        field=detected_field or field,
        profile_url=profile_url,
        research_summary=f"Google Scholar interests: {', '.join(interests)}" if interests else "",
        notes=f"Email hint: {email_hint}" if email_hint else "Email: needs manual lookup",
        status="new",
    )


def _search_scholar_articles(query: str, field: str, max_results: int) -> list[Professor]:
    """Fallback: search Google Scholar articles and extract author info."""
    logger.info("Falling back to article search for: %s", query)
    professors: list[Professor] = []
    seen_names: set[str] = set()

    search_url = f"https://scholar.google.com/scholar?q={quote_plus(query)}&hl=en"

    try:
        time.sleep(_POLITE_DELAY)
        resp = requests.get(search_url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        logger.error("Scholar article search failed: %s", exc)
        return []

    # Parse article results for author names
    results = soup.select("div.gs_ri") or soup.select("div.gs_r")
    for result in results[:max_results * 2]:
        # Author info is usually in gs_a div
        author_div = result.find("div", class_="gs_a")
        if not author_div:
            continue

        author_text = author_div.get_text(strip=True)
        # Format: "Author1, Author2 - University - Journal, Year"
        parts = author_text.split(" - ")
        if not parts:
            continue

        author_names = parts[0].split(",")
        university = parts[1].strip() if len(parts) > 1 else ""

        # Article title for research context
        title_tag = result.find("h3")
        article_title = title_tag.get_text(strip=True) if title_tag else ""

        for author_raw in author_names[:2]:  # First 2 authors only
            name = _clean_name(author_raw.strip().replace("\xa0", " "))
            if name and len(name.split()) >= 2 and name not in seen_names:
                seen_names.add(name)
                professors.append(Professor(
                    name=name,
                    university=_clean_university(university),
                    department="",
                    field=field or "Research",
                    research_summary=f"Recent paper: {article_title}" if article_title else "",
                    notes="Email: needs manual lookup (found via Scholar article)",
                    status="new",
                ))

            if len(professors) >= max_results:
                break
        if len(professors) >= max_results:
            break

    return professors


def _clean_university(raw: str) -> str:
    """Clean up a university/affiliation string."""
    if not raw:
        return ""
    # Remove common prefixes/suffixes
    clean = raw.strip()
    # Truncate very long affiliations
    if len(clean) > 80:
        clean = clean[:80].rsplit(",", 1)[0]
    return clean


# ---------------------------------------------------------------------------
# Combined search
# ---------------------------------------------------------------------------

def find_professors(
    query: str = "",
    universities: Optional[list[str]] = None,
    field: str = "",
    department: str = "Computer Science",
    max_scholar_results: int = 20,
    custom_urls: Optional[dict[str, str]] = None,
) -> Tuple[list[Professor], list[str]]:
    """
    Run both discovery strategies and combine results.

    Parameters
    ----------
    query : str
        Search query for Google Scholar (e.g. "blockchain fintech AI").
    universities : list of str, optional
        Universities to scrape faculty directories.
    field : str
        Research field to filter/tag.
    department : str
        Department name for directory scraping.
    max_scholar_results : int
        Max professors from Scholar search.
    custom_urls : dict, optional
        Custom faculty directory URLs.

    Returns
    -------
    (professors, warnings) : tuple
    """
    all_professors: list[Professor] = []
    warnings: list[str] = []

    # Strategy 1: Faculty directories
    if universities:
        dir_profs, dir_warnings = find_from_directories(
            universities, department, field, custom_urls
        )
        all_professors.extend(dir_profs)
        warnings.extend(dir_warnings)

    # Strategy 2: Google Scholar
    if query:
        scholar_profs = search_scholar(query, field, max_scholar_results)
        all_professors.extend(scholar_profs)

    # Deduplicate by name (case-insensitive)
    seen: set[str] = set()
    unique: list[Professor] = []
    for prof in all_professors:
        key = prof.name.lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(prof)

    return unique, warnings


# ---------------------------------------------------------------------------
# Utility: list known universities
# ---------------------------------------------------------------------------

def list_known_universities() -> list[str]:
    """Return list of universities with known directory URLs."""
    return sorted(_KNOWN_DIRECTORIES.keys())
