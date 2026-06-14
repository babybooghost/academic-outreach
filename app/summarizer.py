"""
Summarization module for the Academic Outreach Email System.

Provides two strategies behind a common Protocol:
  1. KeywordSummarizer -- offline extraction using YAKE
  2. LLMSummarizer    -- API-based extraction (OpenAI / Anthropic / OpenRouter)

A factory function ``get_summarizer`` selects the right strategy based on
config, and ``summarize_all`` processes professors in batch.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Optional, Protocol

import requests as http_requests

from app.config import Config
from app.database import get_connection, get_professors, update_professor
from app.logger import get_logger, audit_log
from app.models import Professor

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class SummarizerStrategy(Protocol):
    """Common interface for summarization strategies."""

    def summarize(
        self,
        text: str,
        professor: Professor,
    ) -> tuple[list[str], str]:
        """
        Extract keywords and produce a summary paragraph.

        Returns
        -------
        tuple[list[str], str]
            (keyword_list, summary_paragraph)
        """
        ...


# ---------------------------------------------------------------------------
# Strategy 1: YAKE-based keyword extraction (no API required)
# ---------------------------------------------------------------------------

class KeywordSummarizer:
    """
    Offline summarizer using YAKE for keyword extraction.

    Cross-references extracted keywords with the professor's CSV-provided
    research_summary and field to build a plain-English summary.
    """

    _TOP_N: int = 10

    def summarize(
        self,
        text: str,
        professor: Professor,
    ) -> tuple[list[str], str]:
        """Extract keywords via YAKE and build a template summary."""
        keywords: list[str] = self._extract_keywords(text)

        # Cross-reference with CSV-provided metadata
        csv_terms: list[str] = self._csv_terms(professor)
        if csv_terms:
            keywords = self._cross_reference(keywords, csv_terms)

        if not keywords:
            keywords = csv_terms[:self._TOP_N] if csv_terms else ["research"]

        summary: str = self._build_summary(professor.name, keywords)
        return keywords, summary

    # -- internal helpers ----------------------------------------------------

    def _extract_keywords(self, text: str) -> list[str]:
        """Use YAKE to extract top-N keywords from *text*."""
        if not text or not text.strip():
            return []

        try:
            import yake  # type: ignore[import-untyped]

            extractor = yake.KeywordExtractor(
                lan="en",
                n=2,           # up to 2-grams
                dedupLim=0.7,  # deduplication threshold
                top=self._TOP_N,
                features=None,
            )
            raw_keywords: list[tuple[str, float]] = extractor.extract_keywords(text)
            return [kw for kw, _score in raw_keywords]
        except ImportError:
            logger.warning(
                "YAKE is not installed; falling back to naive keyword extraction"
            )
            return self._naive_keywords(text)
        except Exception as exc:
            logger.error("YAKE extraction failed: %s", exc)
            return self._naive_keywords(text)

    def _naive_keywords(self, text: str) -> list[str]:
        """
        Very simple fallback: pick the most frequent multi-char,
        non-stopword tokens.
        """
        stopwords: frozenset[str] = frozenset({
            "the", "and", "for", "that", "with", "this", "from", "are",
            "was", "were", "been", "have", "has", "had", "but", "not",
            "they", "their", "our", "his", "her", "its", "can", "will",
            "also", "into", "more", "than", "which", "about", "such",
            "each", "other", "through", "between", "over", "after",
            "before", "under", "these", "those", "both", "some", "any",
        })
        words: list[str] = text.lower().split()
        freq: dict[str, int] = {}
        for w in words:
            cleaned: str = w.strip(".,;:!?()[]{}\"'")
            if len(cleaned) > 3 and cleaned not in stopwords and cleaned.isalpha():
                freq[cleaned] = freq.get(cleaned, 0) + 1
        sorted_words: list[str] = sorted(freq, key=freq.get, reverse=True)  # type: ignore[arg-type]
        return sorted_words[: self._TOP_N]

    @staticmethod
    def _csv_terms(professor: Professor) -> list[str]:
        """Gather terms from the professor's CSV-provided metadata."""
        terms: list[str] = []
        if professor.field:
            terms.extend(
                t.strip() for t in professor.field.replace("/", ",").split(",")
                if t.strip()
            )
        if professor.research_summary:
            # Take the first few significant words as supplementary terms
            words = professor.research_summary.split()
            stopwords = {"and", "the", "of", "in", "for", "to", "a", "an", "on", "with"}
            terms.extend(
                w.strip(".,;:!?()") for w in words
                if w.lower().strip(".,;:!?()") not in stopwords
                and len(w.strip(".,;:!?()")) > 3
            )
        # Deduplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for t in terms:
            lower: str = t.lower()
            if lower not in seen:
                seen.add(lower)
                unique.append(t)
        return unique

    @staticmethod
    def _cross_reference(
        keywords: list[str],
        csv_terms: list[str],
    ) -> list[str]:
        """
        Boost keywords that overlap with CSV metadata by placing them first.
        """
        csv_lower: set[str] = {t.lower() for t in csv_terms}
        overlapping: list[str] = [k for k in keywords if k.lower() in csv_lower]
        non_overlapping: list[str] = [k for k in keywords if k.lower() not in csv_lower]
        return overlapping + non_overlapping

    @staticmethod
    def _build_summary(name: str, keywords: list[str]) -> str:
        """Build a plain-English summary from the professor's name and keywords."""
        if not keywords:
            return f"{name}'s research spans multiple areas of interest."

        top_keyword: str = keywords[0]
        if len(keywords) >= 3:
            kw_str: str = ", ".join(keywords[:5])
            return (
                f"{name}'s research focuses on {kw_str}, "
                f"with particular emphasis on {top_keyword}."
            )
        elif len(keywords) == 2:
            return (
                f"{name}'s research focuses on {keywords[0]} and {keywords[1]}, "
                f"with particular emphasis on {top_keyword}."
            )
        else:
            return (
                f"{name}'s research centers on {top_keyword}."
            )


