"""
Professor finder module for the Academic Outreach Email System.

Two discovery strategies using **free academic APIs** (no keys needed):
    1. OpenAlex API — searches published works and extracts authors + institutions
    2. Semantic Scholar API — searches papers for author info (backup)

Both return Professor dataclass instances ready for upsert into the database.
"""

from __future__ import annotations

import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

import requests

from app.models import Professor

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OPENALEX_BASE: str = "https://api.openalex.org"
_S2_BASE: str = "https://api.semanticscholar.org/graph/v1"

_HEADERS: Dict[str, str] = {
    "User-Agent": "AcademicOutreach/1.0 (mailto:outreach@example.com)",
    "Accept": "application/json",
}

_TIMEOUT: int = 20
_POLITE_DELAY: float = 0.3
# Optional: a Semantic Scholar API key lifts the (very low) shared rate limit.
_S2_KEY: str = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()


def _get_with_retry(
    url: str,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: int = _TIMEOUT,
    retries: int = 2,
    backoff: float = 1.2,
) -> requests.Response:
    """GET that retries on 429 / 5xx with backoff (honoring Retry-After).

    Returns the final Response (which may still be an error) or raises the last
    network exception. Keeps transient rate-limits/blips from killing a source.
    """
    resp = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < retries:
                    try:
                        wait = float(resp.headers.get("Retry-After", "") or (backoff * (attempt + 1)))
                    except ValueError:
                        wait = backoff * (attempt + 1)
                    time.sleep(min(wait, 5.0))
                    continue
            return resp
        except requests.RequestException:
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
                continue
            raise
    return resp  # type: ignore[return-value]


def _friendly_source_error(source: str, exc: Exception) -> str:
    """A human-readable, non-leaky warning when one source fails.

    Keeps raw exception reprs (e.g. ``('Connection aborted.',
    ConnectionResetError(104, ...))``) out of the UI — the full error is still
    logged. Reassures the user the other sources still ran.
    """
    if isinstance(exc, requests.exceptions.Timeout):
        reason = "timed out"
    elif isinstance(exc, requests.exceptions.ConnectionError):
        reason = "couldn't be reached"
    elif (isinstance(exc, requests.exceptions.HTTPError)
          and getattr(getattr(exc, "response", None), "status_code", None) == 429):
        return f"{source} is rate-limiting right now — the other sources still ran."
    else:
        reason = "returned an error"
    return f"{source} {reason} and was skipped — the other sources still ran."

