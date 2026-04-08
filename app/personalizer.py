"""
Personalization module for the Academic Outreach Email System.

Generates talking points and hooks from professor data, cross-referenced
with the sender's interests. Supports both template-based and LLM-based
generation.
"""

from __future__ import annotations

import json
import random
import sqlite3
from typing import Any, Optional

import requests as http_requests

from app.config import Config
from app.database import (
    get_connection,
    get_professors,
    get_sender_profile,
    update_professor,
)
from app.logger import get_logger, audit_log
from app.models import Professor, SenderProfile

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Template pools for talking-point generation
# ---------------------------------------------------------------------------

_SPECIFICITY_TEMPLATES: list[str] = [
    "I was especially interested in the way your work uses {keyword} "
    "rather than assuming ideal conditions.",
    "What stood out to me was your focus on turning {topic} into models "
    "that are still useful without oversimplifying them.",
    "Your approach to {keyword} caught my attention because it addresses "
    "real-world complexity in {field}.",
    "I found it compelling that your research on {topic} bridges the gap "
    "between theoretical {field} and practical application.",
    "What drew me in was how your work on {keyword} handles the messiness "
    "of real data rather than working only with clean benchmarks.",
    "Your research on {keyword} interested me because it tackles problems "
    "that most approaches in {field} tend to simplify away.",
]

_OVERLAP_TEMPLATES: list[str] = [
    "As someone interested in {sender_interest}, I see a strong connection "
    "to your work on {keyword}, especially the way it applies to {field}.",
    "My interest in {sender_interest} aligns well with your research on "
    "{keyword}, and I would love to understand more about how they intersect.",
    "I have been exploring {sender_interest} on my own, and your work on "
    "{keyword} seems like exactly the kind of {field} research I want to "
    "learn more about.",
    "The overlap between {sender_interest} and your focus on {keyword} "
    "is part of what drew me to your lab's work in {field}.",
]

