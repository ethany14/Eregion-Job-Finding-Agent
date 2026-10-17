"""Stage 1 migration: normalise legacy local owner/profile IDs on USER memory.

preview() is read-only. apply() changes only rows the preview marks as safe, in one transaction,
records every change as a scope_migrated audit event holding the old IDs, and is repeatable (a
second run finds nothing to change). Records that would leave two confirmed values for the same
key are listed as conflicts and left untouched: they need a decision, not a newest-wins rule.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from eregion.runtime.memory.events import MemoryEvent, MemoryEventType
from eregion.runtime.memory.models import MemoryItemRow
from eregion.runtime.memory.repository import MemoryRepository
from eregion.runtime.memory.types import MemoryItem, MemoryScope, MemoryStatus


@dataclass
class PlannedChange:
    memory_id: str
    memory_key: str
    status: str
    from_owner_id: str
    from_scope_id: str
    to_owner_id: str
    to_scope_id: str


@dataclass
class MigrationPlan:
    owner_id: str
    legacy_ids: list[str]
    changes: list[PlannedChange] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    unchanged: int = 0

    def to_dict(self) -> dict:
        return {"owner_id": self.owner_id, "legacy_ids": self.legacy_ids, "unchanged": self.unchanged,
                "changes": [asdict(c) for c in self.changes], "conflicts": self.conflicts}


def preview(session_factory: sessionmaker[Session], *, owner_id: str = "local-user",
            legacy_ids: tuple[str, ...] = ("default",)) -> MigrationPlan:
    aliases = {owner_id, *legacy_ids}
    plan = MigrationPlan(owner_id=owner_id, legacy_ids=list(legacy_ids))
    with session_factory() as session:
        rows = session.scalars(select(MemoryItemRow).where(MemoryItemRow.owner_id.in_(sorted(aliases)))).all()
        items = [MemoryRepository._item(row) for row in rows]
    def target(item: MemoryItem) -> tuple[str, str]:
        to_owner = owner_id if item.owner_id in aliases else item.owner_id
        to_scope = owner_id if item.scope == MemoryScope.USER and item.scope_id in aliases else item.scope_id
        return to_owner, to_scope

    # Confirmed values per (scope, scope ID, key) after normalisation; the database allows one.
    confirmed: dict[tuple, list[MemoryItem]] = {}
    for item in items:
        if item.status == MemoryStatus.CONFIRMED:
            confirmed.setdefault((item.scope.value, *target(item), item.memory_key), []).append(item)
    conflicted = set()
    for (scope, _, scope_id, key), values in confirmed.items():
        if len(values) > 1:
            conflicted.update(v.memory_id for v in values)
            plan.conflicts.append({"memory_key": key, "scope": scope, "scope_id": scope_id, "records": [
                {"memory_id": v.memory_id, "owner_id": v.owner_id, "scope_id": v.scope_id,
                 "display_text": v.display_text, "updated_at": v.updated_at.isoformat()} for v in values]})
    for item in items:
        to_owner, to_scope = target(item)
        if (to_owner, to_scope) == (item.owner_id, item.scope_id):
            plan.unchanged += 1
            continue
        if item.memory_id in conflicted:
            continue
        plan.changes.append(PlannedChange(item.memory_id, item.memory_key, item.status.value,
                                          item.owner_id, item.scope_id, to_owner, to_scope))
    return plan


def apply(session_factory: sessionmaker[Session], plan: MigrationPlan) -> int:
    """Apply a preview's changes; each row must still be in the previewed state."""
    now = datetime.now(UTC)
    applied = 0
    with session_factory.begin() as session:
        for change in plan.changes:
            row = session.get(MemoryItemRow, change.memory_id)
            if row is None or (row.owner_id, row.scope_id) != (change.from_owner_id, change.from_scope_id):
                raise RuntimeError(f"Memory {change.memory_id} changed after the preview; rerun the preview.")
            current = MemoryRepository._item(row)
            moved = MemoryItem.model_validate({**current.model_dump(mode="python"), "owner_id": change.to_owner_id,
                "scope_id": change.to_scope_id, "version": current.version + 1,
                "event_sequence": current.event_sequence + 1, "updated_at": now})
            result = session.execute(update(MemoryItemRow).where(
                MemoryItemRow.memory_id == change.memory_id, MemoryItemRow.version == current.version
            ).values(**MemoryRepository._projection(moved)))
            if result.rowcount != 1:
                raise RuntimeError(f"Memory {change.memory_id} has a newer version; rerun the preview.")
            session.add(MemoryRepository._event_row(MemoryEvent(
                memory_id=moved.memory_id, sequence=moved.event_sequence, event_type=MemoryEventType.SCOPE_MIGRATED,
                from_status=current.status, to_status=moved.status, actor_type="system", occurred_at=now,
                payload={"from_owner_id": change.from_owner_id, "from_scope_id": change.from_scope_id,
                         "to_owner_id": change.to_owner_id, "to_scope_id": change.to_scope_id})))
            applied += 1
    return applied