# University names — use EXACT OpenAlex display names so filtering works.
_TOP_UNIVERSITIES: list[str] = [
    # --- Ivy League ---
    "Brown University",
    "Columbia University",
    "Cornell University",
    "Dartmouth College",
    "Harvard University",
    "Princeton University",
    "University of Pennsylvania",
    "Yale University",
    # --- Top US CS / Engineering ---
    "Massachusetts Institute of Technology",
    "Stanford University",
    "Carnegie Mellon University",
    "California Institute of Technology",
    "Georgia Institute of Technology",
    "University of Michigan",
    "University of Illinois Urbana-Champaign",
    "University of Washington",
    "University of Wisconsin–Madison",
    "University of Maryland, College Park",
    "Purdue University",
    "Johns Hopkins University",
    # --- UC System ---
    "University of California, Berkeley",
    "University of California, Los Angeles",
    "University of California, San Diego",
    "University of California, Santa Barbara",
    "University of California, Irvine",
    "University of California, Davis",
    "University of California, Santa Cruz",
    "University of California, Riverside",
    # --- Texas ---
    "University of Texas at Austin",
    "Texas A&M University",
    "University of Houston",
    "University of Texas at Dallas",
    "Rice University",
    "University of Texas at San Antonio",
    "Texas Tech University",
    "University of North Texas",
    "Southern Methodist University",
    "Baylor University",
    # --- Other Top US ---
    "University of Chicago",
    "Northwestern University",
    "New York University",
    "Duke University",
    "University of Southern California",
    "Boston University",
    "Ohio State University",
    "Pennsylvania State University",
    "University of Minnesota",
    "University of Virginia",
    "University of North Carolina at Chapel Hill",
    "University of Florida",
    "University of Colorado Boulder",
    "Arizona State University",
    "University of Arizona",
    "Northeastern University",
    "Georgetown University",
    "Emory University",
    "Vanderbilt University",
    "Washington University in St. Louis",
    "University of Notre Dame",
    "University of Rochester",
    "Case Western Reserve University",
    "Rutgers University",
    "University of Pittsburgh",
    "Indiana University",
    "University of Iowa",
    "University of Oregon",
    "Virginia Tech",
    "North Carolina State University",
    "University of Massachusetts Amherst",
    "Stony Brook University",
    "University of Utah",
    "Michigan State University",
    "University of Georgia",
    # --- Top International ---
    "University of Oxford",
    "University of Cambridge",
    "Imperial College London",
    "University College London",
    "London School of Economics and Political Science",
    "University of Edinburgh",
    "ETH Zurich",
    "École Polytechnique Fédérale de Lausanne",
    "University of Toronto",
    "University of Waterloo",
    "University of British Columbia",
    "McGill University",
    "National University of Singapore",
    "Nanyang Technological University",
    "Tsinghua University",
    "Peking University",
    "University of Tokyo",
    "Seoul National University",
    "Korea Advanced Institute of Science and Technology",
    "Technical University of Munich",
    "University of Melbourne",
    "University of Sydney",
    "Australian National University",
    "Tel Aviv University",
    "Technion – Israel Institute of Technology",
    "University of Amsterdam",
    "KU Leuven",
    "University of Hong Kong",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_name(raw: str) -> str:
    """Clean up a professor name string."""
    name = re.sub(r"^(Dr\.?|Prof\.?|Professor)\s+", "", raw.strip(), flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r",\s*(Ph\.?D\.?|M\.?D\.?|Jr\.?|Sr\.?|III|II).*$", "", name)
    return name


def _clean_institution(raw: str) -> str:
    """Reduce a raw affiliation string to the core institution name.

    Crossref/arXiv often hand back a full postal affiliation like
    ``"Fakultät für Informatik, Technische Universität München, 80290 München,
    Germany"``.  Pick the segment that actually names the institution (a
    university, then an institute/college/lab) and drop sub-departments and
    trailing postal/country segments, so faculty cards read cleanly.
    """
    if not raw:
        return ""
    clean = raw.strip()
    parts = [p.strip() for p in clean.split(",") if p.strip()]
    if len(parts) > 1:
        primary = ("universit", "polytechnic", "college", "academy", "académie")
        secondary = ("institut", "school", "laborator", "centre", "center", "hospital")
        match = next((p for p in parts if any(k in p.lower() for k in primary)), None)
        if match is None:
            match = next(
                (p for p in parts
                 if any(k in p.lower() for k in secondary)
                 and not p.lower().startswith(("school of", "department", "faculty", "fakultät", "dept"))),
                None,
            )
        if match is None:
            # No keyword hit: drop segments that look like postal codes / countries.
            candidates = [p for p in parts if not re.search(r"\d{4,}", p)]
            match = candidates[0] if candidates else parts[0]
        clean = match
    if len(clean) > 120:
        clean = clean[:120].rsplit(",", 1)[0]
    return clean.strip()


def _institution_matches(author_institutions: list[dict], target_inst_id: str) -> bool:
    """Check if any of the author's institutions match the target institution ID."""
    for inst in author_institutions:
        inst_id_raw = inst.get("id", "")
        if not inst_id_raw:
            continue
        inst_id = inst_id_raw.split("/")[-1].upper()
        if inst_id == target_inst_id.upper():
            return True
        # Also check lineage (parent institutions)
        for lineage_id in inst.get("lineage", []):
            if isinstance(lineage_id, str):
                lid = lineage_id.split("/")[-1].upper()
                if lid == target_inst_id.upper():
                    return True
    return False


# ---------------------------------------------------------------------------
# Institution ID resolution (cached per session)
# ---------------------------------------------------------------------------

_inst_id_cache: dict[str, Optional[str]] = {}


def _resolve_institution_id(name: str) -> Optional[str]:
    """Resolve a university name to an OpenAlex institution ID. Cached."""
    if name in _inst_id_cache:
        return _inst_id_cache[name]

    try:
        resp = _get_with_retry(
            f"{_OPENALEX_BASE}/institutions",
            params={"search": name, "per_page": 1, "filter": "type:education"},
            headers=_HEADERS,
            timeout=10,
        )
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            if results:
                oid = results[0]["id"].split("/")[-1]
                display = results[0].get("display_name", "")
                logger.info("Resolved '%s' -> %s (%s)", name, display, oid)
                _inst_id_cache[name] = oid
                return oid
    except Exception as exc:
        logger.warning("Failed to resolve institution '%s': %s", name, exc)

    _inst_id_cache[name] = None
    return None


# ---------------------------------------------------------------------------
# Strategy 1: OpenAlex (primary — free, unlimited, structured data)
# ---------------------------------------------------------------------------

def search_openalex_works(
    query: str,
    field: str = "",
    max_results: int = 30,
    year_from: int = 2018,
    university_filter: Optional[str] = None,
    _resolved_inst_id: Optional[str] = None,
) -> Tuple[list[Professor], list[str]]:
    """
    Search OpenAlex for published works and extract professor info.
    When university_filter is set, only authors AT that university are returned.
    """
    logger.info("Searching OpenAlex works for: %s (uni=%s)", query, university_filter)
    warnings: list[str] = []

    inst_id: Optional[str] = _resolved_inst_id

    params: dict[str, Any] = {
        "search": query,
        "per_page": min(max_results * 4, 200),
        "sort": "cited_by_count:desc",
        "filter": f"type:article,from_publication_date:{year_from}-01-01",
    }

    if university_filter:
        if not inst_id:
            inst_id = _resolve_institution_id(university_filter)
        if inst_id:
            params["filter"] += f",authorships.institutions.lineage:{inst_id}"
        else:
            warnings.append(f"Could not resolve '{university_filter}' — showing unfiltered results.")

    try:
        time.sleep(_POLITE_DELAY)
        resp = _get_with_retry(
            f"{_OPENALEX_BASE}/works",
            params=params,
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.error("OpenAlex works search failed: %s", exc)
        warnings.append(_friendly_source_error("OpenAlex", exc))
        return [], warnings

    professors: list[Professor] = []
    seen_names: set[str] = set()

    for work in data.get("results", []):
        if len(professors) >= max_results:
            break

        title = work.get("title", "")
        cited = work.get("cited_by_count", 0)
        year = work.get("publication_year", "")
        doi = work.get("doi", "")

        # When filtering by university, scan ALL authors in each paper
        # because the matching author may not be first in the list.
        for authorship in work.get("authorships", []):
            if len(professors) >= max_results:
                break

            author = authorship.get("author", {})
            name = _clean_name(author.get("display_name", ""))
            if not name or len(name.split()) < 2:
                continue
            if name.lower() in seen_names:
                continue

            # Get this author's institutions
            institutions = authorship.get("institutions", [])
            if not institutions:
                continue

            # When filtering by university, only include authors who are
            # actually AT that university (not co-authors from elsewhere).
            if inst_id:
                if not _institution_matches(institutions, inst_id):
                    continue
                # Use the filtered university name for display (not
                # necessarily institutions[0] which may be a different
                # primary affiliation)
                institution_name = university_filter or institutions[0].get("display_name", "")
                institution_country = institutions[0].get("country_code", "")
            else:
                institution_name = institutions[0].get("display_name", "")
                institution_country = institutions[0].get("country_code", "")

            if not institution_name:
                continue

            author_id = author.get("id", "")
            orcid = author.get("orcid", "")

            seen_names.add(name.lower())
            professors.append(Professor(
                name=name,
                title=None,
                email="",
                university=_clean_institution(institution_name),
                department="",
                field=field or query,
                profile_url=author_id if author_id else "",
                research_summary=f"Paper: \"{title}\" ({year}, {cited} citations)",
                notes=(
                    f"Source: OpenAlex | "
                    f"Country: {institution_country or '?'} | "
                    f"Citations: {cited}"
                    + (f" | ORCID: {orcid}" if orcid else "")
                    + (f" | DOI: {doi}" if doi else "")
                ),
                status="new",
            ))

    logger.info("Found %d professors from OpenAlex works", len(professors))
    return professors, warnings


def search_openalex_authors(
    query: str,
    field: str = "",
    max_results: int = 30,
    university_filter: Optional[str] = None,
    _resolved_inst_id: Optional[str] = None,
) -> Tuple[list[Professor], list[str]]:
    """
    Search OpenAlex author records directly.
    """
    logger.info("Searching OpenAlex authors for: %s (uni=%s)", query, university_filter)
    warnings: list[str] = []

    params: dict[str, Any] = {
        "search": query,
        "per_page": min(max_results, 50),
        "sort": "cited_by_count:desc",
    }

    if university_filter:
        inst_id = _resolved_inst_id or _resolve_institution_id(university_filter)
        if inst_id:
            params["filter"] = f"last_known_institutions.id:{_OPENALEX_BASE}/institutions/{inst_id}"

    try:
        time.sleep(_POLITE_DELAY)
        resp = _get_with_retry(
            f"{_OPENALEX_BASE}/authors",
            params=params,
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.error("OpenAlex author search failed: %s", exc)
        warnings.append(_friendly_source_error("OpenAlex", exc))
        return [], warnings

    professors: list[Professor] = []
    seen_names: set[str] = set()

    for author in data.get("results", []):
        if len(professors) >= max_results:
            break

        name = _clean_name(author.get("display_name", ""))
        if not name or len(name.split()) < 2:
            continue
        if name.lower() in seen_names:
            continue

        institution = author.get("last_known_institution") or {}
        institution_name = institution.get("display_name", "")
        institution_country = institution.get("country_code", "")

        works_count = author.get("works_count", 0)
        cited_by = author.get("cited_by_count", 0)
        orcid = author.get("orcid", "")
        author_id = author.get("id", "")

        concepts = author.get("x_concepts", [])[:5]
        concept_names = [c.get("display_name", "") for c in concepts if c.get("display_name")]

        seen_names.add(name.lower())
        professors.append(Professor(
            name=name,
            title=None,
            email="",
            university=_clean_institution(institution_name),
            department="",
            field=", ".join(concept_names[:3]) if concept_names else field or query,
            profile_url=author_id,
            research_summary=f"Research areas: {', '.join(concept_names[:5])}" if concept_names else "",
            notes=(
                f"Source: OpenAlex Author | "
                f"Papers: {works_count} | "
                f"Citations: {cited_by} | "
                f"Country: {institution_country or '?'}"
                + (f" | ORCID: {orcid}" if orcid else "")
            ),
            status="new",
        ))

    logger.info("Found %d professors from OpenAlex authors", len(professors))
    return professors, warnings


# ---------------------------------------------------------------------------
# Strategy 2: Semantic Scholar (backup — rate limited but good data)
# ---------------------------------------------------------------------------

def search_semantic_scholar(
    query: str,
    field: str = "",
    max_results: int = 20,
) -> Tuple[list[Professor], list[str]]:
    """Search Semantic Scholar papers and extract author info."""
    logger.info("Searching Semantic Scholar for: %s", query)
    warnings: list[str] = []

    s2_headers = {"User-Agent": _HEADERS["User-Agent"]}
    if _S2_KEY:
        s2_headers["x-api-key"] = _S2_KEY
    try:
        time.sleep(_POLITE_DELAY)
        resp = _get_with_retry(
            f"{_S2_BASE}/paper/search",
            params={
                "query": query,
                "fields": "title,authors,year,citationCount",
                "limit": min(max_results * 3, 100),
            },
            headers=s2_headers,
            timeout=_TIMEOUT,
            # Keyless S2 is reliably rate-limited, so don't burn time retrying it
            # (it runs in parallel anyway); retry hard only when a key makes
            # success likely.
            retries=3 if _S2_KEY else 0,
        )
        if resp.status_code == 429:
            warnings.append(
                "Semantic Scholar is rate-limiting right now — the other sources still ran. "
                "Set a SEMANTIC_SCHOLAR_API_KEY for higher limits."
            )
            return [], warnings
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.error("Semantic Scholar search failed: %s", exc)
        warnings.append(_friendly_source_error("Semantic Scholar", exc))
        return [], warnings

    professors: list[Professor] = []
    seen_names: set[str] = set()

    for paper in data.get("data", []):
        if len(professors) >= max_results:
            break
        title = paper.get("title", "")
        cited = paper.get("citationCount", 0)
        year = paper.get("year", "")

        for author in paper.get("authors", [])[:2]:
            if len(professors) >= max_results:
                break
            name = _clean_name(author.get("name", ""))
            author_id = author.get("authorId", "")
            if not name or len(name.split()) < 2 or name.lower() in seen_names:
                continue
            profile_url = f"https://www.semanticscholar.org/author/{author_id}" if author_id else ""
            seen_names.add(name.lower())
            professors.append(Professor(
                name=name, title=None, email="",
                university="", department="",
                field=field or query,
                profile_url=profile_url,
                research_summary=f"Paper: \"{title}\" ({year}, {cited} citations)" if title else "",
                notes=f"Source: Semantic Scholar | Citations: {cited} | Author ID: {author_id}",
                status="new",
            ))

    logger.info("Found %d professors from Semantic Scholar", len(professors))
    return professors, warnings


# ---------------------------------------------------------------------------
# Strategy 3: Crossref (huge open metadata — 150M+ records, no key)
# ---------------------------------------------------------------------------

_CROSSREF_BASE: str = "https://api.crossref.org"


def search_crossref(
    query: str,
    field: str = "",
    max_results: int = 20,
) -> Tuple[list[Professor], list[str]]:
    """Search Crossref for works and extract author + affiliation info."""
    logger.info("Searching Crossref for: %s", query)
    warnings: list[str] = []

    try:
        time.sleep(_POLITE_DELAY)
        resp = _get_with_retry(
            f"{_CROSSREF_BASE}/works",
            params={
                "query": query,
                "rows": min(max_results * 3, 100),
                "sort": "is-referenced-by-count",
                "order": "desc",
                "filter": "type:journal-article",
                "select": "title,author,is-referenced-by-count,DOI,published-print,published-online",
            },
            headers={**_HEADERS, "User-Agent": "AcademicOutreach/1.0 (mailto:outreach@example.com)"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.error("Crossref search failed: %s", exc)
        warnings.append(_friendly_source_error("Crossref", exc))
        return [], warnings

    professors: list[Professor] = []
    seen_names: set[str] = set()

    for item in data.get("message", {}).get("items", []):
        if len(professors) >= max_results:
            break

        title_list = item.get("title", [])
        title = title_list[0] if title_list else ""
        cited = item.get("is-referenced-by-count", 0)
        doi = item.get("DOI", "")

        # Get year from published-print or published-online
        pub_date = item.get("published-print") or item.get("published-online") or {}
        date_parts = pub_date.get("date-parts", [[]])
        year = date_parts[0][0] if date_parts and date_parts[0] else ""

        for author in item.get("author", [])[:3]:
            if len(professors) >= max_results:
                break

            given = author.get("given", "")
            family = author.get("family", "")
            if not given or not family:
                continue
            name = _clean_name(f"{given} {family}")
            if not name or len(name.split()) < 2 or name.lower() in seen_names:
                continue

            # Crossref sometimes has affiliation data
            affiliations = author.get("affiliation", [])
            institution = affiliations[0].get("name", "") if affiliations else ""
            orcid = author.get("ORCID", "")

            seen_names.add(name.lower())
            professors.append(Professor(
                name=name,
                title=None,
                email="",
                university=_clean_institution(institution),
                department="",
                field=field or query,
                profile_url=orcid if orcid else (f"https://doi.org/{doi}" if doi else ""),
                research_summary=f"Paper: \"{title}\" ({year}, {cited} citations)" if title else "",
                notes=(
                    f"Source: Crossref | "
                    f"Citations: {cited}"
                    + (f" | DOI: {doi}" if doi else "")
                    + (f" | ORCID: {orcid}" if orcid else "")
                ),
                status="new",
            ))

    logger.info("Found %d professors from Crossref", len(professors))
    return professors, warnings


# ---------------------------------------------------------------------------
# Journal-of-choice search (Crossref by journal name or ISSN)
# ---------------------------------------------------------------------------

_ISSN_RE = re.compile(r"^\d{4}-\d{3}[\dxX]$")


def _resolve_journal_issn(name: str) -> tuple[Optional[str], str]:
    """Resolve a journal name to its (ISSN, canonical title) via Crossref."""
    try:
        time.sleep(_POLITE_DELAY)
        resp = _get_with_retry(
            f"{_CROSSREF_BASE}/journals", params={"query": name, "rows": 1},
            headers={**_HEADERS, "User-Agent": "AcademicOutreach/1.0 (mailto:outreach@example.com)"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json().get("message", {}).get("items", [])
    except requests.RequestException as exc:
        logger.warning("Journal name resolution failed for %s: %s", name, exc)
        return None, name
    if not items:
        return None, name
    issns = items[0].get("ISSN", [])
    return (issns[0] if issns else None), items[0].get("title", name)


def search_journal(
    journal: str,
    field: str = "",
    max_results: int = 20,
) -> Tuple[list[Professor], list[str]]:
    """Find recent corresponding authors in a specific journal (name or ISSN).

    Resolves a journal name to its exact ISSN first (so "Physical Review X"
    doesn't fuzzy-match unrelated journals), then pulls its most recent papers.
    Uses Crossref's openly-licensed metadata, so it works for any journal —
    including paywalled ones (IEEE, Elsevier, Springer) — without scraping the
    publisher's site.
    """
    journal = (journal or "").strip()
    warnings: list[str] = []
    if not journal:
        return [], warnings

    issn = journal if _ISSN_RE.match(journal) else None
    if not issn:
        issn, _resolved = _resolve_journal_issn(journal)
        if not issn:
            warnings.append(f"Couldn't find a journal named '{journal}'. Try its ISSN instead.")
            return [], warnings

    params: dict[str, Any] = {
        "rows": min(max_results * 3, 100),
        "sort": "published",
        "order": "desc",
        "filter": f"type:journal-article,issn:{issn}",
        "select": "title,author,is-referenced-by-count,DOI,published-print,published-online,container-title",
    }

    try:
        time.sleep(_POLITE_DELAY)
        resp = _get_with_retry(
            f"{_CROSSREF_BASE}/works", params=params,
            headers={**_HEADERS, "User-Agent": "AcademicOutreach/1.0 (mailto:outreach@example.com)"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.error("Journal search failed for %s: %s", journal, exc)
        warnings.append(_friendly_source_error(f"The journal '{journal}'", exc))
        return [], warnings

    professors: list[Professor] = []
    seen_names: set[str] = set()
    for item in data.get("message", {}).get("items", []):
        if len(professors) >= max_results:
            break
        title = (item.get("title") or [""])[0]
        cited = item.get("is-referenced-by-count", 0)
        doi = item.get("DOI", "")
        container = (item.get("container-title") or [journal])[0]
        pub_date = item.get("published-print") or item.get("published-online") or {}
        date_parts = pub_date.get("date-parts", [[]])
        year = date_parts[0][0] if date_parts and date_parts[0] else ""

        # Corresponding author is usually first; take the leading authors.
        for author in item.get("author", [])[:3]:
            if len(professors) >= max_results:
                break
            given, family = author.get("given", ""), author.get("family", "")
            if not given or not family:
                continue
            name = _clean_name(f"{given} {family}")
            if not name or len(name.split()) < 2 or name.lower() in seen_names:
                continue
            affiliations = author.get("affiliation", [])
            institution = affiliations[0].get("name", "") if affiliations else ""
            orcid = author.get("ORCID", "")
            seen_names.add(name.lower())
            professors.append(Professor(
                name=name, title=None, email="",
                university=_clean_institution(institution), department="",
                field=field or container,
                profile_url=orcid if orcid else (f"https://doi.org/{doi}" if doi else ""),
                research_summary=f'Paper: "{title}" ({year}) in {container}' if title else f"Published in {container}",
                notes=(
                    f'Source: Journal "{container}" | Citations: {cited}'
                    + (f" | DOI: {doi}" if doi else "")
                    + (f" | ORCID: {orcid}" if orcid else "")
                ),
                status="new",
            ))

    logger.info("Found %d authors from journal '%s'", len(professors), journal)
    return professors, warnings


# ---------------------------------------------------------------------------
# Strategy 4: DBLP (computer science focused — free, no key)
# ---------------------------------------------------------------------------

_DBLP_BASE: str = "https://dblp.org/search/publ/api"


def search_dblp(
    query: str,
    field: str = "",
    max_results: int = 20,
) -> Tuple[list[Professor], list[str]]:
    """Search DBLP for computer science publications."""
    logger.info("Searching DBLP for: %s", query)
    warnings: list[str] = []

    try:
        time.sleep(_POLITE_DELAY)
        resp = _get_with_retry(
            _DBLP_BASE,
            params={
                "q": query,
                "h": min(max_results * 3, 100),
                "format": "json",
            },
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.error("DBLP search failed: %s", exc)
        warnings.append(_friendly_source_error("DBLP", exc))
        return [], warnings

    professors: list[Professor] = []
    seen_names: set[str] = set()

    hits = data.get("result", {}).get("hits", {}).get("hit", [])
    for hit in hits:
        if len(professors) >= max_results:
            break

        info = hit.get("info", {})
        title = info.get("title", "")
        year = info.get("year", "")
        venue = info.get("venue", "")
        url = info.get("ee", "") or info.get("url", "")

        # Authors can be a single dict or a list
        authors_raw = info.get("authors", {}).get("author", [])
        if isinstance(authors_raw, dict):
            authors_raw = [authors_raw]

        for author_entry in authors_raw[:3]:
            if len(professors) >= max_results:
                break

            # DBLP author can be a string or dict with "text"
            if isinstance(author_entry, str):
                name = _clean_name(author_entry)
                pid = ""
            else:
                name = _clean_name(author_entry.get("text", author_entry.get("@text", "")))
                pid = author_entry.get("@pid", "")

            if not name or len(name.split()) < 2 or name.lower() in seen_names:
                continue

            profile_url = f"https://dblp.org/pid/{pid}" if pid else ""

            seen_names.add(name.lower())
            professors.append(Professor(
                name=name,
                title=None,
                email="",
                university="",
                department="Computer Science",
                field=field or query,
                profile_url=profile_url,
                research_summary=f"Paper: \"{title}\" ({year})" + (f" @ {venue}" if venue else ""),
                notes=f"Source: DBLP | Year: {year}" + (f" | Venue: {venue}" if venue else ""),
                status="new",
            ))

    logger.info("Found %d professors from DBLP", len(professors))
    return professors, warnings


# ---------------------------------------------------------------------------
# Strategy 5: arXiv (preprints — physics, CS, math, quant, econ)
# ---------------------------------------------------------------------------

_ARXIV_BASE: str = "http://export.arxiv.org/api/query"


def search_arxiv(
    query: str,
    field: str = "",
    max_results: int = 20,
    category: str = "",
) -> Tuple[list[Professor], list[str]]:
    """Search arXiv for preprints and extract author info.

    With a ``category`` (e.g. ``cs.LG``, ``math.CO``, ``quant-ph``) it browses
    that sub-field's most recent submissions; otherwise it searches by topic.
    """
    category = (category or "").strip()
    logger.info("Searching arXiv for: %s%s", query, f" [cat:{category}]" if category else "")
    warnings: list[str] = []

    if category:
        search_query = f"cat:{category}" + (f" AND all:{query}" if query else "")
        sort_by = "submittedDate"
    else:
        search_query = f"all:{query}"
        sort_by = "relevance"

    try:
        time.sleep(_POLITE_DELAY)
        resp = _get_with_retry(
            _ARXIV_BASE,
            params={
                "search_query": search_query,
                "start": 0,
                "max_results": min(max_results * 3, 50),
                "sortBy": sort_by,
                "sortOrder": "descending",
            },
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error("arXiv search failed: %s", exc)
        warnings.append(_friendly_source_error("arXiv", exc))
        return [], warnings

    # arXiv returns Atom XML — parse it
    professors: list[Professor] = []
    seen_names: set[str] = set()

    try:
        import xml.etree.ElementTree as ET
        ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
        root = ET.fromstring(resp.text)

        for entry in root.findall("atom:entry", ns):
            if len(professors) >= max_results:
                break

            title_el = entry.find("atom:title", ns)
            title = title_el.text.strip().replace("\n", " ") if title_el is not None else ""

            # Published date
            published_el = entry.find("atom:published", ns)
            year = published_el.text[:4] if published_el is not None else ""

            # arXiv ID link
            id_el = entry.find("atom:id", ns)
            arxiv_url = id_el.text.strip() if id_el is not None else ""

            # Categories
            categories = [c.get("term", "") for c in entry.findall("atom:category", ns)]
            primary_cat = categories[0] if categories else ""

            # Affiliation from arxiv:affiliation
            for author_el in entry.findall("atom:author", ns):
                if len(professors) >= max_results:
                    break

                name_el = author_el.find("atom:name", ns)
                if name_el is None:
                    continue
                name = _clean_name(name_el.text.strip())
                if not name or len(name.split()) < 2 or name.lower() in seen_names:
                    continue

                # arXiv sometimes includes affiliation
                aff_el = author_el.find("arxiv:affiliation", ns)
                affiliation = aff_el.text.strip() if aff_el is not None else ""

                seen_names.add(name.lower())
                professors.append(Professor(
                    name=name,
                    title=None,
                    email="",
                    university=_clean_institution(affiliation),
                    department="",
                    field=field or primary_cat or query,
                    profile_url=arxiv_url,
                    research_summary=f"Paper: \"{title}\" ({year})",
                    notes=f"Source: arXiv | Year: {year} | Category: {primary_cat}",
                    status="new",
                ))
    except Exception as exc:
        logger.warning("arXiv XML parsing failed: %s", exc)
        warnings.append(f"arXiv parse error: {exc}")

    logger.info("Found %d professors from arXiv", len(professors))
    return professors, warnings


# ---------------------------------------------------------------------------
# Combined search
# ---------------------------------------------------------------------------

# All discovery sources the finder can draw on (all free, no API keys).
ALL_SOURCES: tuple[str, ...] = ("openalex", "crossref", "dblp", "arxiv", "semantic_scholar")


def find_professors(
    query: str = "",
    universities: Optional[list[str]] = None,
    field: str = "",
    department: str = "Computer Science",
    max_scholar_results: int = 30,
    custom_urls: Optional[dict[str, str]] = None,
    sources: Optional[list[str]] = None,
    journals: Optional[list[str]] = None,
    arxiv_categories: Optional[list[str]] = None,
) -> Tuple[list[Professor], list[str]]:
    """Run discovery across the selected academic databases, concurrently.

    ``sources`` selects which databases to query (defaults to all). Every chosen
    source runs regardless of the university filter — OpenAlex uses true
    institution filtering, while the others have the university names folded into
    their query so results lean toward those schools. ``journals`` adds a search
    of one or more specific journals (by name or ISSN) via Crossref. All calls
    run in parallel so total latency is roughly the slowest one, not their sum.
    """
    all_professors: list[Professor] = []
    warnings: list[str] = []

    journals = [j.strip() for j in (journals or []) if j and j.strip()]
    arxiv_categories = [c.strip() for c in (arxiv_categories or []) if c and c.strip()]
    if not query and not journals and not arxiv_categories:
        warnings.append("Enter a search query, a journal, or an arXiv category to find professors.")
        return [], warnings

    chosen = [s for s in (sources or ALL_SOURCES) if s in ALL_SOURCES] or list(ALL_SOURCES)
    # Non-OpenAlex sources can't filter by institution, so bias their query text
    # toward the requested universities instead of dropping them entirely.
    uni_suffix = (" " + " ".join(universities)) if universities else ""

    # Build the list of independent source calls, then fan them out on threads.
    tasks: list[tuple[str, Any]] = []  # (label, callable)

    # Topic-based sources only run when there's a research-topic query.
    if query and "openalex" in chosen:
        if universities:
            per_uni = max(max_scholar_results // len(universities), 8)
            for uni in universities:
                inst_id = _resolve_institution_id(uni)
                if not inst_id:
                    warnings.append(f"Could not resolve '{uni}' — skipped on OpenAlex.")
                    continue
                tasks.append((f"OpenAlex works @ {uni}", lambda u=uni, i=inst_id, n=per_uni: search_openalex_works(
                    query=query, field=field, max_results=n, university_filter=u, _resolved_inst_id=i)))
                tasks.append((f"OpenAlex authors @ {uni}", lambda u=uni, i=inst_id, n=per_uni: search_openalex_authors(
                    query=query, field=field, max_results=max(n // 2, 5), university_filter=u, _resolved_inst_id=i)))
        else:
            tasks.append(("OpenAlex works", lambda: search_openalex_works(
                query=query, field=field, max_results=max_scholar_results)))
            tasks.append(("OpenAlex authors", lambda: search_openalex_authors(
                query=query, field=field, max_results=max_scholar_results // 2)))

    if query and "crossref" in chosen:
        tasks.append(("Crossref", lambda: search_crossref(
            query=query + uni_suffix, field=field, max_results=min(max_scholar_results, 15))))
    if query and "dblp" in chosen:
        tasks.append(("DBLP", lambda: search_dblp(
            query=query + uni_suffix, field=field or "Computer Science", max_results=min(max_scholar_results, 12))))
    if query and "arxiv" in chosen:
        tasks.append(("arXiv", lambda: search_arxiv(
            query=query + uni_suffix, field=field, max_results=min(max_scholar_results, 12))))
    if query and "semantic_scholar" in chosen:
        tasks.append(("Semantic Scholar", lambda: search_semantic_scholar(
            query=query + uni_suffix, field=field, max_results=min(max_scholar_results, 15))))

    # Journal-of-choice: one task per requested journal (name or ISSN).
    per_journal = max(max_scholar_results // len(journals), 8) if journals else 0
    for jrnl in journals:
        tasks.append((f'Journal "{jrnl}"', lambda j=jrnl, n=per_journal: search_journal(
            j, field=field, max_results=n)))

    # arXiv sub-field browsing: one task per requested category (cs.LG, math.CO…).
    per_cat = max(max_scholar_results // len(arxiv_categories), 8) if arxiv_categories else 0
    for cat in arxiv_categories:
        tasks.append((f"arXiv {cat}", lambda c=cat, n=per_cat: search_arxiv(
            query=query, field=field, max_results=n, category=c)))

    # Fan out: every source call is independent I/O, so run them concurrently.
    with ThreadPoolExecutor(max_workers=min(8, len(tasks)) or 1) as pool:
        futures = {pool.submit(fn): label for label, fn in tasks}
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                profs, warns = fut.result()
                all_professors.extend(profs)
                warnings.extend(warns)
            except Exception as exc:
                warnings.append(_friendly_source_error(label, exc))
                logger.warning("Finder source %s failed: %s", label, exc)

    # Deduplicate by name
    seen: set[str] = set()
    unique: list[Professor] = []
    for prof in all_professors:
        key = prof.name.lower().strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(prof)

    # Sort by citation count
    def _cit(p: Professor) -> int:
        m = re.search(r"Citations:\s*(\d+)", p.notes or "")
        return int(m.group(1)) if m else 0
    unique.sort(key=_cit, reverse=True)

    # Collapse duplicate warnings (e.g. two OpenAlex sub-calls failing the same
    # way) while preserving order.
    warnings = list(dict.fromkeys(warnings))

    return unique[:max_scholar_results], warnings


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def list_known_universities() -> list[str]:
    """Return list of top universities for filtering."""
    return sorted(set(_TOP_UNIVERSITIES))
