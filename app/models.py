"""Dataclass models for the Academic Outreach Email System."""

from __future__ import annotations

import json
import sqlite3
import dataclasses
from dataclasses import dataclass, field as dc_field, asdict
from datetime import datetime
from typing import Any, Optional


def _now_iso() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.utcnow().isoformat()


def _parse_json_list(raw: Optional[str]) -> list[str]:
    """Safely parse a JSON string that should contain a list of strings."""
    if not raw:
        return []
    try:
        parsed: Any = json.loads(raw)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
        return []
    except (json.JSONDecodeError, TypeError):
        return []


def _serialize_list(items: list[str]) -> str:
    """Serialize a list of strings to a JSON string."""
    return json.dumps(items)


def _row_to_dict(row: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
    """Convert a sqlite3.Row or dict to a plain dict."""
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return dict(row)


# ---------------------------------------------------------------------------
# SenderProfile
# ---------------------------------------------------------------------------

@dataclass
class SenderProfile:
    """A student/sender who initiates outreach."""

    id: Optional[int] = None
    name: str = ""
    school: str = ""
    grade: str = ""
    email: str = ""
    interests: str = ""
    background: str = ""
    graduation_year: Optional[str] = None
    created_at: str = dc_field(default_factory=_now_iso)

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: dict[str, Any] | sqlite3.Row) -> SenderProfile:
        data: dict[str, Any] = _row_to_dict(row)
        return cls(
            id=data.get("id"),
            name=data.get("name", ""),
            school=data.get("school", ""),
            grade=data.get("grade", ""),
            email=data.get("email", ""),
            interests=data.get("interests", ""),
            background=data.get("background", ""),
            graduation_year=data.get("graduation_year"),
            created_at=data.get("created_at", _now_iso()),
        )


# ---------------------------------------------------------------------------
# Professor
# ---------------------------------------------------------------------------

PROFESSOR_STATUSES: frozenset[str] = frozenset({"new", "enriched", "ready", "skip", "error"})


@dataclass
class Professor:
    """A professor targeted for outreach."""

    id: Optional[int] = None
    name: str = ""
    title: Optional[str] = None
    email: str = ""
    university: str = ""
    department: str = ""
    lab_name: Optional[str] = None
    field: str = ""
    profile_url: Optional[str] = None
    research_summary: Optional[str] = None
    recent_work: Optional[str] = None
    notes: Optional[str] = None
    enrichment_text: Optional[str] = None
    keywords: Optional[str] = None          # JSON string of list
    summary: Optional[str] = None
    talking_points: Optional[str] = None    # JSON string of list
    status: str = "new"
    created_at: str = dc_field(default_factory=_now_iso)
    updated_at: str = dc_field(default_factory=_now_iso)

    # -- JSON helpers --------------------------------------------------------

    @property
    def keywords_list(self) -> list[str]:
        return _parse_json_list(self.keywords)

    @keywords_list.setter
    def keywords_list(self, items: list[str]) -> None:
        self.keywords = _serialize_list(items)

    @property
    def talking_points_list(self) -> list[str]:
        return _parse_json_list(self.talking_points)

    @talking_points_list.setter
    def talking_points_list(self, items: list[str]) -> None:
        self.talking_points = _serialize_list(items)

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: dict[str, Any] | sqlite3.Row) -> Professor:
        data: dict[str, Any] = _row_to_dict(row)
        return cls(
            id=data.get("id"),
            name=data.get("name", ""),
            title=data.get("title"),
            email=data.get("email", ""),
            university=data.get("university", ""),
            department=data.get("department", ""),
            lab_name=data.get("lab_name"),
            field=data.get("field", ""),
            profile_url=data.get("profile_url"),
            research_summary=data.get("research_summary"),
            recent_work=data.get("recent_work"),
            notes=data.get("notes"),
            enrichment_text=data.get("enrichment_text"),
            keywords=data.get("keywords"),
            summary=data.get("summary"),
            talking_points=data.get("talking_points"),
            status=data.get("status", "new"),
            created_at=data.get("created_at", _now_iso()),
            updated_at=data.get("updated_at", _now_iso()),
        )


# ---------------------------------------------------------------------------
# Draft
# ---------------------------------------------------------------------------

DRAFT_STATUSES: frozenset[str] = frozenset({
    "generated", "approved", "rejected", "edited", "sent", "failed",
})


