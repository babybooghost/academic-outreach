"""
Template engine for the Academic Outreach Email System.

Loads Jinja2 email templates, registers custom filters, and renders
personalised outreach emails and follow-ups with controlled, reproducible
variation driven by seeded RNG.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import jinja2

from app.config import Config
from app.logger import get_logger
from app.models import Draft, FollowUp, Professor, SenderProfile

_logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Template directory (sibling to this module)
# ---------------------------------------------------------------------------
_TEMPLATE_DIR: Path = Path(__file__).resolve().parent / "templates" / "emails"


# ---------------------------------------------------------------------------
# Custom Jinja2 filters
# ---------------------------------------------------------------------------

def _filter_last_name(full_name: str) -> str:
    """Extract the surname (last whitespace-delimited token) from *full_name*."""
    parts: list[str] = full_name.strip().split()
    return parts[-1] if parts else full_name


def _filter_humanize_list(items: list[str]) -> str:
    """Format a list as 'X, Y, and Z'."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + ", and " + items[-1]


def _make_pick_filter(pools: dict[str, list[str]]) -> Any:
    """Return a Jinja2 filter that selects from a named variation pool using a seed."""

    def _pick(pool_name: str, seed: int) -> str:
        pool: list[str] = pools.get(pool_name, [])
        if not pool:
            _logger.warning("Variation pool %r is empty or missing", pool_name)
            return ""
        rng: random.Random = random.Random(seed)
        return rng.choice(pool)

    return _pick


# ---------------------------------------------------------------------------
# Jinja2 environment factory
# ---------------------------------------------------------------------------

def _build_jinja_env(config: Config) -> jinja2.Environment:
    """Create a Jinja2 environment with custom filters registered."""
    env: jinja2.Environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=False,
        undefined=jinja2.StrictUndefined,
    )

    # Flatten all variation pools into a single dict for the pick filter
    variation_dict: dict[str, list[str]] = {
        "greetings": list(config.variation_pools.greetings),
        "openers": list(config.variation_pools.openers),
        "transitions": list(config.variation_pools.transitions),
        "interest_connectors": list(config.variation_pools.interest_connectors),
        "asks": list(config.variation_pools.asks),
        "fallbacks": list(config.variation_pools.fallbacks),
        "signoffs": list(config.variation_pools.signoffs),
        "closings": list(config.variation_pools.closings),
    }

    env.filters["last_name"] = _filter_last_name
    env.filters["humanize_list"] = _filter_humanize_list
    env.filters["pick"] = _make_pick_filter(variation_dict)

    return env


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

