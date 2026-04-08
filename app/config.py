"""
Central configuration module for the Academic Outreach Email System.

Loads secrets from .env (via python-dotenv) and all application settings
from config.yaml.  Exposes a frozen Config dataclass produced by load_config().
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Project root is one level above the app/ package
# ---------------------------------------------------------------------------
_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Default config.yaml content (written when the file is missing)
# ---------------------------------------------------------------------------
DEFAULT_CONFIG_YAML: Dict[str, Any] = {
    "scoring": {
        "weights": {
            "specificity": 0.30,
            "authenticity": 0.20,
            "relevance": 0.20,
            "conciseness": 0.15,
            "completeness": 0.15,
        },
        "thresholds": {
            "minimum_score": 4.0,
            "high_quality": 7.5,
            "genericness_threshold": 0.70,
        },
    },
    "sending": {
        "rate_limit_per_hour": 15,
        "session_cap": 30,
        "cooldown_min": 30,
        "cooldown_max": 90,
    },
    "generation": {
        "word_count_min": 150,
        "word_count_max": 350,
        "similarity_threshold": 0.85,
        "template_variants": [
            "formal",
            "enthusiastic",
            "concise",
            "research_focused",
        ],
    },
    "variation_pools": {
        "greetings": [
            "Dear Professor {last_name},",
            "Dear Dr. {last_name},",
            "Dear Prof. {last_name},",
        ],
        "openers": [
            "I came across your {source} while looking for research in {field}, and I was especially interested in your work on {topic}.",
            "I found your profile while exploring research in {field}, and your work on {topic} stood out to me.",
            "While researching opportunities in {field}, I came across your work on {topic} and wanted to reach out.",
            "I have been looking into research in {field} and was drawn to your work on {topic}.",
        ],
        "transitions": [
            "What stood out to me was",
            "What especially caught my attention was",
            "I was particularly interested in",
            "I found it compelling that",
        ],
        "interest_connectors": [
            "I have been spending a lot of time building my background in {area}, and I am especially interested in work that {connection}.",
            "I have been focused on developing my skills in {area}, and problems where {connection} are what interest me most.",
            "I have been working on building a foundation in {area}, and your research seemed like a strong example of {connection}.",
        ],
        "asks": [
            "I wanted to ask whether there might be any way for a student at my stage to get involved, even through a small technical task, reading direction, or another entry point into your group's work.",
            "I am reaching out to see if there might be any opportunity for a high school student to contribute, whether through a small task, guided reading, or another way to start learning from your group.",
            "I would be grateful for any chance to learn from your group, even if it is just a small project, reading recommendation, or skill-building direction.",
        ],
        "fallbacks": [
            "If that is not possible, I would still really appreciate any recommendation for a paper, topic, or skill that would be worth studying seriously.",
            "If there is no opening available, I would still value any suggestion for a paper or topic I should study to build a stronger background in this area.",
            "Even if there is no opportunity right now, I would be grateful for any guidance on what to read or study next.",
        ],
        "signoffs": [
            "Thank you very much for your time and consideration.",
            "Thank you for taking the time to read this.",
            "I appreciate you taking the time to consider this.",
        ],
        "closings": [
            "Sincerely,",
            "Best regards,",
            "Respectfully,",
        ],
    },
    "followup_pools": {
        "openers": [
            "I hope you are doing well. I wanted to follow up on my earlier email in case it got buried.",
            "I wanted to briefly follow up on my previous email.",
            "I hope this finds you well. I am writing to follow up on my earlier message.",
        ],
        "interest_restatements": [
            "I am still very interested in your work on {topic}, especially {hook}.",
            "I remain very interested in your research, particularly {topic}, and I would still be grateful for any advice on how a student at my stage could begin learning more seriously in this area.",
            "Your work on {topic} continues to interest me, and I wanted to reiterate my interest in finding a way to learn from your group.",
        ],
        "soft_asks": [
            "I know you are busy, so I completely understand if there is no opportunity available. Even so, if there is a paper, topic, or skill you think I should focus on to build a stronger background in this area, I would really appreciate the guidance.",
            "If there is no opening in your group, I would still really appreciate a recommendation for a paper, topic, or technical skill to study.",
            "I understand that you may not have availability, but any guidance on what to study or read would mean a lot.",
        ],
    },
    "fields": [
        "Computer Science",
        "Artificial Intelligence",
        "Machine Learning",
        "Data Science",
        "Fintech",
        "Finance",
        "Blockchain",
        "Cryptocurrency",
        "Computational Biology",
        "Bioinformatics",
        "Applied Mathematics",
        "Electrical Engineering",
        "Computer Engineering",
        "Robotics",
        "Operations Research",
        "Systems Engineering",
    ],
    "llm_models": {
        "openrouter": {
            "gemini-flash": "google/gemini-2.5-flash-preview",
            "gemini-pro": "google/gemini-2.5-pro-preview",
            "claude-haiku": "anthropic/claude-haiku-4-5-20251001",
            "claude-sonnet": "anthropic/claude-sonnet-4-6",
            "claude-opus": "anthropic/claude-opus-4-6",
        },
        "default_model": "google/gemini-2.5-flash-preview",
    },
    "email_providers": {
        "gmail": {
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 587,
            "imap_host": "imap.gmail.com",
        },
        "outlook": {
            "smtp_host": "smtp-mail.outlook.com",
            "smtp_port": 587,
            "imap_host": "outlook.office365.com",
        },
        "hotmail": {
            "smtp_host": "smtp-mail.outlook.com",
            "smtp_port": 587,
            "imap_host": "outlook.office365.com",
        },
    },
    "targeting": {
        "preferred_titles": [
            "Assistant Professor",
            "Associate Professor",
        ],
        "good_signals": [
            "student researchers",
            "undergraduate members",
            "mentoring",
            "lab alumni",
            "open positions",
            "current projects",
            "summer students",
            "education and outreach",
            "REU",
        ],
    },
}


# ---------------------------------------------------------------------------
# Dataclasses that mirror config.yaml structure
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ScoringWeights:
    specificity: float
    authenticity: float
    relevance: float
    conciseness: float
    completeness: float


@dataclass(frozen=True)
class ScoringThresholds:
    minimum_score: float
    high_quality: float
    genericness_threshold: float


@dataclass(frozen=True)
class ScoringConfig:
    weights: ScoringWeights
    thresholds: ScoringThresholds


@dataclass(frozen=True)
class SendingConfig:
    rate_limit_per_hour: int
    session_cap: int
    cooldown_min: int
    cooldown_max: int


@dataclass(frozen=True)
class GenerationConfig:
    word_count_min: int
    word_count_max: int
    similarity_threshold: float
    template_variants: List[str]


@dataclass(frozen=True)
class FollowUpPools:
    openers: List[str]
    interest_restatements: List[str]
    soft_asks: List[str]


@dataclass(frozen=True)
class VariationPools:
    greetings: List[str]
    openers: List[str]
    transitions: List[str]
    interest_connectors: List[str]
    asks: List[str]
    fallbacks: List[str]
    signoffs: List[str]
    closings: List[str]


@dataclass(frozen=True)
class Config:
    """Top-level application configuration (immutable after creation)."""

    # --- secrets / env vars ---
    gmail_credentials_path: str
    gmail_token_path: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    sender_email: str
    llm_provider: Optional[str]
    llm_api_key: Optional[str]
    llm_model: str
    email_provider: str
    db_path: str
    log_dir: str
    output_dir: str

    # --- yaml settings ---
    scoring: ScoringConfig = field(default_factory=lambda: _default_scoring())
    sending: SendingConfig = field(default_factory=lambda: _default_sending())
    generation: GenerationConfig = field(default_factory=lambda: _default_generation())
    variation_pools: VariationPools = field(default_factory=lambda: _default_variation_pools())
    followup_pools: FollowUpPools = field(default_factory=lambda: _default_followup_pools())
    fields: List[str] = field(default_factory=lambda: list(DEFAULT_CONFIG_YAML["fields"]))


# ---------------------------------------------------------------------------
# Helpers to build defaults from DEFAULT_CONFIG_YAML
# ---------------------------------------------------------------------------
def _default_scoring() -> ScoringConfig:
    s = DEFAULT_CONFIG_YAML["scoring"]
    return ScoringConfig(
        weights=ScoringWeights(**s["weights"]),
        thresholds=ScoringThresholds(**s["thresholds"]),
    )


def _default_sending() -> SendingConfig:
    s = DEFAULT_CONFIG_YAML["sending"]
    return SendingConfig(**s)


def _default_generation() -> GenerationConfig:
    g = DEFAULT_CONFIG_YAML["generation"]
    return GenerationConfig(**g)


def _default_variation_pools() -> VariationPools:
    v = DEFAULT_CONFIG_YAML["variation_pools"]
    return VariationPools(**v)


def _default_followup_pools() -> FollowUpPools:
    f = DEFAULT_CONFIG_YAML["followup_pools"]
    return FollowUpPools(**f)


# ---------------------------------------------------------------------------
# Config‑file I/O
# ---------------------------------------------------------------------------
def _ensure_config_yaml(path: Path) -> Dict[str, Any]:
    """Return parsed config.yaml, creating it with defaults if missing."""
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                yaml.dump(
                    DEFAULT_CONFIG_YAML,
                    fh,
                    default_flow_style=False,
                    sort_keys=False,
                    allow_unicode=True,
                )
        except OSError:
            # Read-only filesystem (e.g. Vercel) — use defaults only
            return dict(DEFAULT_CONFIG_YAML)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data: Dict[str, Any] = yaml.safe_load(fh) or {}
        return data
    except OSError:
        return dict(DEFAULT_CONFIG_YAML)


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge *override* into *base*, returning a new dict."""
    merged: Dict[str, Any] = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