_RESEARCH_DEPTH_TEMPLATES: list[str] = [
    "Your recent work on {topic} suggests a direction in {field} that "
    "could have significant practical impact.",
    "I was particularly drawn to {topic} because it represents an "
    "underexplored but promising area within {field}.",
    "The way your research addresses {topic} within the broader context "
    "of {field} shows a level of depth I find inspiring.",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_overlapping_interests(
    professor_keywords: list[str],
    professor_field: str,
    sender_interests: str,
) -> list[str]:
    """
    Find terms that appear in both the professor's keywords/field and the
    sender's interests string.
    """
    if not sender_interests:
        return []

    sender_terms: set[str] = set()
    for raw in sender_interests.lower().replace(",", " ").replace(";", " ").split():
        cleaned: str = raw.strip()
        if len(cleaned) > 2:
            sender_terms.add(cleaned)

    overlaps: list[str] = []
    all_prof_terms: list[str] = professor_keywords + [professor_field]
    for term in all_prof_terms:
        term_lower: str = term.lower()
        for sender_term in sender_terms:
            if sender_term in term_lower or term_lower in sender_term:
                overlaps.append(term)
                break
    return overlaps


def _pick_template(
    templates: list[str],
    config: Config,
) -> str:
    """Select a random template from the given pool."""
    return random.choice(templates)


def _safe_format(template: str, **kwargs: str) -> str:
    """
    Format a template string, substituting empty strings for any
    missing keys rather than raising KeyError.
    """
    class SafeDict(dict):  # type: ignore[type-arg]
        def __missing__(self, key: str) -> str:
            return f"[{key}]"

    return template.format_map(SafeDict(**kwargs))


# ---------------------------------------------------------------------------
# Template-based talking-point generation
# ---------------------------------------------------------------------------

def _generate_template_points(
    prof: Professor,
    sender: SenderProfile,
    config: Config,
) -> list[str]:
    """
    Generate 2-3 talking points using template pools and professor data.
    """
    points: list[str] = []
    keywords: list[str] = prof.keywords_list
    field: str = prof.field or "their research area"
    topic: str = prof.summary or prof.research_summary or field

    # Truncate topic for template insertion
    if len(topic) > 80:
        topic = topic[:77] + "..."

    primary_keyword: str = keywords[0] if keywords else field

    # Point 1: Specificity -- what stood out about their research
    specificity_template: str = _pick_template(_SPECIFICITY_TEMPLATES, config)
    point1: str = _safe_format(
        specificity_template,
        keyword=primary_keyword,
        topic=topic,
        field=field,
    )
    points.append(point1)

    # Point 2: Overlap with sender interests (if any)
    overlaps: list[str] = _find_overlapping_interests(
        keywords, field, sender.interests
    )
    if overlaps and sender.interests:
        # Pick the best overlap keyword
        overlap_keyword: str = overlaps[0]
        # Pick a sender interest term to reference
        sender_interest_terms: list[str] = [
            t.strip() for t in sender.interests.split(",") if t.strip()
        ]
        sender_interest: str = (
            sender_interest_terms[0] if sender_interest_terms
            else sender.interests
        )
        overlap_template: str = _pick_template(_OVERLAP_TEMPLATES, config)
        point2: str = _safe_format(
            overlap_template,
            sender_interest=sender_interest,
            keyword=overlap_keyword,
            field=field,
        )
        points.append(point2)
    elif sender.interests:
        # No direct overlap found -- use sender's interest + a professor keyword
        sender_interest_terms = [
            t.strip() for t in sender.interests.split(",") if t.strip()
        ]
        sender_interest = (
            sender_interest_terms[0] if sender_interest_terms
            else sender.interests
        )
        secondary_keyword: str = keywords[1] if len(keywords) > 1 else primary_keyword
        overlap_template = _pick_template(_OVERLAP_TEMPLATES, config)
        point2 = _safe_format(
            overlap_template,
            sender_interest=sender_interest,
            keyword=secondary_keyword,
            field=field,
        )
        points.append(point2)

    # Point 3: Research depth (if we have enough material)
    if len(keywords) > 2 or prof.research_summary:
        depth_topic: str = keywords[2] if len(keywords) > 2 else primary_keyword
        depth_template: str = _pick_template(_RESEARCH_DEPTH_TEMPLATES, config)
        point3: str = _safe_format(
            depth_template,
            topic=depth_topic,
            field=field,
        )
        points.append(point3)

    return points[:3]


# ---------------------------------------------------------------------------
# LLM-based talking-point generation
# ---------------------------------------------------------------------------

def _generate_llm_points(
    prof: Professor,
    sender: SenderProfile,
    config: Config,
) -> list[str]:
    """
    Generate talking points using the configured LLM provider.

    Falls back to template-based generation on failure.
    """
    if not config.llm_provider or not config.llm_api_key:
        return _generate_template_points(prof, sender, config)

    prompt: str = (
        f"You are helping a student write a personalized outreach email to a "
        f"professor. Generate exactly 3 specific, concrete talking points "
        f"(each 1-2 sentences) about the professor's research that would work "
        f"well in a cold email.\n\n"
        f"Professor: {prof.name}\n"
        f"Field: {prof.field}\n"
        f"Keywords: {', '.join(prof.keywords_list)}\n"
        f"Summary: {prof.summary or prof.research_summary or 'N/A'}\n"
        f"Student interests: {sender.interests}\n\n"
        f"Return JSON: {{\"talking_points\": [\"point1\", \"point2\", \"point3\"]}}\n"
        f"Make them sound genuine, specific, and show real understanding. "
        f"Avoid generic praise."
    )

    try:
        response_text: str = _call_llm(prompt, config)
        points: list[str] = _parse_llm_points(response_text)
        if points:
            return points
    except Exception as exc:
        logger.warning(
            "LLM talking-point generation failed for %s: %s; using templates",
            prof.name, exc,
        )

    return _generate_template_points(prof, sender, config)


def _call_llm(prompt: str, config: Config) -> str:
    """Dispatch to the configured LLM provider."""
    provider: str = (config.llm_provider or "").lower()
    api_key: str = config.llm_api_key or ""

    if provider == "openai":
        try:
            import openai  # type: ignore[import-untyped]
            client = openai.OpenAI(api_key=api_key)
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=600,
            )
            return response.choices[0].message.content or ""
        except ImportError:
            raise RuntimeError("openai package is not installed")

    elif provider == "anthropic":
        try:
            import anthropic  # type: ignore[import-untyped]
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
            )
            if response.content and len(response.content) > 0:
                return response.content[0].text
            return ""
        except ImportError:
            raise RuntimeError("anthropic package is not installed")

    elif provider == "openrouter":
        url: str = "https://openrouter.ai/api/v1/chat/completions"
        headers: dict[str, str] = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": config.llm_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4,
            "max_tokens": 600,
        }
        resp = http_requests.post(url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        choices: list[dict[str, Any]] = data.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "")
        return ""

    else:
        raise ValueError(f"Unsupported LLM provider: {provider}")