def _build_context(
    prof: Professor,
    sender: SenderProfile,
    config: Config,
    seed: int,
) -> dict[str, Any]:
    """
    Assemble the full template context dictionary from professor data,
    sender profile, and seeded variation-pool selections.
    """
    rng: random.Random = random.Random(seed)

    # Deterministic picks from each variation pool
    pools = config.variation_pools

    last_name: str = _filter_last_name(prof.name)
    talking_points: list[str] = prof.talking_points_list
    keywords: list[str] = prof.keywords_list

    # Primary topic: use a clean keyword phrase or field, NOT a full sentence
    # Clean keywords by removing multi-word artifacts from YAKE
    clean_keywords: list[str] = [
        k for k in keywords
        if len(k.split()) <= 4 and not any(c.isupper() for c in k[1:] if c.isalpha())
    ] or keywords
    topic: str = clean_keywords[0] if clean_keywords else prof.field
    # If topic looks like a sentence (too long), fall back to field
    if len(topic.split()) > 5:
        topic = prof.field

    # Hook: first talking point (a full sentence about specific research)
    hook: str = talking_points[0] if talking_points else (prof.summary or f"your research in {prof.field}")
    # Additional hook from second talking point
    extra_hook: str = (
        talking_points[1]
        if len(talking_points) > 1
        else ""
    )
    # Source for how the student found the professor
    source: str = "research profile"
    if prof.recent_work:
        source = "recent publication"
    elif prof.profile_url:
        source = "lab website"

    # Area of student interest (from sender interests or professor field)
    area: str = sender.interests if sender.interests else prof.field
    # Connection between student interest and professor work
    connection: str = f"exploring how {prof.field.lower()} can be applied to real-world problems"

    # --- Seeded selections from variation pools ---
    greeting: str = rng.choice(pools.greetings).format(last_name=last_name)
    opener: str = rng.choice(pools.openers).format(
        source=source, field=prof.field, topic=topic,
    )
    transition: str = rng.choice(pools.transitions)
    interest_connector: str = rng.choice(pools.interest_connectors).format(
        area=area, connection=connection,
    )
    ask: str = rng.choice(pools.asks)
    fallback: str = rng.choice(pools.fallbacks)
    signoff: str = rng.choice(pools.signoffs)
    closing: str = rng.choice(pools.closings)

    context: dict[str, Any] = {
        # Professor fields
        "professor_name": prof.name,
        "professor_last_name": last_name,
        "professor_title": prof.title or "Professor",
        "professor_email": prof.email,
        "university": prof.university,
        "department": prof.department,
        "lab_name": prof.lab_name or prof.department,
        "field": prof.field,
        "research_summary": prof.research_summary or "",
        "recent_work": prof.recent_work or "",
        "topic": topic,
        "hook": hook,
        "extra_hook": extra_hook,
        "source": source,
        "keywords": keywords,
        "talking_points": talking_points,
        # Sender fields
        "sender_name": sender.name,
        "sender_school": sender.school,
        "sender_grade": sender.grade,
        "sender_email": sender.email,
        "sender_interests": sender.interests,
        "sender_background": sender.background,
        "area": area,
        "connection": connection,
        # Variation pool selections
        "greeting": greeting,
        "opener": opener,
        "transition": transition,
        "interest_connector": interest_connector,
        "ask": ask,
        "fallback": fallback,
        "signoff": signoff,
        "closing": closing,
    }
    return context


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_available_variants() -> list[str]:
    """Return the list of available template variant names (without extension)."""
    variants: list[str] = []
    if _TEMPLATE_DIR.is_dir():
        for path in sorted(_TEMPLATE_DIR.glob("*.j2")):
            name: str = path.stem
            if name != "followup":
                variants.append(name)
    return variants


def generate_subject_lines(
    prof: Professor,
    sender: SenderProfile,
    config: Config,
    seed: int,
) -> list[str]:
    """
    Generate three varied subject-line options for the outreach email.

    Uses a seeded RNG so that the same inputs always produce the same
    subject lines.
    """
    rng: random.Random = random.Random(seed)

    last_name: str = _filter_last_name(prof.name)
    keywords: list[str] = prof.keywords_list
    # Use a clean, short topic for subject lines -- not a full sentence
    clean_kw: list[str] = [
        k for k in keywords
        if len(k.split()) <= 4 and not any(c.isupper() for c in k[1:] if c.isalpha())
    ] or keywords
    topic: str = clean_kw[0] if clean_kw else prof.field
    if len(topic.split()) > 5:
        topic = prof.field

    templates: list[str] = [
        "High school student interested in your research on {topic}",
        "Question about research opportunities in {field}",
        "{field} research inquiry from a high school student",
        "Interest in your {topic} research - {grade} student",
        "Aspiring researcher interested in {field}",
        "Research opportunity inquiry - {topic}",
    ]

    # Pick 3 unique templates
    selected: list[str] = rng.sample(templates, min(3, len(templates)))

    subject_lines: list[str] = [
        t.format(
            topic=topic,
            field=prof.field,
            grade=sender.grade,
            last_name=last_name,
        )
        for t in selected
    ]
    return subject_lines