class ConfigError(Exception):
    """Raised when configuration validation fails."""


def _env(
    name: str,
    default: Optional[str] = None,
    required: bool = False,
    db_settings: Optional[Dict[str, str]] = None,
) -> str:
    """Read a config value.  Priority: DB setting → env var → default."""
    # 1. Check DB settings first (key is lowercase version of env var name)
    if db_settings:
        db_key = name.lower()
        db_val = db_settings.get(db_key)
        if db_val:
            return db_val
    # 2. Env var
    value: Optional[str] = os.getenv(name, default)
    if required and not value:
        raise ConfigError(f"Required environment variable {name!r} is not set.")
    return value or ""


def _validate_env_vars(
    llm_provider: Optional[str],
    llm_api_key: Optional[str],
    smtp_port: int,
) -> None:
    """Cross-field validation on environment-sourced values."""
    if llm_provider and llm_provider.lower() not in ("openai", "anthropic", "openrouter"):
        raise ConfigError(
            f"LLM_PROVIDER must be 'openai', 'anthropic', 'openrouter', or unset. Got: {llm_provider!r}"
        )
    if llm_provider and not llm_api_key:
        raise ConfigError(
            f"LLM_PROVIDER is set to {llm_provider!r} but LLM_API_KEY is missing."
        )
    if not (0 < smtp_port < 65536):
        raise ConfigError(f"SMTP_PORT must be 1-65535. Got: {smtp_port}")


