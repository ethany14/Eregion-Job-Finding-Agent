"""Validated, server-owned queries for deterministic memory retrieval."""

from __future__ import annotations

from pydantic import Field, field_validator

from eregion.runtime.memory.types import MemoryType
from eregion.runtime.types import RuntimeModel


class MemoryQuery(RuntimeModel):
    owner_id: str = Field(min_length=1, max_length=128)
    text: str = Field(default="", max_length=20_000)
    session_id: str | None = Field(default=None, max_length=128)
    project_id: str | None = Field(default=None, max_length=128)
    # The application this request concerns; its exceptions (legacy PROJECT "application:<id>")
    # apply only here.
    application_id: str | None = Field(default=None, max_length=128)
    # Accepted USER scope IDs (owner ID plus legacy aliases). Empty keeps the older behaviour:
    # any USER scope ID of this owner.
    user_scope_ids: frozenset[str] = Field(default_factory=frozenset)
    memory_types: frozenset[MemoryType] = Field(default_factory=frozenset)
    required_memory_keys: frozenset[str] = Field(default_factory=frozenset)
    allow_personal: bool = False
    allow_sensitive: bool = False
    minimum_confidence: float = Field(default=0.0, ge=0, le=1)
    limit: int = Field(default=10, ge=1, le=100)
    token_budget: int = Field(default=1_000, ge=1, le=100_000)

    @field_validator("owner_id", "text", "session_id", "project_id", "application_id", mode="before")
    @classmethod
    def clean_text(cls, value: str | None) -> str | None:
        return None if value is None else value.strip()

    @field_validator("required_memory_keys")
    @classmethod
    def validate_keys(cls, values: frozenset[str]) -> frozenset[str]:
        allowed = set("abcdefghijklmnopqrstuvwxyz0123456789._:-")
        for value in values:
            if not value or any(character not in allowed for character in value):
                raise ValueError("Required memory keys must be stable lowercase identifiers.")
        return values