def render_email(
    prof: Professor,
    sender: SenderProfile,
    config: Config,
    session_id: int,
    variant: str | None = None,
) -> Draft:
    """
    Render a personalised outreach email for *prof* from *sender*.

    Parameters
    ----------
    prof : Professor
        The target professor.
    sender : SenderProfile
        The student sending the email.
    config : Config
        Application configuration (pools, generation settings).
    session_id : int
        Session identifier used as part of the RNG seed.
    variant : str or None
        Template variant name (e.g. ``"formal"``).  When *None* a variant
        is selected pseudo-randomly using the seed.

    Returns
    -------
    Draft
        A Draft object with ``body``, ``subject_lines``, and
        ``template_variant`` populated.
    """
    prof_id: int = prof.id if prof.id is not None else 0
    seed: int = prof_id * 1000 + session_id
    rng: random.Random = random.Random(seed)

    # Select variant
    available: list[str] = (
        config.generation.template_variants
        if config.generation.template_variants
        else get_available_variants()
    )
    if variant is not None:
        chosen_variant: str = variant
    else:
        chosen_variant = rng.choice(available)

    _logger.info(
        "Rendering email for professor=%s variant=%s seed=%d",
        prof.name,
        chosen_variant,
        seed,
    )

    # Build context and render
    env: jinja2.Environment = _build_jinja_env(config)
    template: jinja2.Template = env.get_template(f"{chosen_variant}.j2")
    context: dict[str, Any] = _build_context(prof, sender, config, seed)
    body: str = template.render(**context).strip()

    # Subject lines (use a derived seed so they don't share RNG state with body)
    subject_lines: list[str] = generate_subject_lines(
        prof, sender, config, seed + 7,
    )

    draft: Draft = Draft(
        professor_id=prof_id,
        sender_profile_id=sender.id if sender.id is not None else 0,
        session_id=session_id,
        subject_lines=json.dumps(subject_lines),
        body=body,
        template_variant=chosen_variant,
    )

    _logger.info(
        "Draft rendered: variant=%s words=%d subjects=%d",
        chosen_variant,
        len(body.split()),
        len(subject_lines),
    )
    return draft


def render_followup(
    prof: Professor,
    sender: SenderProfile,
    original_draft: Draft,
    config: Config,
) -> FollowUp:
    """
    Render a follow-up email referencing the *original_draft*.

    Parameters
    ----------
    prof : Professor
        The target professor.
    sender : SenderProfile
        The student.
    original_draft : Draft
        The previously sent draft (used to pull topic/hook context).
    config : Config
        Application configuration.

    Returns
    -------
    FollowUp
        A FollowUp object with ``body`` and ``subject`` populated.
    """
    prof_id: int = prof.id if prof.id is not None else 0
    draft_id: int = original_draft.id if original_draft.id is not None else 0
    seed: int = prof_id * 1000 + draft_id + 500

    rng: random.Random = random.Random(seed)

    last_name: str = _filter_last_name(prof.name)
    talking_points: list[str] = prof.talking_points_list
    topic: str = talking_points[0] if talking_points else prof.field
    hook: str = (
        talking_points[1]
        if len(talking_points) > 1
        else (prof.summary or topic)
    )

    fp = config.followup_pools

    greeting: str = rng.choice(config.variation_pools.greetings).format(
        last_name=last_name,
    )
    fu_opener: str = rng.choice(fp.openers)
    interest_restatement: str = rng.choice(fp.interest_restatements).format(
        topic=topic, hook=hook,
    )
    soft_ask: str = rng.choice(fp.soft_asks)
    signoff: str = rng.choice(config.variation_pools.signoffs)
    closing: str = rng.choice(config.variation_pools.closings)

    context: dict[str, Any] = {
        "professor_name": prof.name,
        "professor_last_name": last_name,
        "greeting": greeting,
        "followup_opener": fu_opener,
        "interest_restatement": interest_restatement,
        "soft_ask": soft_ask,
        "signoff": signoff,
        "closing": closing,
        "sender_name": sender.name,
        "sender_school": sender.school,
        "topic": topic,
        "hook": hook,
        "field": prof.field,
    }

    env: jinja2.Environment = _build_jinja_env(config)
    template: jinja2.Template = env.get_template("followup.j2")
    body: str = template.render(**context).strip()

    # Follow-up subject: re-prefix on original subject
    original_subjects: list[str] = original_draft.subject_lines_list
    base_subject: str = original_subjects[0] if original_subjects else f"Interest in {topic} research"
    subject: str = f"Re: {base_subject}"

    followup: FollowUp = FollowUp(
        original_draft_id=draft_id,
        professor_id=prof_id,
        sender_profile_id=sender.id if sender.id is not None else 0,
        body=body,
        subject=subject,
    )

    _logger.info(
        "Follow-up rendered for professor=%s draft_id=%d words=%d",
        prof.name,
        draft_id,
        len(body.split()),
    )
    return followup