def _validate_yaml(data: Dict[str, Any]) -> None:
    """Sanity-check values loaded from config.yaml."""
    scoring = data.get("scoring", {})
    weights = scoring.get("weights", {})
    weight_sum = sum(weights.values())
    if weights and not (0.99 <= weight_sum <= 1.01):
        raise ConfigError(
            f"Scoring weights must sum to 1.0 (got {weight_sum:.4f})."
        )

    thresholds = scoring.get("thresholds", {})
    min_score = thresholds.get("minimum_score", 0)
    high_pri = thresholds.get("high_quality", 1)
    if min_score >= high_pri:
        raise ConfigError(
            "scoring.thresholds.minimum_score must be less than high_priority."
        )

    sending = data.get("sending", {})
    if sending.get("cooldown_min", 0) > sending.get("cooldown_max", 0):
        raise ConfigError("sending.cooldown_min must be <= cooldown_max.")

    gen = data.get("generation", {})
    if gen.get("word_count_min", 0) > gen.get("word_count_max", 0):
        raise ConfigError("generation.word_count_min must be <= word_count_max.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def load_config(
    project_root: Optional[Path] = None,
    env_path: Optional[Path] = None,
    yaml_path: Optional[Path] = None,
) -> Config:
    """
    Load, validate, and return the application Config.

    Parameters
    ----------
    project_root : Path, optional
        Defaults to the repo root (parent of app/).
    env_path : Path, optional
        Explicit path to the .env file.
    yaml_path : Path, optional
        Explicit path to the config.yaml file.
    """
    root: Path = project_root or _PROJECT_ROOT
    env_file: Path = env_path or (root / ".env")
    config_file: Path = yaml_path or (root / "config.yaml")

    # 1. Load .env into os.environ
    load_dotenv(dotenv_path=env_file, override=False)

    # 2. Load / create config.yaml and merge with defaults
    raw_yaml: Dict[str, Any] = _ensure_config_yaml(config_file)
    merged: Dict[str, Any] = _deep_merge(DEFAULT_CONFIG_YAML, raw_yaml)

    # 3. Validate yaml
    _validate_yaml(merged)

    # 4. Load runtime settings from DB (if DB exists already)
    db_path_initial: str = os.getenv("DB_PATH", str(root / "data" / "outreach.db"))
    db_settings: Dict[str, str] = {}
    try:
        import sqlite3 as _sqlite3
        _db_file = Path(db_path_initial)
        if _db_file.exists():
            _tmp_conn = _sqlite3.connect(str(_db_file))
            _tmp_conn.row_factory = _sqlite3.Row
            try:
                rows = _tmp_conn.execute(
                    "SELECT key, value FROM app_settings"
                ).fetchall()
                db_settings = {r["key"]: r["value"] for r in rows}
            except _sqlite3.OperationalError:
                pass  # Table doesn't exist yet — first run
            finally:
                _tmp_conn.close()
    except Exception:
        pass  # DB not reachable — use env vars only

    # 5. Read env vars (DB settings take priority via _env())
    gmail_credentials_path: str = _env(
        "GMAIL_CREDENTIALS_PATH", str(root / "credentials.json"), db_settings=db_settings,
    )
    gmail_token_path: str = _env("GMAIL_TOKEN_PATH", str(root / "token.json"), db_settings=db_settings)
    # Determine email provider to set SMTP defaults
    _email_provider_raw: str = _env("EMAIL_PROVIDER", "gmail", db_settings=db_settings).lower()
    _provider_defaults: Dict[str, Any] = merged.get("email_providers", {}).get(
        _email_provider_raw, {"smtp_host": "smtp.gmail.com", "smtp_port": 587}
    )
    smtp_host: str = _env("SMTP_HOST", _provider_defaults.get("smtp_host", "smtp.gmail.com"), db_settings=db_settings)
    smtp_port_raw: str = _env("SMTP_PORT", str(_provider_defaults.get("smtp_port", 587)), db_settings=db_settings)
    try:
        smtp_port: int = int(smtp_port_raw)
    except ValueError as exc:
        raise ConfigError(f"SMTP_PORT must be an integer. Got: {smtp_port_raw!r}") from exc

    smtp_user: str = _env("SMTP_USER", "", db_settings=db_settings)
    smtp_password: str = _env("SMTP_PASSWORD", "", db_settings=db_settings)
    sender_email: str = _env("SENDER_EMAIL", "", db_settings=db_settings)
    llm_provider_raw: str = _env("LLM_PROVIDER", "", db_settings=db_settings)
    llm_provider: Optional[str] = llm_provider_raw if llm_provider_raw else None
    llm_api_key_raw: str = _env("LLM_API_KEY", "", db_settings=db_settings)
    llm_api_key: Optional[str] = llm_api_key_raw if llm_api_key_raw else None
    llm_model: str = _env("LLM_MODEL", merged.get("llm_models", {}).get("default_model", "google/gemini-2.5-flash-preview"), db_settings=db_settings)
    email_provider: str = _env("EMAIL_PROVIDER", "gmail", db_settings=db_settings).lower()
    db_path: str = _env("DB_PATH", str(root / "data" / "outreach.db"), db_settings=db_settings)
    log_dir: str = _env("LOG_DIR", str(root / "logs"), db_settings=db_settings)
    output_dir: str = _env("OUTPUT_DIR", str(root / "outputs"), db_settings=db_settings)

    # 6. Cross-field env validation
    _validate_env_vars(llm_provider, llm_api_key, smtp_port)

    # 7. Build nested dataclasses from merged yaml
    scoring_cfg = ScoringConfig(
        weights=ScoringWeights(**merged["scoring"]["weights"]),
        thresholds=ScoringThresholds(**merged["scoring"]["thresholds"]),
    )
    sending_cfg = SendingConfig(**merged["sending"])
    generation_cfg = GenerationConfig(**merged["generation"])
    variation_cfg = VariationPools(**merged["variation_pools"])
    followup_cfg = FollowUpPools(**merged["followup_pools"])
    fields_list: List[str] = list(merged.get("fields", DEFAULT_CONFIG_YAML["fields"]))

    # 8. On Vercel (serverless), only /tmp is writable
    if os.environ.get("VERCEL"):
        if not db_path.startswith("/tmp"):
            db_path = "/tmp/outreach.db"
        if not log_dir.startswith("/tmp"):
            log_dir = "/tmp/logs"
        if not output_dir.startswith("/tmp"):
            output_dir = "/tmp/outputs"

    # 9. Ensure critical directories exist
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    return Config(
        gmail_credentials_path=gmail_credentials_path,
        gmail_token_path=gmail_token_path,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_password=smtp_password,
        sender_email=sender_email,
        llm_provider=llm_provider,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        email_provider=email_provider,
        db_path=db_path,
        log_dir=log_dir,
        output_dir=output_dir,
        scoring=scoring_cfg,
        sending=sending_cfg,
        generation=generation_cfg,
        variation_pools=variation_cfg,
        followup_pools=followup_cfg,
        fields=fields_list,
    )
