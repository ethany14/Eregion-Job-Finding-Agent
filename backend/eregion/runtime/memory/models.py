"""SQLAlchemy rows for governed memory and audit events."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from eregion.api.db import Base


class MemoryItemRow(Base):
    __tablename__ = "memory_items"
    __table_args__ = (
        CheckConstraint("memory_type IN ('semantic','preference','episodic')", name="ck_memory_items_type"),
        CheckConstraint("scope_type IN ('user','project','session','application')", name="ck_memory_items_scope"),
        CheckConstraint("status IN ('candidate','confirmed','rejected','superseded','deleted','expired')", name="ck_memory_items_status"),
        CheckConstraint("sensitivity IN ('normal','personal','sensitive')", name="ck_memory_items_sensitivity"),
        Index(
            "uq_memory_active_confirmed_key",
            "owner_id", "scope_type", "scope_id", "memory_key",
            unique=True,
            sqlite_where=text("status = 'confirmed'"),
        ),
    )

    memory_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    scope_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    memory_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    memory_type: Mapped[str] = mapped_column(String(16), nullable=False)
    display_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    provenance_json: Mapped[str] = mapped_column(Text, nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    supersedes_memory_id: Mapped[str | None] = mapped_column(ForeignKey("memory_items.memory_id", ondelete="SET NULL"))
    superseded_by_memory_id: Mapped[str | None] = mapped_column(ForeignKey("memory_items.memory_id", ondelete="SET NULL"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    item_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class MemoryEventRow(Base):
    __tablename__ = "memory_events"
    __table_args__ = (UniqueConstraint("memory_id", "sequence", name="uq_memory_event_sequence"),)

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    memory_id: Mapped[str] = mapped_column(ForeignKey("memory_items.memory_id", ondelete="CASCADE"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
