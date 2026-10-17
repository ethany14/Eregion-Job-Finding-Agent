"""Memory stage 2: decide how a new statement relates to existing records, then write it.

| relation to existing records of the same concept | action                                  |
|--------------------------------------------------|-----------------------------------------|
| same scope, same key, same value                 | add the source to it (idempotent)       |
| same scope, same key, different confirmed value  | replacement candidate; old stays in use |
| narrower scope than an existing broader value    | separate local record; global untouched |
| otherwise                                        | new candidate                           |

Nothing is confirmed here; confirmation and supersession remain explicit user actions. Scope,
key and value arrive already decided by the extractor and validated by the server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from eregion.runtime.memory.normalization import normalize_key, normalize_value
from eregion.runtime.memory.types import MemoryItem, MemoryProvenance, MemoryScope, MemoryStatus


class Relation(StrEnum):
    DUPLICATE = "duplicate"
    REPLACEMENT = "replacement"
    NARROWER_EXCEPTION = "narrower_exception"
    NEW = "new"


_BREADTH = {MemoryScope.USER: 0, MemoryScope.PROJECT: 1, MemoryScope.APPLICATION: 2, MemoryScope.SESSION: 3}


@dataclass
class Consolidation:
    relation: Relation
    existing: MemoryItem | None = None
    broader: list[str] = field(default_factory=list)


def _value(item: MemoryItem):
    return normalize_value(item.memory_key, item.content.get("value"))


def classify(existing: list[MemoryItem], *, scope: MemoryScope, scope_id: str, key: str, value,
             canonical_scope_id=lambda scope, scope_id: scope_id) -> Consolidation:
    key, value = normalize_key(key), normalize_value(normalize_key(key), value)
    target = canonical_scope_id(scope, scope_id)
    live = [i for i in existing if i.memory_key == key and i.status in {MemoryStatus.CANDIDATE, MemoryStatus.CONFIRMED}]
    same_scope = [i for i in live if i.scope == scope and canonical_scope_id(i.scope, i.scope_id) == target]
    for item in same_scope:
        if _value(item) == value:
            return Consolidation(Relation.DUPLICATE, existing=item)
    confirmed = next((i for i in same_scope if i.status == MemoryStatus.CONFIRMED), None)
    if confirmed is not None:
        return Consolidation(Relation.REPLACEMENT, existing=confirmed)
    broader = [i.memory_id for i in live if i.status == MemoryStatus.CONFIRMED and _BREADTH[i.scope] < _BREADTH[scope]]
    if broader:
        return Consolidation(Relation.NARROWER_EXCEPTION, broader=broader)
    return Consolidation(Relation.NEW)


def write_statement(repository, *, owner_id: str, scope: MemoryScope, scope_id: str, key: str, value,
                    display_text: str, memory_type, provenance: MemoryProvenance, memory_id: str | None = None,
                    sensitivity=None, confidence: float = 1.0, valid_from=None, valid_to=None, expires_at=None,
                    applies_to: dict | None = None, extra_content: dict | None = None,
                    canonical_scope_id=None) -> tuple[MemoryItem, Consolidation]:
    """Classify, then merge or create a candidate. Returns (record, decision)."""
    canonical = canonical_scope_id or (lambda s, i: i)
    key = normalize_key(key)
    value = normalize_value(key, value)
    existing = repository.list_items(owner_id=owner_id, statuses={MemoryStatus.CANDIDATE, MemoryStatus.CONFIRMED})
    decision = classify(existing, scope=scope, scope_id=scope_id, key=key, value=value, canonical_scope_id=canonical)
    if decision.relation == Relation.DUPLICATE:
        item = repository.add_provenance(decision.existing.memory_id, owner_id=owner_id,
                                         expected_version=decision.existing.version, provenance=provenance)
        return item, decision
    content = {"value": value, **(extra_content or {})}
    if decision.relation == Relation.REPLACEMENT:
        content["replaces_memory_id"] = decision.existing.memory_id
    if decision.relation == Relation.NARROWER_EXCEPTION:
        content["narrows_memory_ids"] = decision.broader
    kwargs = {} if sensitivity is None else {"sensitivity": sensitivity}
    item = repository.create_candidate(owner_id=owner_id, scope=scope, scope_id=canonical(scope, scope_id),
        memory_key=key, memory_type=memory_type, display_text=display_text, content=content,
        provenance=[provenance], confidence=confidence, expires_at=expires_at, memory_id=memory_id,
        valid_from=valid_from, valid_to=valid_to, applies_to=applies_to or {}, **kwargs)
    return item, decision
