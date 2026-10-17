"""Scoped memory retrieval with semantic relevance and a lexical fallback."""

from __future__ import annotations

from datetime import UTC
from typing import Protocol

from pydantic import Field

from eregion.runtime.context.budget import estimate_tokens
from eregion.runtime.context.types import ContextBlock
from eregion.runtime.memory.access import application_scope_id, legacy_application_id
from eregion.runtime.memory.query import MemoryQuery
from eregion.runtime.memory.rendering import render_memory_item
from eregion.runtime.memory.scoring import MemoryScoreBreakdown, score_memory
from eregion.runtime.memory.types import MemoryItem, MemoryScope, MemorySensitivity, MemoryStatus
from eregion.runtime.sessions.clock import Clock, SystemClock
from eregion.runtime.types import RuntimeModel


class ConfirmedMemoryReader(Protocol):
    def list_confirmed(
        self, *, owner_id: str, scope: MemoryScope | None = None, scope_id: str | None = None
    ) -> list[MemoryItem]: ...


class RetrievedMemory(RuntimeModel):
    memory: MemoryItem
    score: MemoryScoreBreakdown
    rendered_block: ContextBlock


class MemoryRetrievalResult(RuntimeModel):
    items: list[RetrievedMemory]
    omitted_memory_ids: list[str] = Field(default_factory=list)
    estimated_tokens: int = Field(ge=0)


_SCOPE_RANK = {MemoryScope.USER: 1, MemoryScope.PROJECT: 2, MemoryScope.SESSION: 4}


def _rank(item: MemoryItem) -> int:
    # An application exception (legacy PROJECT "application:<id>") overrides user and project
    # values for its own application only; a session value stays the most specific.
    return 3 if legacy_application_id(item) is not None else _SCOPE_RANK[item.scope]


class MemoryRetriever:
    def __init__(self, reader: ConfirmedMemoryReader, *, clock: Clock | None = None,
                 semantic_ranker=None) -> None:
        self._reader = reader
        self._clock = clock or SystemClock()
        self._semantic_ranker = semantic_ranker

    def retrieve(self, query: MemoryQuery) -> MemoryRetrievalResult:
        candidates = list(self._reader.list_confirmed(
            owner_id=query.owner_id, scope=MemoryScope.USER
        ))
        if query.project_id:
            candidates.extend(self._reader.list_confirmed(
                owner_id=query.owner_id, scope=MemoryScope.PROJECT, scope_id=query.project_id
            ))
        if query.session_id:
            candidates.extend(self._reader.list_confirmed(
                owner_id=query.owner_id, scope=MemoryScope.SESSION, scope_id=query.session_id
            ))
        if query.application_id:
            candidates.extend(self._reader.list_confirmed(
                owner_id=query.owner_id, scope=MemoryScope.PROJECT,
                scope_id=application_scope_id(query.application_id),
            ))
        filtered = [item for item in candidates if self._allowed(item, query)]
        resolved = self._resolve_keys(filtered)
        scored: list[tuple[MemoryItem, MemoryScoreBreakdown]] = [
            (item, score_memory(item, query.text, query.required_memory_keys))
            for item in resolved
        ]
        relevant_ids = set()
        if self._semantic_ranker and scored:
            # Apply ownership, sensitivity, expiry and scope BEFORE sharing any
            # candidate with the model. Bound cost even for a large memory store.
            bounded = sorted(scored, key=lambda pair: (-pair[1].total, pair[0].memory_id))[:40]
            try:
                relevant_ids = set(self._semantic_ranker(query.text, [m for m, _ in bounded]))
                relevant_ids &= {m.memory_id for m, _ in bounded}
            except Exception:
                relevant_ids = set()  # lexical fallback stays useful during outages
        scored = [(item, score.model_copy(update={"total": score.total + 8.0})
                   if item.memory_id in relevant_ids else score) for item, score in scored]
        # Confidence alone does not make a Memory relevant. Required keys bypass
        # lexical matching; every other item needs an actual query match.
        scored = [
            (item, score)
            for item, score in scored
            if score.required_key_priority > 0 or bool(score.matched_tokens) or item.memory_id in relevant_ids
        ]
        scored.sort(key=lambda pair: (
            -pair[1].required_key_priority,
            -pair[1].total,
            -_rank(pair[0]),
            -pair[0].updated_at.timestamp(),
            pair[0].memory_key,
            pair[0].memory_id,
        ))
        selected: list[RetrievedMemory] = []
        omitted: list[str] = []
        used = 0
        for item, score in scored:
            if len(selected) >= query.limit:
                omitted.append(item.memory_id)
                continue
            block = render_memory_item(item, priority=score.total)
            cost = block.estimated_tokens or estimate_tokens(block.content)
            if used + cost > query.token_budget:
                omitted.append(item.memory_id)
                continue
            selected.append(RetrievedMemory(memory=item, score=score, rendered_block=block))
            used += cost
        return MemoryRetrievalResult(items=selected, omitted_memory_ids=omitted,
                                     estimated_tokens=used)

    def _allowed(self, item: MemoryItem, query: MemoryQuery) -> bool:
        now = self._clock.now()
        now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
        expires = item.expires_at
        if expires is not None:
            expires = expires.replace(tzinfo=UTC) if expires.tzinfo is None else expires.astimezone(UTC)
        application = legacy_application_id(item)
        allowed_scope = (
            (item.scope == MemoryScope.USER
             and (not query.user_scope_ids or item.scope_id in query.user_scope_ids))
            or (item.scope == MemoryScope.PROJECT and application is None and item.scope_id == query.project_id)
            or (application is not None and application == query.application_id)
            or (item.scope == MemoryScope.SESSION and item.scope_id == query.session_id)
        )
        if item.status != MemoryStatus.CONFIRMED or item.owner_id != query.owner_id or not allowed_scope:
            return False
        if expires is not None and expires <= now:
            return False
        if item.confidence < query.minimum_confidence:
            return False
        if query.memory_types and item.memory_type not in query.memory_types:
            return False
        if item.sensitivity == MemorySensitivity.PERSONAL and not query.allow_personal:
            return False
        if item.sensitivity == MemorySensitivity.SENSITIVE and not query.allow_sensitive:
            return False
        return True

    @staticmethod
    def _resolve_keys(items: list[MemoryItem]) -> list[MemoryItem]:
        by_key: dict[str, MemoryItem] = {}
        for item in sorted(items, key=lambda value: (
            -_rank(value), -value.updated_at.timestamp(), value.memory_id
        )):
            by_key.setdefault(item.memory_key, item)
        return list(by_key.values())
