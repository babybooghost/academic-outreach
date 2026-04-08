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
_POLITE_DELAY: float = 0.3

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
    """Clean up institution name."""
    if not raw:
        return ""
    clean = raw.strip()
    if len(clean) > 120:
        clean = clean[:120].rsplit(",", 1)[0]
    return clean


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
        resp = requests.get(
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

    try:
        time.sleep(1.0)
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
        resp = requests.get(
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
        warnings.append(f"Crossref API error: {exc}")
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
        resp = requests.get(
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
        warnings.append(f"DBLP API error: {exc}")
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
) -> Tuple[list[Professor], list[str]]:
    """Search arXiv for preprints and extract author info."""
    logger.info("Searching arXiv for: %s", query)
    warnings: list[str] = []

    try:
        time.sleep(_POLITE_DELAY)
        resp = requests.get(
            _ARXIV_BASE,
            params={
                "search_query": f"all:{query}",
                "start": 0,
                "max_results": min(max_results * 3, 50),
                "sortBy": "relevance",
                "sortOrder": "descending",
            },
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error("arXiv search failed: %s", exc)
        warnings.append(f"arXiv API error: {exc}")
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

def find_professors(
    query: str = "",
    universities: Optional[list[str]] = None,
    field: str = "",
    department: str = "Computer Science",
    max_scholar_results: int = 30,
    custom_urls: Optional[dict[str, str]] = None,
) -> Tuple[list[Professor], list[str]]:
    """
    Run discovery across multiple academic databases and combine results.

    Sources (all free, no API keys):
        1. OpenAlex — 250M+ works, structured institution data (primary)
        2. Crossref — 150M+ metadata records, DOI authority
        3. DBLP — Computer science focused, venue info
        4. Semantic Scholar — backup if others return few results
    """
    all_professors: list[Professor] = []
    warnings: list[str] = []

    if not query:
        warnings.append("Enter a search query to find professors.")
        return [], warnings

    # --- Strategy 1: OpenAlex (primary — best for university filtering) ---
    if universities:
        per_uni = max(max_scholar_results // len(universities), 8)
        for uni in universities:
            inst_id = _resolve_institution_id(uni)
            if not inst_id:
                warnings.append(f"Could not resolve '{uni}' — skipped.")
                continue

            work_profs, work_warns = search_openalex_works(
                query=query, field=field,
                max_results=per_uni,
                university_filter=uni,
                _resolved_inst_id=inst_id,
            )
            all_professors.extend(work_profs)
            warnings.extend(work_warns)

            auth_profs, auth_warns = search_openalex_authors(
                query=query, field=field,
                max_results=max(per_uni // 2, 5),
                university_filter=uni,
                _resolved_inst_id=inst_id,
            )
            all_professors.extend(auth_profs)
            warnings.extend(auth_warns)
    else:
        work_profs, work_warns = search_openalex_works(
            query=query, field=field, max_results=max_scholar_results,
        )
        all_professors.extend(work_profs)
        warnings.extend(work_warns)

        auth_profs, auth_warns = search_openalex_authors(
            query=query, field=field, max_results=max_scholar_results // 2,
        )
        all_professors.extend(auth_profs)
        warnings.extend(auth_warns)

    # --- Strategy 2: Crossref (always mix in for broader coverage) ---
    if not universities:
        cr_profs, cr_warns = search_crossref(
            query=query, field=field, max_results=min(max_scholar_results // 3, 10),
        )
        all_professors.extend(cr_profs)
        warnings.extend(cr_warns)

    # --- Strategy 3: DBLP (CS-focused, good for CS/AI/ML queries) ---
    cs_keywords = {"computer", "algorithm", "machine learning", "ai ", "deep learning",
                   "neural", "nlp", "software", "database", "crypto", "blockchain",
                   "distributed", "security", "network", "programming", "data"}
    is_cs_query = any(kw in query.lower() for kw in cs_keywords)

    if is_cs_query and not universities:
        dblp_profs, dblp_warns = search_dblp(
            query=query, field=field or "Computer Science",
            max_results=min(max_scholar_results // 4, 8),
        )
        all_professors.extend(dblp_profs)
        warnings.extend(dblp_warns)

    # --- Strategy 4: arXiv (preprints — physics, CS, math, fintech) ---
    arxiv_keywords = cs_keywords | {"physics", "quantum", "fintech", "finance",
                                     "economic", "math", "statistics", "biology"}
    is_arxiv_query = any(kw in query.lower() for kw in arxiv_keywords)

    if is_arxiv_query and not universities:
        ax_profs, ax_warns = search_arxiv(
            query=query, field=field,
            max_results=min(max_scholar_results // 4, 8),
        )
        all_professors.extend(ax_profs)
        warnings.extend(ax_warns)

    # --- Strategy 5: Semantic Scholar (backup if still few results) ---
    if len(all_professors) < 5 and not universities:
        s2_profs, s2_warns = search_semantic_scholar(
            query=query, field=field, max_results=max_scholar_results,
        )
        all_professors.extend(s2_profs)
        warnings.extend(s2_warns)

    # Deduplicate by name
    seen: set[str] = set()
    unique: list[Professor] = []
    for prof in all_professors:
        key = prof.name.lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(prof)

    # Sort by citation count
    def _cit(p: Professor) -> int:
        m = re.search(r"Citations:\s*(\d+)", p.notes or "")
        return int(m.group(1)) if m else 0
    unique.sort(key=_cit, reverse=True)

    return unique[:max_scholar_results], warnings


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def list_known_universities() -> list[str]:
    """Return list of top universities for filtering."""
    return sorted(set(_TOP_UNIVERSITIES))
