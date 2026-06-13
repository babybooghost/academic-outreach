"""Deliverability guardrails: daily send caps and spam-trigger detection.

Keeps cold outreach from looking like spam and from tripping Gmail's
volume limits. Pure functions + light DB reads; no sending happens here.
"""

from __future__ import annotations

import re
from typing import Any

# Gmail personal accounts realistically tolerate well under their hard cap for
# *cold* mail before reputation suffers. This is a soft, advisory ceiling.
DAILY_SEND_SOFT_CAP: int = 40

# Phrases/patterns that push cold email toward spam folders. Each entry is
# (compiled regex, human explanation).
_SPAM_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(act now|limited time|urgent|don'?t miss|last chance)\b", re.I),
     "urgency language ('act now', 'limited time')"),
    (re.compile(r"\b(free|100% free|no cost|risk[- ]free|guarantee[d]?)\b", re.I),
     "promotional words ('free', 'guaranteed')"),
    (re.compile(r"\b(click here|buy now|order now|sign up now|subscribe)\b", re.I),
     "call-to-action spam phrasing ('click here', 'buy now')"),
    (re.compile(r"\b(dear sir or madam|dear sir/madam|to whom it may concern)\b", re.I),
     "impersonal greeting (use the professor's name)"),
    (re.compile(r"\b(winner|congratulations|cash|prize|earn \$|make money)\b", re.I),
     "scammy/financial words"),
    (re.compile(r"!{3,}"), "excessive exclamation marks"),
    (re.compile(r"\$\d"), "dollar amounts"),
]


def scan_spam(text: str) -> list[str]:
    """Return human-readable spam-risk flags for an email body/subject.

    Also flags shouting (lots of ALL-CAPS words). Empty list = looks clean.
    """
    body = text or ""
    issues: list[str] = []
    for pattern, label in _SPAM_PATTERNS:
        if pattern.search(body):
            issues.append(label)

    caps_words = re.findall(r"\b[A-Z]{4,}\b", body)
    # Ignore common legitimate acronyms in academic outreach.
    caps_words = [w for w in caps_words if w not in {"REU", "PHD", "STEM", "MIT", "UCLA", "USA"}]
    if len(caps_words) >= 3:
        issues.append("several ALL-CAPS words (reads as shouting)")

    return issues


def daily_send_count(conn: Any) -> int:
    """Count successful sends for the active workspace since midnight UTC.

    Relies on the workspace-bound connection so it is naturally tenant-scoped.
    """
    try:
        wid = getattr(conn, "workspace_id", 0)
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM send_log "
            "WHERE workspace_id = ? AND status = 'success' AND sent_at >= date('now')",
            (wid,),
        ).fetchone()
        return int(row["c"]) if row else 0
    except Exception:
        return 0


def cap_status(sent_today: int, queued: int, cap: int = DAILY_SEND_SOFT_CAP) -> dict[str, Any]:
    """Summarize where a workspace stands against the soft daily cap."""
    remaining = max(0, cap - sent_today)
    over = (sent_today + queued) > cap
    return {
        "sent_today": sent_today,
        "cap": cap,
        "remaining": remaining,
        "queued": queued,
        "over_cap": over,
    }