def _parse_llm_points(raw: str) -> list[str]:
    """Parse LLM JSON response into a list of talking-point strings."""
    text: str = raw.strip()

    # Strip markdown code fences
    if text.startswith("```"):
        lines: list[str] = text.split("\n")
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()

    start: int = text.find("{")
    end: int = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return []

    try:
        data: dict[str, Any] = json.loads(text[start : end + 1])
        points: list[Any] = data.get("talking_points", [])
        return [str(p) for p in points if p]
    except (json.JSONDecodeError, TypeError):
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_talking_points(
    prof: Professor,
    sender: SenderProfile,
    config: Config,
) -> list[str]:
    """
    Generate 2-3 personalized talking points for outreach.

    Uses LLM if configured, otherwise falls back to template-based generation.
    Cross-references professor keywords, summary, and field with the sender's
    interests to find meaningful overlap.

    Parameters
    ----------
    prof : Professor
        The professor to generate points for.
    sender : SenderProfile
        The sender/student profile.
    config : Config
        Application configuration.

    Returns
    -------
    list[str]
        2-3 talking point sentences.
    """
    if config.llm_provider and config.llm_api_key:
        return _generate_llm_points(prof, sender, config)
    return _generate_template_points(prof, sender, config)


def personalize_professor(
    prof: Professor,
    sender: SenderProfile,
    config: Config,
) -> Professor:
    """
    Generate talking points and update the professor record.

    Sets prof.talking_points (JSON list) and prof.status to 'ready'.

    Parameters
    ----------
    prof : Professor
        The professor to personalize.
    sender : SenderProfile
        The sender/student profile.
    config : Config
        Application configuration.

    Returns
    -------
    Professor
        The updated professor (same object, mutated in place).
    """
    points: list[str] = generate_talking_points(prof, sender, config)
    prof.talking_points_list = points
    prof.status = "ready"

    logger.info(
        "Personalized %s: %d talking point(s) generated",
        prof.name, len(points),
    )
    return prof


def personalize_all(
    db_path: str,
    sender_profile_id: int,
    config: Config,
) -> tuple[int, int]:
    """
    Generate talking points for all professors with status='enriched'
    that have been summarized (have keywords and summary).

    Parameters
    ----------
    db_path : str
        Path to the SQLite database.
    sender_profile_id : int
        The sender profile ID to use for cross-referencing interests.
    config : Config
        Application configuration.

    Returns
    -------
    tuple[int, int]
        (personalized_count, failed_count)
    """
    personalized: int = 0
    failed: int = 0

    conn: sqlite3.Connection = get_connection(db_path)
    try:
        # Load sender profile
        sender: Optional[SenderProfile] = get_sender_profile(
            conn, sender_profile_id
        )
        if sender is None:
            error_msg: str = (
                f"Sender profile with id={sender_profile_id} not found"
            )
            logger.error(error_msg)
            audit_log(
                action="personalization_error",
                detail=error_msg,
                db_path=db_path,
            )
            return 0, 0

        # Get professors that have been enriched/summarized
        professors: list[Professor] = get_professors(conn, status="enriched")
        total: int = len(professors)
        logger.info(
            "Starting personalization for %d professor(s) with sender '%s'",
            total, sender.name,
        )

        for prof in professors:
            # Only personalize professors that have summary data
            if not prof.keywords and not prof.summary:
                logger.info(
                    "Skipping %s: no keywords or summary available",
                    prof.name,
                )
                failed += 1
                continue

            try:
                personalize_professor(prof, sender, config)

                if prof.id is not None:
                    update_professor(conn, prof)

                personalized += 1
                audit_log(
                    action="personalization_success",
                    detail=(
                        f"Personalized professor '{prof.name}' "
                        f"({len(prof.talking_points_list)} talking points)"
                    ),
                    metadata={
                        "professor_id": prof.id,
                        "professor_email": prof.email,
                        "talking_point_count": len(prof.talking_points_list),
                    },
                    db_path=db_path,
                )
            except Exception as exc:
                failed += 1
                logger.error(
                    "Error personalizing %s: %s", prof.name, exc
                )
                audit_log(
                    action="personalization_error",
                    detail=f"Error personalizing '{prof.name}': {exc}",
                    metadata={
                        "professor_id": prof.id,
                        "professor_email": prof.email,
                    },
                    db_path=db_path,
                )

    finally:
        conn.close()

    summary_msg: str = (
        f"Personalization complete: {personalized} personalized, {failed} failed "
        f"out of {total} total"
    )
    audit_log(
        action="personalization_batch_complete",
        detail=summary_msg,
        metadata={
            "personalized": personalized,
            "failed": failed,
            "total": total,
            "sender_profile_id": sender_profile_id,
        },
        db_path=db_path,
    )
    logger.info(summary_msg)

    return personalized, failed
