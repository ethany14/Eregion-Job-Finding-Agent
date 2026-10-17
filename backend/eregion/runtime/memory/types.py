"""Versioned contracts for memory data and provenance."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from eregion.runtime.types import RuntimeModel


class MemoryType(StrEnum):
    SEMANTIC = "semantic"
    PREFERENCE = "preference"
    EPISODIC = "episodic"


class MemoryScope(StrEnum):
    USER = "user"
    PROJECT = "project"
    SESSION = "session"


class MemoryStatus(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    DELETED = "deleted"
    EXPIRED = "expired"


class MemorySensitivity(StrEnum):
    NORMAL = "normal"
    PERSONAL = "personal"
    SENSITIVE = "sensitive"


class MemoryProvenance(RuntimeModel):
    schema_version: int = Field(default=1, ge=1)
    source_type: str = Field(min_length=1, max_length=64)
    source_id: str | None = Field(default=None, max_length=128)
    actor_type: str = Field(default="agent", min_length=1, max_length=32)
    user_confirmed: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryItem(RuntimeModel):
    """Stored data only; no field in this contract is executable instruction."""

    schema_version: int = Field(default=1, ge=1)
    memory_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    owner_id: str = Field(min_length=1, max_length=128)
    scope: MemoryScope
    scope_id: str = Field(min_length=1, max_length=128)
    memory_key: str = Field(min_length=1, max_length=160)
    memory_type: MemoryType
    display_text: str = Field(min_length=1, max_length=2_000)
    content: dict[str, Any] = Field(default_factory=dict)
    status: MemoryStatus = MemoryStatus.CANDIDATE
    provenance: list[MemoryProvenance] = Field(min_length=1)
    sensitivity: MemorySensitivity = MemorySensitivity.NORMAL
    confidence: float = Field(default=1.0, ge=0, le=1)
    supersedes_memory_id: str | None = Field(default=None, max_length=128)
    superseded_by_memory_id: str | None = Field(default=None, max_length=128)
    expires_at: datetime | None = None
    version: int = Field(default=0, ge=0)
    event_sequence: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_used_at: datetime | None = None

    @field_validator("owner_id", "scope_id", "memory_key", "display_text")
    @classmethod
    def clean_required_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Memory identifiers and display text must not be blank.")
        return cleaned

    @field_validator("memory_key")
    @classmethod
    def stable_key_format(cls, value: str) -> str:
        allowed = set("abcdefghijklmnopqrstuvwxyz0123456789._:-")
        if any(character not in allowed for character in value):
            raise ValueError("memory_key must be a stable lowercase identifier.")
        return value

    @model_validator(mode="after")
    def validate_links(self) -> "MemoryItem":
        if self.memory_id in {self.supersedes_memory_id, self.superseded_by_memory_id}:
            raise ValueError("A memory cannot supersede itself.")
        if self.status == MemoryStatus.SUPERSEDED and not self.superseded_by_memory_id:
            raise ValueError("A superseded memory requires its replacement ID.")
        # A deleted record keeps its replacement link so the supersession history stays
        # traceable (memory upgrade stage 1); every reader excludes deleted records.
        if self.status not in {MemoryStatus.SUPERSEDED, MemoryStatus.DELETED} and self.superseded_by_memory_id:
            raise ValueError("Only superseded or deleted memory may identify a replacement.")
        return self
