"""
Quality scoring system for the Academic Outreach Email System.

Scores generated email drafts on a 1-10 scale across five dimensions:
specificity, authenticity, relevance, conciseness, and completeness.
All thresholds and weights are sourced from the application Config.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import List, Optional, Set, Tuple

from app.config import Config
from app.database import get_connection, get_draft, get_drafts, get_professor, update_draft
from app.models import Draft, Professor

logger: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    """Lowercase tokenize text into words, stripping punctuation."""
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower())


def _ngrams(tokens: list[str], n: int) -> list[tuple[str, ...]]:
    """Generate n-grams from a token list."""
    if len(tokens) < n:
        return []
    return [tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]


def _collect_template_phrases(config: Config) -> set[tuple[str, ...]]:
    """Extract 3-grams from all variation pool phrases for boilerplate detection."""
    phrases: list[str] = []
    pools = config.variation_pools
    phrases.extend(pools.greetings)
    phrases.extend(pools.openers)
    phrases.extend(pools.transitions)
    phrases.extend(pools.interest_connectors)
    phrases.extend(pools.asks)
    phrases.extend(pools.fallbacks)
    phrases.extend(pools.signoffs)
    phrases.extend(pools.closings)

    template_ngrams: set[tuple[str, ...]] = set()
    for phrase in phrases:
        # Strip template placeholders before tokenizing
        cleaned: str = re.sub(r"\{[^}]+\}", "", phrase)
        tokens: list[str] = _tokenize(cleaned)
        for gram in _ngrams(tokens, 3):
            template_ngrams.add(gram)
    return template_ngrams


def _first_sentence(text: str) -> str:
    """Extract the first sentence from text."""
    # Match up to the first sentence-ending punctuation
    match = re.match(r"^(.*?[.!?])\s", text, re.DOTALL)
    if match:
        return match.group(1)
    # Fallback: first 200 chars
    return text[:200]


# ---------------------------------------------------------------------------
# Individual scoring dimensions
# ---------------------------------------------------------------------------

def _score_specificity(body: str, professor: Professor) -> float:
    """Score 0.0-1.0 based on professor-specific terms found in the body.

    Checks for professor's keywords, name, university, department, and field.
    Score = min(specific_terms_found / 3, 1.0).
    """
    body_lower: str = body.lower()
    specific_terms_found: int = 0

    # Check professor keywords
    for keyword in professor.keywords_list:
        if keyword.lower() in body_lower:
            specific_terms_found += 1

    # Check professor name (last name is most meaningful)
    if professor.name:
        name_parts: list[str] = professor.name.split()
        for part in name_parts:
            if len(part) > 2 and part.lower() in body_lower:
                specific_terms_found += 1
                break  # Count name presence once

    # Check university
    if professor.university and professor.university.lower() in body_lower:
        specific_terms_found += 1

    # Check department
    if professor.department and professor.department.lower() in body_lower:
        specific_terms_found += 1

    # Check field
    if professor.field and professor.field.lower() in body_lower:
        specific_terms_found += 1

    return min(specific_terms_found / 3.0, 1.0)


def _score_authenticity(body: str, config: Config) -> float:
    """Score 0.0-1.0 based on how much content is unique vs template boilerplate.

    Tokenizes the body into 3-grams, compares against known template phrases.
    Score = 1.0 - (matched_template_3grams / total_3grams).
    Higher score = more unique content.
    """
    tokens: list[str] = _tokenize(body)
    body_trigrams: list[tuple[str, ...]] = _ngrams(tokens, 3)

    if not body_trigrams:
        return 0.0

    template_trigrams: set[tuple[str, ...]] = _collect_template_phrases(config)

    matched: int = 0
    for gram in body_trigrams:
        if gram in template_trigrams:
            matched += 1

    total: int = len(body_trigrams)
    return 1.0 - (matched / total)


def _score_relevance(body: str, professor: Professor) -> float:
    """Score 0.0-1.0 based on overlap between professor's research and email content.

    Checks keyword/field overlap and whether talking points relate to professor's
    research area using simple keyword matching.
    """
    if not professor.keywords_list and not professor.field:
        return 0.0

    body_lower: str = body.lower()
    body_tokens: set[str] = set(_tokenize(body))

    relevant_terms: list[str] = list(professor.keywords_list)
    if professor.field:
        relevant_terms.extend(_tokenize(professor.field))
    if professor.research_summary:
        # Extract significant words from the research summary (4+ chars)
        summary_tokens: list[str] = _tokenize(professor.research_summary)
        relevant_terms.extend([t for t in summary_tokens if len(t) >= 4])

    if not relevant_terms:
        return 0.0

    # Deduplicate
    unique_terms: set[str] = {t.lower() for t in relevant_terms}

    matches: int = 0
    for term in unique_terms:
        term_tokens: list[str] = _tokenize(term)
        if any(tt in body_tokens for tt in term_tokens):
            matches += 1

    # Also check if talking points appear in the body
    talking_points: list[str] = professor.talking_points_list
    talking_point_found: bool = False
    for tp in talking_points:
        tp_words: list[str] = _tokenize(tp)
        # A talking point is relevant if at least half its words appear in body
        if tp_words:
            found_count: int = sum(1 for w in tp_words if w in body_tokens and len(w) >= 3)
            if found_count >= max(len(tp_words) * 0.3, 1):
                talking_point_found = True
                break

    base_score: float = min(matches / max(len(unique_terms) * 0.3, 1.0), 1.0)

    # Boost slightly if a talking point was found
    if talking_point_found:
        base_score = min(base_score + 0.15, 1.0)

    return base_score


def _score_conciseness(body: str, config: Config) -> float:
    """Score 0.0-1.0 based on word count relative to configured min/max.

    1.0 if in range; linear decrease outside; heavy penalty beyond 2x max or
    below 0.5x min.
    """
    word_count: int = len(body.split())
    wc_min: int = config.generation.word_count_min
    wc_max: int = config.generation.word_count_max

    if wc_min <= word_count <= wc_max:
        return 1.0

    if word_count < wc_min:
        hard_floor: float = wc_min * 0.5
        if word_count <= hard_floor:
            return 0.0
        # Linear decrease from 1.0 at wc_min to 0.0 at hard_floor
        return (word_count - hard_floor) / (wc_min - hard_floor)

    # word_count > wc_max
    hard_ceiling: float = wc_max * 2.0
    if word_count >= hard_ceiling:
        return 0.0
    # Linear decrease from 1.0 at wc_max to 0.0 at hard_ceiling
    return (hard_ceiling - word_count) / (hard_ceiling - wc_max)


def _score_completeness(body: str, professor: Professor) -> float:
    """Score 0.0-1.0 based on binary checks for required email components.

    Checks: has professor name, has research reference, has clear ask,
    has fallback ask, has sign-off, has sender introduction.
    """
    body_lower: str = body.lower()
    checks_passed: int = 0
    total_checks: int = 6

    # 1. Has professor name
    if professor.name:
        name_parts: list[str] = professor.name.split()
        if any(part.lower() in body_lower for part in name_parts if len(part) > 2):
            checks_passed += 1

    # 2. Has research reference (any keyword or research-related term)
    research_indicators: list[str] = ["research", "work", "study", "paper", "project", "lab"]
    has_research_ref: bool = False
    for keyword in professor.keywords_list:
        if keyword.lower() in body_lower:
            has_research_ref = True
            break
    if not has_research_ref:
        for indicator in research_indicators:
            if indicator in body_lower:
                has_research_ref = True
                break
    if has_research_ref:
        checks_passed += 1

    # 3. Has clear ask (looking for opportunity-related language)
    ask_patterns: list[str] = [
        "opportunity", "get involved", "contribute", "join",
        "position", "opening", "chance to", "way to",
    ]
    if any(pat in body_lower for pat in ask_patterns):
        checks_passed += 1

    # 4. Has fallback ask (backup request language)
    fallback_patterns: list[str] = [
        "if not", "if that", "even if", "alternatively",
        "recommendation", "suggest", "guidance", "advice",
        "paper", "reading", "topic",
    ]
    # Require at least 2 fallback signals to count
    fallback_hits: int = sum(1 for pat in fallback_patterns if pat in body_lower)
    if fallback_hits >= 2:
        checks_passed += 1

    # 5. Has sign-off (closing phrase)
    signoff_patterns: list[str] = [
        "thank you", "thanks", "sincerely", "best regards",
        "respectfully", "appreciation", "grateful",
    ]
    if any(pat in body_lower for pat in signoff_patterns):
        checks_passed += 1

    # 6. Has sender introduction (self-introduction language)
    intro_patterns: list[str] = [
        "i am a", "i'm a", "my name is", "i am currently",
        "i have been", "i've been", "as a student",
        "high school", "undergraduate", "my background",
    ]
    if any(pat in body_lower for pat in intro_patterns):
        checks_passed += 1

    return checks_passed / total_checks


# ---------------------------------------------------------------------------
# Genericness detection
# ---------------------------------------------------------------------------

def get_genericness_score(body: str, config: Config) -> float:
    """Return 0.0-1.0 indicating how generic/template-heavy the email is.

    Higher values mean more generic. Uses variation pool phrases to detect
    boilerplate saturation.
    """
    if not body.strip():
        return 1.0

    tokens: list[str] = _tokenize(body)
    body_trigrams: list[tuple[str, ...]] = _ngrams(tokens, 3)

    if not body_trigrams:
        return 1.0

    template_trigrams: set[tuple[str, ...]] = _collect_template_phrases(config)

    matched: int = 0
    for gram in body_trigrams:
        if gram in template_trigrams:
            matched += 1

    return matched / len(body_trigrams)


# ---------------------------------------------------------------------------
# Warning generation
# ---------------------------------------------------------------------------

def generate_warnings(
    draft: Draft,
    professor: Professor,
    config: Config,
) -> list[str]:
    """Generate a list of warning strings for a scored draft.

    Each warning identifies a specific quality concern. All thresholds
    are sourced from config.
    """
    warnings: list[str] = []
    body: str = draft.body

    # --- Opening too generic ---
    first_sent: str = _first_sentence(body)
    if first_sent:
        first_sent_trigrams: list[tuple[str, ...]] = _ngrams(_tokenize(first_sent), 3)
        if first_sent_trigrams:
            template_trigrams: set[tuple[str, ...]] = _collect_template_phrases(config)
            matched_count: int = sum(1 for g in first_sent_trigrams if g in template_trigrams)
            if matched_count / len(first_sent_trigrams) > 0.6:
                warnings.append("Opening too generic")

    # --- No concrete research reference ---
    keywords_found: int = 0
    body_lower: str = body.lower()
    for keyword in professor.keywords_list:
        if keyword.lower() in body_lower:
            keywords_found += 1
    if keywords_found == 0:
        warnings.append("No concrete research reference")

    # --- Could apply to almost anyone ---
    genericness: float = get_genericness_score(body, config)
    if genericness > config.scoring.thresholds.genericness_threshold:
        warnings.append("Could apply to almost anyone")

    # --- Research hook is weak ---
    talking_points: list[str] = professor.talking_points_list
    body_tokens: set[str] = set(_tokenize(body))
    hook_found: bool = False
    for tp in talking_points:
        tp_words: list[str] = [w for w in _tokenize(tp) if len(w) >= 3]
        if tp_words:
            hit_count: int = sum(1 for w in tp_words if w in body_tokens)
            if hit_count >= max(len(tp_words) * 0.3, 1):
                hook_found = True
                break
    if not hook_found and talking_points:
        warnings.append("Research hook is weak")

    # --- Based on insufficient data ---
    has_data: bool = bool(
        professor.keywords_list
        or professor.summary
        or professor.enrichment_text
    )
    if not has_data:
        warnings.append("Based on insufficient data")

    # --- Relies too heavily on template language ---
    if draft.authenticity_score < 0.3:
        warnings.append("Relies too heavily on template language")

    # --- Email too short / too long ---
    word_count: int = len(body.split())
    if word_count < config.generation.word_count_min:
        warnings.append("Email too short")
    elif word_count > config.generation.word_count_max:
        warnings.append("Email too long")

    # --- Missing fallback ask ---
    fallback_patterns: list[str] = [
        "if not", "if that", "even if", "alternatively",
        "recommendation", "suggest", "guidance",
    ]
    fallback_hits: int = sum(1 for pat in fallback_patterns if pat in body_lower)
    if fallback_hits < 2:
        warnings.append("Missing fallback ask")

    # --- Missing sender introduction ---
    intro_patterns: list[str] = [
        "i am a", "i'm a", "my name is", "i am currently",
        "i have been", "i've been", "as a student",
        "high school", "undergraduate", "my background",
    ]
    if not any(pat in body_lower for pat in intro_patterns):
        warnings.append("Missing sender introduction")

    return warnings


# ---------------------------------------------------------------------------
# Main scoring entry point
# ---------------------------------------------------------------------------

def score_draft(
    draft: Draft,
    professor: Professor,
    config: Config,
) -> Draft:
    """Score a draft across all dimensions and populate score fields and warnings.

    Returns the draft with all score fields (specificity_score, authenticity_score,
    relevance_score, conciseness_score, completeness_score, overall_score) and
    warnings filled in. The overall score is scaled to 1-10.
    """
    try:
        body: str = draft.body

        if not body.strip():
            logger.warning("Attempted to score empty draft id=%s", draft.id)
            draft.specificity_score = 0.0
            draft.authenticity_score = 0.0
            draft.relevance_score = 0.0
            draft.conciseness_score = 0.0
            draft.completeness_score = 0.0
            draft.overall_score = 1.0
            draft.warnings_list = ["Empty email body"]
            return draft

        # Compute each dimension (0.0-1.0)
        specificity: float = _score_specificity(body, professor)
        authenticity: float = _score_authenticity(body, config)
        relevance: float = _score_relevance(body, professor)
        conciseness: float = _score_conciseness(body, config)
        completeness: float = _score_completeness(body, professor)

        # Store raw dimension scores
        draft.specificity_score = round(specificity, 4)
        draft.authenticity_score = round(authenticity, 4)
        draft.relevance_score = round(relevance, 4)
        draft.conciseness_score = round(conciseness, 4)
        draft.completeness_score = round(completeness, 4)

        # Weighted sum scaled to 1-10
        weights = config.scoring.weights
        weighted_sum: float = (
            specificity * weights.specificity
            + authenticity * weights.authenticity
            + relevance * weights.relevance
            + conciseness * weights.conciseness
            + completeness * weights.completeness
        )
        # Scale to 1-10 range (0.0 maps to 1, 1.0 maps to 10)
        overall: float = weighted_sum * 9.0 + 1.0
        draft.overall_score = round(min(max(overall, 1.0), 10.0), 2)

        # Generate warnings
        warning_list: list[str] = generate_warnings(draft, professor, config)
        draft.warnings_list = warning_list

        logger.info(
            "Scored draft id=%s: overall=%.2f (spec=%.2f auth=%.2f rel=%.2f "
            "conc=%.2f comp=%.2f) warnings=%d",
            draft.id,
            draft.overall_score,
            draft.specificity_score,
            draft.authenticity_score,
            draft.relevance_score,
            draft.conciseness_score,
            draft.completeness_score,
            len(warning_list),
        )

        return draft

    except Exception:
        logger.exception("Failed to score draft id=%s", draft.id)
        raise


# ---------------------------------------------------------------------------
# Batch scoring
# ---------------------------------------------------------------------------

def score_all_drafts(
    db_path: str,
    session_id: int,
    config: Config,
) -> int:
    """Score all drafts in a session and persist results to the database.

    Returns the count of drafts scored.
    """
    conn: Optional[sqlite3.Connection] = None
    scored_count: int = 0

    try:
        conn = get_connection(db_path)
        drafts: list[Draft] = get_drafts(conn, session_id=session_id)

        if not drafts:
            logger.info("No drafts found for session_id=%d", session_id)
            return 0

        for draft in drafts:
            professor: Optional[Professor] = get_professor(conn, draft.professor_id)
            if professor is None:
                logger.warning(
                    "Professor id=%d not found for draft id=%s; skipping",
                    draft.professor_id,
                    draft.id,
                )
                continue

            scored_draft: Draft = score_draft(draft, professor, config)
            update_draft(conn, scored_draft)
            scored_count += 1

        logger.info(
            "Scored %d/%d drafts for session_id=%d",
            scored_count,
            len(drafts),
            session_id,
        )
        return scored_count

    except sqlite3.Error:
        logger.exception(
            "Database error while scoring drafts for session_id=%d", session_id
        )
        raise
    finally:
        if conn is not None:
            conn.close()
