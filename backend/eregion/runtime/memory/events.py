"""Append-only memory audit events."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import Field

from eregion.runtime.memory.types import MemoryStatus
from eregion.runtime.types import RuntimeModel


class MemoryEventType(StrEnum):
    CANDIDATE_CREATED = "candidate_created"
    CANDIDATE_REJECTED_BY_POLICY = "candidate_rejected_by_policy"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    SUPERSESSION_LINKED = "supersession_linked"
    DELETED = "deleted"
    EXPIRED = "expired"
    # Stage 1 migration: legacy owner/profile IDs normalised; payload keeps the old IDs.
    SCOPE_MIGRATED = "scope_migrated"
    # Stage 2: the same statement seen again; its source is added, no new record is made.
    PROVENANCE_MERGED = "provenance_merged"


class MemoryEvent(RuntimeModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    memory_id: str
    sequence: int = Field(default=0, ge=0)
    event_type: MemoryEventType
    from_status: MemoryStatus | None = None
    to_status: MemoryStatus
    actor_type: str = Field(default="system", min_length=1, max_length=32)
    payload: dict = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
