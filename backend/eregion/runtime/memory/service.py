"""One read entry for usable memory (memory upgrade stage 1).

- effective_preferences: the confirmed rules that apply to a task kind (resume, cover letter,
  application, writing). Explicit rules are read by key prefix, never by whether the user's
  wording happens to match.
- recall: memories relevant to the current text, through the scoped retriever.
- get_usable: a single item named by ID (for example handed to a sub-task), under the same
  checks.

All three share MemoryAccessContext.usable; nothing here decides what a memory means.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from eregion.runtime.memory.access import MemoryAccessContext
from eregion.runtime.memory.query import MemoryQuery
from eregion.runtime.memory.retrieval import MemoryRetrievalResult, MemoryRetriever
from eregion.runtime.memory.types import MemoryItem, MemoryType
from eregion.runtime.sessions.clock import Clock, SystemClock

TASK_PREFIXES = {
    "application_materials": ("resume.", "cover_letter.", "application.", "writing."),
    "resume": ("resume.", "writing."),
    "cover_letter": ("cover_letter.", "writing."),
}


@dataclass(frozen=True)
class EffectiveMemory:
    item: MemoryItem
    reason: str = "usable"


@dataclass
class EffectivePreferences:
    items: list[EffectiveMemory] = field(default_factory=list)
    # Same key, same specificity, different confirmed records (e.g. legacy "default" and the
    # owner ID). Reported, never silently merged; the newer one is used meanwhile.
    conflicts: list[dict] = field(default_factory=list)
    excluded: dict[str, str] = field(default_factory=dict)

    def as_payload(self) -> list[dict]:
        return [{"memory_id": e.item.memory_id, "version": e.item.version, "memory_key": e.item.memory_key,
                 "display_text": e.item.display_text, "content": e.item.content} for e in self.items]


class MemoryService:
    def __init__(self, repository, *, retriever: MemoryRetriever | None = None, clock: Clock | None = None) -> None:
        self._repository = repository
        self._retriever = retriever
        self._clock = clock or SystemClock()

    def effective_preferences(self, context: MemoryAccessContext | None, task_kind: str) -> EffectivePreferences:
        result = EffectivePreferences()
        if context is None:
            return result
        prefixes = TASK_PREFIXES.get(task_kind)
        if prefixes is None:
            raise ValueError(f"Unknown memory task kind: {task_kind}")
        now = self._clock.now()
        by_key: dict[str, list[MemoryItem]] = {}
        for item in self._repository.list_confirmed(owner_id=context.owner_id):
            if item.memory_type != MemoryType.PREFERENCE or not item.memory_key.startswith(prefixes):
                continue
            ok, reason = context.usable(item, now=now)
            if not ok:
                result.excluded[item.memory_id] = reason
                continue
            by_key.setdefault(item.memory_key, []).append(item)
        for key in sorted(by_key):
            values = sorted(by_key[key], key=lambda i: (-context.specificity(i), -i.updated_at.timestamp(), i.memory_id))
            chosen = values[0]
            peers = [i for i in values[1:] if context.specificity(i) == context.specificity(chosen)]
            if peers:
                result.conflicts.append({"memory_key": key, "used": chosen.memory_id,
                                         "also_confirmed": [i.memory_id for i in peers]})
            for other in values[1:]:
                result.excluded[other.memory_id] = "overridden_by_more_specific" if other not in peers else "conflict_older"
            result.items.append(EffectiveMemory(chosen))
        return result

    def recall(self, query_text: str, context: MemoryAccessContext | None, *, token_budget: int = 1_000,
               limit: int = 10, required_memory_keys: frozenset[str] = frozenset()) -> MemoryRetrievalResult:
        if context is None or self._retriever is None:
            return MemoryRetrievalResult(items=[], estimated_tokens=0)
        return self._retriever.retrieve(MemoryQuery(
            owner_id=context.owner_id, text=query_text, session_id=context.session_id,
            project_id=context.project_id, application_id=context.application_id,
            user_scope_ids=context.user_scope_ids, allow_personal=context.allow_personal,
            required_memory_keys=required_memory_keys, token_budget=token_budget, limit=limit))

    def get_usable(self, memory_id: str, context: MemoryAccessContext | None) -> MemoryItem | None:
        if context is None:
            return None
        item = self._repository.get(memory_id, owner_id=context.owner_id)
        if item is None:
            return None
        ok, _ = context.usable(item, now=self._clock.now())
        return item if ok else None