@dataclass
class Draft:
    """A generated outreach email draft."""

    id: Optional[int] = None
    professor_id: int = 0
    sender_profile_id: int = 0
    session_id: int = 0
    subject_lines: str = "[]"              # JSON string of list of 3
    body: str = ""
    template_variant: str = ""
    specificity_score: float = 0.0
    authenticity_score: float = 0.0
    relevance_score: float = 0.0
    conciseness_score: float = 0.0
    completeness_score: float = 0.0
    overall_score: float = 0.0
    similarity_score: Optional[float] = None
    warnings: str = "[]"                   # JSON string of list
    status: str = "generated"
    created_at: str = dc_field(default_factory=_now_iso)
    reviewed_at: Optional[str] = None
    reviewer_notes: Optional[str] = None

    # -- JSON helpers --------------------------------------------------------

    @property
    def subject_lines_list(self) -> list[str]:
        return _parse_json_list(self.subject_lines)

    @subject_lines_list.setter
    def subject_lines_list(self, items: list[str]) -> None:
        self.subject_lines = _serialize_list(items)

    @property
    def warnings_list(self) -> list[str]:
        return _parse_json_list(self.warnings)

    @warnings_list.setter
    def warnings_list(self, items: list[str]) -> None:
        self.warnings = _serialize_list(items)

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: dict[str, Any] | sqlite3.Row) -> Draft:
        data: dict[str, Any] = _row_to_dict(row)
        return cls(
            id=data.get("id"),
            professor_id=data.get("professor_id", 0),
            sender_profile_id=data.get("sender_profile_id", 0),
            session_id=data.get("session_id", 0),
            subject_lines=data.get("subject_lines", "[]"),
            body=data.get("body", ""),
            template_variant=data.get("template_variant", ""),
            specificity_score=float(data.get("specificity_score", 0.0)),
            authenticity_score=float(data.get("authenticity_score", 0.0)),
            relevance_score=float(data.get("relevance_score", 0.0)),
            conciseness_score=float(data.get("conciseness_score", 0.0)),
            completeness_score=float(data.get("completeness_score", 0.0)),
            overall_score=float(data.get("overall_score", 0.0)),
            similarity_score=(
                float(data["similarity_score"])
                if data.get("similarity_score") is not None
                else None
            ),
            warnings=data.get("warnings", "[]"),
            status=data.get("status", "generated"),
            created_at=data.get("created_at", _now_iso()),
            reviewed_at=data.get("reviewed_at"),
            reviewer_notes=data.get("reviewer_notes"),
        )


# ---------------------------------------------------------------------------
# SendRecord
# ---------------------------------------------------------------------------

SEND_METHODS: frozenset[str] = frozenset({"gmail_draft", "gmail_send", "smtp"})
SEND_STATUSES: frozenset[str] = frozenset({"success", "failed", "bounced"})


@dataclass
class SendRecord:
    """Tracks an actual send or draft-creation event."""

    id: Optional[int] = None
    draft_id: int = 0
    professor_id: int = 0
    sent_at: str = dc_field(default_factory=_now_iso)
    method: str = "gmail_draft"
    gmail_draft_id: Optional[str] = None
    status: str = "success"
    error_message: Optional[str] = None
    message_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: dict[str, Any] | sqlite3.Row) -> SendRecord:
        data: dict[str, Any] = _row_to_dict(row)
        return cls(
            id=data.get("id"),
            draft_id=data.get("draft_id", 0),
            professor_id=data.get("professor_id", 0),
            sent_at=data.get("sent_at", _now_iso()),
            method=data.get("method", "gmail_draft"),
            gmail_draft_id=data.get("gmail_draft_id"),
            status=data.get("status", "success"),
            error_message=data.get("error_message"),
            message_id=data.get("message_id"),
        )


# ---------------------------------------------------------------------------
# FollowUp
# ---------------------------------------------------------------------------

FOLLOWUP_STATUSES: frozenset[str] = frozenset({"generated", "approved", "sent"})


@dataclass
class FollowUp:
    """A follow-up email linked to a previous draft."""

    id: Optional[int] = None
    original_draft_id: int = 0
    professor_id: int = 0
    sender_profile_id: int = 0
    body: str = ""
    subject: str = ""
    status: str = "generated"
    scheduled_date: Optional[str] = None
    created_at: str = dc_field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: dict[str, Any] | sqlite3.Row) -> FollowUp:
        data: dict[str, Any] = _row_to_dict(row)
        return cls(
            id=data.get("id"),
            original_draft_id=data.get("original_draft_id", 0),
            professor_id=data.get("professor_id", 0),
            sender_profile_id=data.get("sender_profile_id", 0),
            body=data.get("body", ""),
            subject=data.get("subject", ""),
            status=data.get("status", "generated"),
            scheduled_date=data.get("scheduled_date"),
            created_at=data.get("created_at", _now_iso()),
        )


# ---------------------------------------------------------------------------
# AuditEntry
# ---------------------------------------------------------------------------

@dataclass
class AuditEntry:
    """Immutable audit-log row."""

    id: Optional[int] = None
    timestamp: str = dc_field(default_factory=_now_iso)
    action: str = ""
    actor_profile_id: Optional[int] = None
    entity_type: str = ""
    entity_id: Optional[int] = None
    details: str = "{}"                    # JSON string

    # -- JSON helpers --------------------------------------------------------

    @property
    def details_dict(self) -> dict[str, Any]:
        if not self.details:
            return {}
        try:
            parsed: Any = json.loads(self.details)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    @details_dict.setter
    def details_dict(self, value: dict[str, Any]) -> None:
        self.details = json.dumps(value)

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: dict[str, Any] | sqlite3.Row) -> AuditEntry:
        data: dict[str, Any] = _row_to_dict(row)
        return cls(
            id=data.get("id"),
            timestamp=data.get("timestamp", _now_iso()),
            action=data.get("action", ""),
            actor_profile_id=data.get("actor_profile_id"),
            entity_type=data.get("entity_type", ""),
            entity_id=data.get("entity_id"),
            details=data.get("details", "{}"),
        )


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

@dataclass
class Session:
    """Groups a batch of drafts together."""

    id: Optional[int] = None
    sender_profile_id: int = 0
    created_at: str = dc_field(default_factory=_now_iso)
    notes: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: dict[str, Any] | sqlite3.Row) -> Session:
        data: dict[str, Any] = _row_to_dict(row)
        return cls(
            id=data.get("id"),
            sender_profile_id=data.get("sender_profile_id", 0),
            created_at=data.get("created_at", _now_iso()),
            notes=data.get("notes"),
        )