# ---------------------------------------------------------------------------
# Strategy 2: LLM-based summarization (requires API key)
# ---------------------------------------------------------------------------

class LLMSummarizer:
    """
    API-based summarizer supporting openai, anthropic, and openrouter providers.

    Falls back to KeywordSummarizer on any failure.
    """

    _PROMPT_TEMPLATE: str = (
        "You are analyzing a faculty member's research page to support a "
        "student's genuine, specific outreach. From the text below, identify "
        "the professor's ACTUAL research focus: concrete topics, methods, "
        "problems, and applications they work on. Use only what the text "
        "supports — never invent papers, results, or details.\n\n"
        "Return ONLY JSON in exactly this shape:\n"
        '{{"keywords": ["specific topic", "specific method", ...], '
        '"summary": "..."}}\n\n'
        "Rules:\n"
        "- 5-8 keywords, each SPECIFIC (e.g. 'graph neural networks', "
        "'cryo-EM', 'mechanism design' — not 'computer science' or 'research').\n"
        "- summary: 2-3 factual sentences naming concrete topics/methods the "
        "professor works on. No flattery, no filler, no 'renowned/leading'.\n\n"
        "Text:\n{text}"
    )
    _MAX_TEXT_FOR_LLM: int = 3000
    _FALLBACK: KeywordSummarizer = KeywordSummarizer()

    def __init__(self, provider: str, api_key: str, model: str = "") -> None:
        self._provider: str = provider.lower()
        self._api_key: str = api_key
        self._model: str = model

    def summarize(
        self,
        text: str,
        professor: Professor,
    ) -> tuple[list[str], str]:
        """Send text to the LLM provider and parse the JSON response."""
        if not text or not text.strip():
            return self._FALLBACK.summarize(text, professor)

        truncated: str = text[: self._MAX_TEXT_FOR_LLM]
        prompt: str = self._PROMPT_TEMPLATE.format(text=truncated)

        try:
            raw_response: str = self._call_llm(prompt)
            keywords, summary = self._parse_response(raw_response)
            if keywords and summary:
                return keywords, summary
            logger.warning(
                "LLM returned incomplete data for %s; falling back to YAKE",
                professor.name,
            )
            return self._FALLBACK.summarize(text, professor)
        except Exception as exc:
            logger.error(
                "LLM summarization failed for %s: %s; falling back to YAKE",
                professor.name, exc,
            )
            return self._FALLBACK.summarize(text, professor)

    # -- provider dispatch ---------------------------------------------------

    def _call_llm(self, prompt: str) -> str:
        """Dispatch to the configured provider and return the raw text response."""
        if self._provider == "openrouter":
            return self._call_openrouter(prompt)
        raise ValueError(f"Unsupported LLM provider: {self._provider}")

    def _call_openrouter(self, prompt: str) -> str:
        """Call OpenRouter's chat completions API via requests."""
        url: str = "https://openrouter.ai/api/v1/chat/completions"
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self._model or "google/gemini-2.5-flash",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 500,
        }
        response = http_requests.post(
            url, headers=headers, json=payload, timeout=30
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        choices: list[dict[str, Any]] = data.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "")
        return ""

    # -- response parsing ----------------------------------------------------

    @staticmethod
    def _parse_response(raw: str) -> tuple[list[str], str]:
        """
        Parse the LLM's JSON response into keywords and summary.

        Attempts to find a JSON block in the response, handling cases where
        the LLM wraps its response in markdown code fences.
        """
        text: str = raw.strip()

        # Strip markdown code fences if present
        if text.startswith("```"):
            lines: list[str] = text.split("\n")
            # Remove first and last lines (fences)
            if len(lines) >= 3:
                text = "\n".join(lines[1:-1]).strip()

        # Try to find JSON in the text
        start: int = text.find("{")
        end: int = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return [], ""

        json_str: str = text[start : end + 1]
        try:
            data: dict[str, Any] = json.loads(json_str)
            keywords: list[str] = [str(k) for k in data.get("keywords", [])]
            summary: str = str(data.get("summary", ""))
            return keywords, summary
        except (json.JSONDecodeError, TypeError):
            return [], ""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

