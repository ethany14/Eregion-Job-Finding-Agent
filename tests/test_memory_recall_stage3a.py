"""Memory upgrade stage 3a: the semantic ranker sees every in-scope candidate (no top-40 cut).

The ranker is a deterministic fake that recognises one meaning-only match; this tests the
retrieval mechanics, not a model's recall quality.
"""
from datetime import UTC, datetime

import pytest

from eregion.runtime.memory.query import MemoryQuery
from eregion.runtime.memory.retrieval import SEMANTIC_MAX_BATCHES, MemoryRetriever
from eregion.runtime.memory.types import MemoryItem, MemoryProvenance, MemoryScope, MemoryStatus, MemoryType

NOW = datetime(2026, 10, 17, tzinfo=UTC)


class Clock:
    def now(self):
        return NOW


def item(memory_id, text, *, key=None, owner="owner-a", scope=MemoryScope.USER, scope_id="owner-a"):
    return MemoryItem(memory_id=memory_id, owner_id=owner, scope=scope, scope_id=scope_id,
                      memory_key=key or f"writing.{memory_id}", memory_type=MemoryType.PREFERENCE,
                      display_text=text, content={}, status=MemoryStatus.CONFIRMED,
                      provenance=[MemoryProvenance(source_type="explicit_user", actor_type="user")],
                      created_at=NOW, updated_at=NOW)


class Reader:
    def __init__(self, items):
        self.items = items

    def list_confirmed(self, *, owner_id, scope=None, scope_id=None):
        return [i for i in self.items if i.owner_id == owner_id and i.scope == scope and (scope_id is None or i.scope_id == scope_id)]


TARGET = item("target", "求职信的语气保持正式", key="cover_letter.tone")   # meaning-only match for the English query


def distractors(n):
    # Each shares words with the query, so all outrank the target lexically.
    return [item(f"d{i:04d}", f"Cover letter tone note {i} about formatting margins and fonts") for i in range(n)]


def ranker_seen():
    seen = []
    def rank(text, batch):
        seen.append([m.memory_id for m in batch])
        return [m.memory_id for m in batch if m.memory_id == "target"]
    return rank, seen


@pytest.mark.parametrize("count", [40, 100])
def test_meaning_only_match_is_ranked_behind_many_lexical_distractors(count):
    rank, seen = ranker_seen()
    retriever = MemoryRetriever(Reader([*distractors(count), TARGET]), clock=Clock(), semantic_ranker=rank)
    result = retriever.retrieve(MemoryQuery(owner_id="owner-a", text="cover letter tone", limit=100, token_budget=100_000))
    assert "target" in {r.memory.memory_id for r in result.items}
    assert sum(len(b) for b in seen) == count + 1 and not result.semantic_unranked_ids


def test_beyond_the_batch_limit_candidates_are_reported_not_silently_dropped():
    rank, seen = ranker_seen()
    retriever = MemoryRetriever(Reader([*distractors(1000), TARGET]), clock=Clock(), semantic_ranker=rank)
    result = retriever.retrieve(MemoryQuery(owner_id="owner-a", text="cover letter tone", limit=10, token_budget=100_000))
    assert len(seen) == SEMANTIC_MAX_BATCHES
    ranked = {m for b in seen for m in b}
    assert ranked | set(result.semantic_unranked_ids) == {d.memory_id for d in distractors(1000)} | {"target"}
    assert "target" in ranked or "target" in result.semantic_unranked_ids


def test_a_failing_batch_does_not_discard_the_others():
    calls = []
    def rank(text, batch):
        calls.append(len(batch))
        if len(calls) == 1:
            raise RuntimeError("provider unavailable")
        return [m.memory_id for m in batch if m.memory_id == "target"]
    items = [*distractors(150), TARGET]
    retriever = MemoryRetriever(Reader(items), clock=Clock(), semantic_ranker=rank)
    result = retriever.retrieve(MemoryQuery(owner_id="owner-a", text="cover letter tone", limit=100, token_budget=100_000))
    assert len(calls) >= 2 and result.semantic_failed
    assert "target" in {r.memory.memory_id for r in result.items}   # the target sits in a later batch


def test_out_of_scope_and_other_owner_records_never_reach_the_ranker():
    rank, seen = ranker_seen()
    other_owner = item("stranger", "求职信的语气保持正式", owner="owner-b", scope_id="owner-b")
    other_session = item("session-x", "求职信的语气保持正式", scope=MemoryScope.SESSION, scope_id="other-session")
    retriever = MemoryRetriever(Reader([TARGET, other_owner, other_session]), clock=Clock(), semantic_ranker=rank)
    result = retriever.retrieve(MemoryQuery(owner_id="owner-a", text="cover letter tone", session_id="mine"))
    assert {m for b in seen for m in b} == {"target"}
    assert [r.memory.memory_id for r in result.items] == ["target"]
    assert result.strategy_version.startswith("stage3a")
