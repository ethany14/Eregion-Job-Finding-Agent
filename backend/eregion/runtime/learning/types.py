"""Versioned contracts for learning from persisted conversation turns."""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from typing import Literal

from pydantic import Field, field_validator

from eregion.runtime.types import RuntimeModel


class ExperienceSignalType(StrEnum):
    PREFERENCE = "preference"
    CAREER_FACT = "career_fact"
    CORRECTION = "correction"
    PROCEDURAL_SUCCESS = "procedural_success"
    PROCEDURAL_FAILURE = "procedural_failure"
    NONE = "none"


class LearningPatternStatus(StrEnum):
    COLLECTING = "collecting"
    READY_FOR_REVIEW = "ready_for_review"
    CANDIDATE_EMITTED = "candidate_emitted"
    REJECTED = "rejected"


class ExperienceSignal(RuntimeModel):
    """A bounded observation. It is data and never an executable instruction."""

    signal_type: ExperienceSignalType
    canonical_key: str = Field(default="", max_length=160)
    summary: str = Field(default="", max_length=1_000)
    evidence_quote: str | None = Field(default=None, max_length=2_000)
    normalized_value: str | int | float | bool | list[str] | None = None
    confidence: float = Field(default=0, ge=0, le=1)
    reusable_across_sessions: bool = False
    sensitive: bool = False
    rationale: str = Field(default="", max_length=500)
    # Stage 2 (proposed by the extractor, validated by the server before any write):
    # who the statement is about; facts quoted from a job description or another person are
    # "quoted_other" and are never learned as the user's own facts or preferences.
    attribution: Literal["user_self", "quoted_other"] = "user_self"
    # Where it applies: everywhere, only the application this conversation is about, or only
    # this conversation. Temporary statements ("today", "this time") never become long-term.
    scope_hint: Literal["user", "application", "session"] = "user"
    durability: Literal["durable", "temporary"] = "durable"
    # When the stated fact holds, as ISO dates resolved against the message date; null = unknown.
    valid_from: str | None = Field(default=None, max_length=40)
    valid_to: str | None = Field(default=None, max_length=40)
    applies_to: dict[str, str] = Field(default_factory=dict)

    @field_validator("canonical_key")
    @classmethod
    def clean_key(cls, value: str) -> str:
        return value.strip().casefold()


class LearningObservation(RuntimeModel):
    schema_version: int = 1
    signals: list[ExperienceSignal] = Field(default_factory=list, max_length=8)
    turn_outcome: str = Field(default="unknown", max_length=64)


class ConversationExperience(RuntimeModel):
    schema_version: int = 1
    experience_id: str = Field(default_factory=lambda: str(uuid4()))
    owner_id: str
    session_id: str
    application_id: str | None = None
    user_message_id: str
    assistant_message_id: str
    source_hash: str
    observation: LearningObservation | None = None
    status: str = "observed"
    error_code: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LearningPattern(RuntimeModel):
    schema_version: int = 1
    pattern_id: str = Field(default_factory=lambda: str(uuid4()))
    owner_id: str
    canonical_key: str
    proposed_instruction: str
    status: LearningPatternStatus = LearningPatternStatus.COLLECTING
    experience_ids: list[str] = Field(default_factory=list)
    session_ids: list[str] = Field(default_factory=list)
    positive_count: int = Field(default=0, ge=0)
    negative_count: int = Field(default=0, ge=0)
    emitted_feedback_event_id: str | None = None
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def occurrence_count(self) -> int:
        return len(self.experience_ids)

    @property
    def session_count(self) -> int:
        return len(self.session_ids)
