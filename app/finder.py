"""
Professor finder module for the Academic Outreach Email System.

Two discovery strategies using **free academic APIs** (no keys needed):
    1. OpenAlex API — searches published works and extracts authors + institutions
    2. Semantic Scholar API — searches papers for author info (backup)

Both return Professor dataclass instances ready for upsert into the database.
"""

from __future__ import annotations

import logging
import re
import time
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
_POLITE_DELAY: float = 0.3  # OpenAlex is generous, minimal delay needed

# Top universities for filtering
_TOP_UNIVERSITIES: list[str] = [
    "MIT", "Stanford University", "UC Berkeley", "Carnegie Mellon University",
    "Georgia Institute of Technology", "Princeton University", "Cornell University",
    "Columbia University", "University of Michigan", "University of Chicago",
    "Harvard University", "Yale University", "University of Pennsylvania",
    "Northwestern University", "New York University", "UCLA",
    "University of Illinois Urbana-Champaign", "University of Washington",
    "California Institute of Technology", "Duke University",
    "University of Oxford", "University of Cambridge", "ETH Zurich",
    "University of Toronto", "National University of Singapore",
    "Tsinghua University", "Peking University", "University of Tokyo",
    "Imperial College London", "University College London",
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
    """Clean up institution name."""
    if not raw:
        return ""
    clean = raw.strip()
    if len(clean) > 100:
        clean = clean[:100].rsplit(",", 1)[0]
    return clean


# ---------------------------------------------------------------------------
# Strategy 1: OpenAlex (primary — free, unlimited, structured data)
# ---------------------------------------------------------------------------

def _resolve_institution_id(name: str) -> Optional[str]:
    """Resolve a university name to an OpenAlex institution ID."""
    try:
        resp = requests.get(
            f"{_OPENALEX_BASE}/institutions",
            params={"search": name, "per_page": 1},
            headers=_HEADERS,
            timeout=10,
        )
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            if results:
                oid = results[0]["id"].split("/")[-1]
                logger.info("Resolved '%s' -> %s (%s)", name, results[0]["display_name"], oid)
                return oid
    except Exception as exc:
        logger.warning("Failed to resolve institution '%s': %s", name, exc)
    return None


def search_openalex_works(
    query: str,
    field: str = "",
    max_results: int = 30,
    min_citations: int = 0,
    year_from: int = 2018,
    university_filter: Optional[str] = None,
) -> Tuple[list[Professor], list[str]]:
    """
    Search OpenAlex for published works and extract professor info.

    Returns (professors, warnings).
    """
    logger.info("Searching OpenAlex works for: %s", query)
    warnings: list[str] = []

    params: dict[str, Any] = {
        "search": query,
        "per_page": min(max_results * 3, 200),
        "sort": "cited_by_count:desc",
        "filter": f"type:article,from_publication_date:{year_from}-01-01",
    }

    if university_filter:
        inst_id = _resolve_institution_id(university_filter)
        if inst_id:
            params["filter"] += f",authorships.institutions.lineage:{inst_id}"
        else:
            warnings.append(f"Could not find institution ID for '{university_filter}'.")

    try:
        time.sleep(_POLITE_DELAY)
        resp = requests.get(
            f"{_OPENALEX_BASE}/works",
            params=params,
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.error("OpenAlex works search failed: %s", exc)
        warnings.append(f"OpenAlex API error: {exc}")
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

        for authorship in work.get("authorships", [])[:3]:  # Top 3 authors per paper
            if len(professors) >= max_results:
                break

            author = authorship.get("author", {})
            name = _clean_name(author.get("display_name", ""))

            if not name or len(name.split()) < 2:
                continue
            if name.lower() in seen_names:
                continue

            # Get institution
            institutions = authorship.get("institutions", [])
            institution_name = ""
            institution_country = ""
            if institutions:
                institution_name = institutions[0].get("display_name", "")
                institution_country = institutions[0].get("country_code", "")

            # Skip if no institution (probably not a professor)
            if not institution_name:
                continue

            # Get OpenAlex author ID for profile URL
            author_id = author.get("id", "")
            profile_url = author_id if author_id else ""

            # Get ORCID if available
            orcid = author.get("orcid", "")

            seen_names.add(name.lower())
            professors.append(Professor(
                name=name,
                title=None,
                email="",  # OpenAlex doesn't expose emails
                university=_clean_institution(institution_name),
                department="",
                field=field or query,
                profile_url=profile_url,
                research_summary=f"Paper: \"{title}\" ({year}, {cited} citations)",
                notes=(
                    f"Source: OpenAlex | "
                    f"Country: {institution_country or '?'} | "
                    f"Citations: {cited} | "
                    + (f"ORCID: {orcid} | " if orcid else "")
                    + (f"DOI: {doi}" if doi else "")
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
) -> Tuple[list[Professor], list[str]]:
    """
    Search OpenAlex author records directly.

    Returns (professors, warnings).
    """
    logger.info("Searching OpenAlex authors for: %s", query)
    warnings: list[str] = []

    params: dict[str, Any] = {
        "search": query,
        "per_page": min(max_results, 50),
        "sort": "cited_by_count:desc",
    }

    if university_filter:
        inst_id = _resolve_institution_id(university_filter)
        if inst_id:
            params["filter"] = f"last_known_institutions.id:{_OPENALEX_BASE}/institutions/{inst_id}"

    try:
        time.sleep(_POLITE_DELAY)
        resp = requests.get(
            f"{_OPENALEX_BASE}/authors",
            params=params,
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.error("OpenAlex author search failed: %s", exc)
        warnings.append(f"OpenAlex author API error: {exc}")
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

        # Get top concepts/topics
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
    """
    Search Semantic Scholar papers and extract author info.

    Returns (professors, warnings). May hit rate limits.
    """
    logger.info("Searching Semantic Scholar for: %s", query)
    warnings: list[str] = []

    try:
        time.sleep(1.0)  # S2 needs more politeness
        resp = requests.get(
            f"{_S2_BASE}/paper/search",
            params={
                "query": query,
                "fields": "title,authors,year,citationCount",
                "limit": min(max_results * 3, 100),
            },
            headers={"User-Agent": _HEADERS["User-Agent"]},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 429:
            warnings.append("Semantic Scholar rate limited — try again in a minute.")
            return [], warnings
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.error("Semantic Scholar search failed: %s", exc)
        warnings.append(f"Semantic Scholar API error: {exc}")
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

            if not name or len(name.split()) < 2:
                continue
            if name.lower() in seen_names:
                continue

            profile_url = f"https://www.semanticscholar.org/author/{author_id}" if author_id else ""

            seen_names.add(name.lower())
            professors.append(Professor(
                name=name,
                title=None,
                email="",
                university="",  # S2 paper search doesn't include affiliations
                department="",
                field=field or query,
                profile_url=profile_url,
                research_summary=f"Paper: \"{title}\" ({year}, {cited} citations)" if title else "",
                notes=f"Source: Semantic Scholar | Author ID: {author_id}",
                status="new",
            ))

    logger.info("Found %d professors from Semantic Scholar", len(professors))
    return professors, warnings


# ---------------------------------------------------------------------------
# Combined search
# ---------------------------------------------------------------------------

def find_professors(
    query: str = "",
    universities: Optional[list[str]] = None,
    field: str = "",
    department: str = "Computer Science",
    max_scholar_results: int = 30,
    custom_urls: Optional[dict[str, str]] = None,
) -> Tuple[list[Professor], list[str]]:
    """
    Run discovery strategies and combine results.

    Primary: OpenAlex (works search + author search)
    Backup: Semantic Scholar (if OpenAlex returns few results)

    Parameters
    ----------
    query : str
        Search query (e.g. "blockchain fintech AI").
    universities : list of str, optional
        University names to filter by.
    field : str
        Research field to tag professors with.
    department : str
        Department name for context.
    max_scholar_results : int
        Max professors to return.
    custom_urls : dict, optional
        Unused (kept for backward compat).

    Returns
    -------
    (professors, warnings) : tuple
    """
    all_professors: list[Professor] = []
    warnings: list[str] = []

    if not query:
        warnings.append("Enter a search query to find professors.")
        return [], warnings

    # If specific universities are selected, search with university filter
    if universities:
        for uni in universities:
            uni_profs, uni_warns = search_openalex_works(
                query=query,
                field=field,
                max_results=max(max_scholar_results // len(universities), 10),
                university_filter=uni,
            )
            all_professors.extend(uni_profs)
            warnings.extend(uni_warns)

            # Also try author search for this university
            auth_profs, auth_warns = search_openalex_authors(
                query=query,
                field=field,
                max_results=max(max_scholar_results // len(universities), 5),
                university_filter=uni,
            )
            all_professors.extend(auth_profs)
            warnings.extend(auth_warns)
    else:
        # General search — works first
        work_profs, work_warns = search_openalex_works(
            query=query,
            field=field,
            max_results=max_scholar_results,
        )
        all_professors.extend(work_profs)
        warnings.extend(work_warns)

        # Also search authors
        auth_profs, auth_warns = search_openalex_authors(
            query=query,
            field=field,
            max_results=max_scholar_results // 2,
        )
        all_professors.extend(auth_profs)
        warnings.extend(auth_warns)

    # If we got very few results, try Semantic Scholar as backup
    if len(all_professors) < 5:
        s2_profs, s2_warns = search_semantic_scholar(
            query=query,
            field=field,
            max_results=max_scholar_results,
        )
        all_professors.extend(s2_profs)
        warnings.extend(s2_warns)

    # Deduplicate by name (case-insensitive)
    seen: set[str] = set()
    unique: list[Professor] = []
    for prof in all_professors:
        key = prof.name.lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(prof)

    # Sort by citation count (extracted from notes)
    def _citation_sort(p: Professor) -> int:
        notes = p.notes or ""
        m = re.search(r"Citations:\s*(\d+)", notes)
        return int(m.group(1)) if m else 0
    unique.sort(key=_citation_sort, reverse=True)

    return unique[:max_scholar_results], warnings


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def list_known_universities() -> list[str]:
    """Return list of top universities for filtering."""
    return sorted(_TOP_UNIVERSITIES)