# Cheap, capable model for reading/summarizing professor pages. Used unless a
# workspace explicitly picks a parsing model in Setup. The "Test AI" probe
# reports the model OpenRouter actually serves so this can be verified live.
DEFAULT_PARSE_MODEL: str = "google/gemini-3.5-flash"


def probe_openrouter(api_key: str, model: str) -> dict[str, Any]:
    """One tiny OpenRouter call that reports which model actually answered.

    Returns ``{ok, served_model, text, error}``. ``served_model`` is the model
    OpenRouter says it ran (its response ``model`` field) — the ground truth for
    "did it really use the model I picked?". Never raises.
    """
    try:
        resp = http_requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Reply with exactly the word: ready"}],
                # Generous budget: thinking-capable models (e.g. Gemini Flash) spend
                # tokens on internal reasoning before any visible output, so a tiny
                # cap can come back empty even though the model works fine in real use.
                "max_tokens": 256,
            },
            timeout=30,
        )
        try:
            data = resp.json()
        except Exception:
            return {"ok": False, "served_model": "", "text": "", "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        if resp.status_code >= 400:
            msg = (data.get("error") or {}).get("message") if isinstance(data, dict) else None
            return {"ok": False, "served_model": "", "text": "", "error": msg or f"HTTP {resp.status_code}"}
        served = data.get("model", "") if isinstance(data, dict) else ""
        choices = data.get("choices") or [] if isinstance(data, dict) else []
        text = (choices[0].get("message", {}).get("content", "") if choices else "")
        if not text:
            return {"ok": False, "served_model": served, "text": "",
                    "error": "model returned no visible text (it may route output to internal reasoning)"}
        return {"ok": True, "served_model": served, "text": text, "error": ""}
    except Exception as exc:
        return {"ok": False, "served_model": "", "text": "", "error": str(exc)}


def get_summarizer(config: Config) -> SummarizerStrategy:
    """
    Return the appropriate summarizer strategy based on config.

    Uses LLMSummarizer if an LLM provider and API key are configured,
    otherwise falls back to KeywordSummarizer.
    """
    if config.llm_provider and config.llm_api_key:
        logger.info(
            "Using LLM summarizer with provider: %s", config.llm_provider
        )
        return LLMSummarizer(
            provider=config.llm_provider,
            api_key=config.llm_api_key,
            # Parsing/summarizing professor pages is extraction, not writing, so
            # it defaults to the cheapest capable model. A workspace can override
            # this with an explicit parsing model; the premium writing model is
            # reserved for composing the actual emails.
            model=config.llm_model_parse or DEFAULT_PARSE_MODEL,
        )
    logger.info("Using keyword-based summarizer (no LLM configured)")
    return KeywordSummarizer()


# ---------------------------------------------------------------------------
# Single professor summarization
# ---------------------------------------------------------------------------

def summarize_professor(
    prof: Professor,
    config: Config,
) -> Professor:
    """
    Summarize a single professor's research and update their record.

    Uses enrichment_text if available, otherwise falls back to combining
    research_summary and recent_work from the CSV import.

    Parameters
    ----------
    prof : Professor
        The professor to summarize.
    config : Config
        Application configuration.

    Returns
    -------
    Professor
        The updated professor (same object, mutated in place).
    """
    # Determine source text
    text: str = ""
    if prof.enrichment_text and prof.enrichment_text.strip():
        text = prof.enrichment_text
    else:
        parts: list[str] = []
        if prof.research_summary:
            parts.append(prof.research_summary)
        if prof.recent_work:
            parts.append(prof.recent_work)
        text = " ".join(parts)

    if not text.strip():
        logger.info(
            "No text available for summarization of %s -- skipping", prof.name
        )
        return prof

    strategy: SummarizerStrategy = get_summarizer(config)
    keywords, summary = strategy.summarize(text, prof)

    prof.keywords_list = keywords
    prof.summary = summary

    logger.info(
        "Summarized %s: %d keywords, summary length %d",
        prof.name, len(keywords), len(summary),
    )
    return prof


# ---------------------------------------------------------------------------
# Batch summarization
# ---------------------------------------------------------------------------

def summarize_all(
    db_path: str,
    config: Config,
) -> tuple[int, int]:
    """
    Summarize all professors with status='enriched'.

    Parameters
    ----------
    db_path : str
        Path to the SQLite database.
    config : Config
        Application configuration.

    Returns
    -------
    tuple[int, int]
        (summarized_count, failed_count)
    """
    summarized: int = 0
    failed: int = 0

    conn: sqlite3.Connection = get_connection(db_path)
    try:
        professors: list[Professor] = get_professors(conn, status="enriched")
        total: int = len(professors)
        logger.info("Starting summarization for %d professor(s)", total)

        for prof in professors:
            try:
                summarize_professor(prof, config)

                if prof.keywords and prof.summary:
                    if prof.id is not None:
                        update_professor(conn, prof)
                    summarized += 1
                    audit_log(
                        action="summarization_success",
                        detail=(
                            f"Summarized professor '{prof.name}' "
                            f"({len(prof.keywords_list)} keywords)"
                        ),
                        metadata={
                            "professor_id": prof.id,
                            "professor_email": prof.email,
                            "keyword_count": len(prof.keywords_list),
                        },
                        db_path=db_path,
                    )
                else:
                    failed += 1
                    audit_log(
                        action="summarization_empty",
                        detail=f"No summary produced for '{prof.name}'",
                        metadata={
                            "professor_id": prof.id,
                            "professor_email": prof.email,
                        },
                        db_path=db_path,
                    )
            except Exception as exc:
                failed += 1
                logger.error(
                    "Error summarizing %s: %s", prof.name, exc
                )
                audit_log(
                    action="summarization_error",
                    detail=f"Error summarizing '{prof.name}': {exc}",
                    metadata={
                        "professor_id": prof.id,
                        "professor_email": prof.email,
                    },
                    db_path=db_path,
                )

    finally:
        conn.close()

    summary_msg: str = (
        f"Summarization complete: {summarized} summarized, {failed} failed "
        f"out of {total} total"
    )
    audit_log(
        action="summarization_batch_complete",
        detail=summary_msg,
        metadata={"summarized": summarized, "failed": failed, "total": total},
        db_path=db_path,
    )
    logger.info(summary_msg)

    return summarized, failed
