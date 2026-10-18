"""Memory upgrade stage 1: deterministic end-to-end checks through the real runtime wiring.

The runtime is created by create_session_runtime with a placeholder model (no provider calls,
no semantic ranker). Chat context comes from the runtime's own context projector, material
preferences from its ApplicationPackWorkflow, sub-task memory from its AgentContextPolicy.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from eregion.api.main import create_app
from eregion.api.session_dependencies import create_session_runtime
from eregion.runtime.memory import MemoryProvenance, MemoryScope, MemoryType
from eregion.runtime.memory.access import MemoryPurpose, application_scope_id
from eregion.runtime.memory.errors import StaleMemoryError
from eregion.runtime.memory.service import MemoryService
from eregion.runtime.memory.types import MemorySensitivity, MemoryStatus
from eregion.runtime.sessions.events import SessionEvent, SessionEventType
from eregion.runtime.sessions.state import SessionMessageDraft, SessionState
from eregion.runtime.tools.messages import AgentMessage


@pytest.fixture
def stack(tmp_path):
    runtime = create_session_runtime(migrate_schema=True, database_url=f"sqlite:///{(tmp_path / 'memory-e2e.sqlite').as_posix()}", model=object())
    app = create_app(run_service=object(), session_runtime=runtime)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, runtime
    runtime.close()


def owner(runtime):
    return runtime.owner_resolver.resolve().owner_id


def confirmed(runtime, key, text, *, scope=MemoryScope.USER, scope_id=None, sensitivity=MemorySensitivity.NORMAL,
              owner_id=None, memory_type=MemoryType.PREFERENCE, expires_at=None):
    owner_id = owner_id or owner(runtime)
    item = runtime.memories.create_candidate(
        owner_id=owner_id, scope=scope, scope_id=scope_id or owner_id, memory_key=key, memory_type=memory_type,
        display_text=text, content={"value": text}, provenance=[MemoryProvenance(source_type="explicit_user", actor_type="user")],
        sensitivity=sensitivity, expires_at=expires_at)
    return runtime.memories.confirm(item.memory_id, owner_id=owner_id, expected_version=0, confirmed_by_user=True)


def new_session(runtime, session_id, user_text, *, user_id="local-user"):
    message = AgentMessage(message_id=f"user-{session_id}", role="user", content=user_text)
    runtime.sessions.create(SessionState(session_id=session_id, user_id=user_id),
                            SessionEvent(session_id=session_id, event_type=SessionEventType.SESSION_CREATED),
                            messages=[SessionMessageDraft(message=message)])
    return runtime.sessions.require(session_id)


def chat_memory(runtime, session_id, text, **kwargs):
    state = new_session(runtime, session_id, text, **kwargs)
    prepared = runtime.coordinator._context_projector.prepare(state)
    return {(ref.memory_id, ref.version) for ref in prepared.snapshot.memory_versions}


def material_memory(runtime, application_id=None):
    return {(p["memory_id"], p["version"]) for p in runtime.pack_workflow._preferences(application_id)}


def test_1_conversation_personal_candidate_confirmed_then_used_in_a_new_session(stack, tmp_path):
    client, runtime = stack
    # Conversation learning stores what the user said as a PERSONAL candidate (learning/service.py).
    from eregion.runtime.learning.repository import ConversationLearningRepository
    from eregion.runtime.learning.service import ConversationLearningService
    from eregion.runtime.learning.types import ExperienceSignal, ExperienceSignalType, LearningObservation
    quote = "I prefer resume summaries with no more than two sentences."
    observation = LearningObservation(signals=[ExperienceSignal(
        signal_type=ExperienceSignalType.PREFERENCE, canonical_key="resume.summary.max_sentences",
        summary="Prefers resume summaries with no more than two sentences.", evidence_quote=quote,
        normalized_value=2, confidence=0.96)])
    class Observer:
        def analyze(self, *, user_message, assistant_message):
            return observation
    service = ConversationLearningService(sessions=runtime.sessions,
        repository=ConversationLearningRepository(runtime.database.session_factory), analyzer=Observer(),
        memories=runtime.memories, feedback=runtime.feedback, owner_id=owner(runtime))
    user = AgentMessage(message_id="u-1", role="user", content=quote)
    assistant = AgentMessage(message_id="a-1", role="assistant", content="Understood.")
    runtime.sessions.create(SessionState(session_id="learn-1", user_id="local-user"),
                            SessionEvent(session_id="learn-1", event_type=SessionEventType.SESSION_CREATED),
                            messages=[SessionMessageDraft(message=user), SessionMessageDraft(message=assistant)])
    service.observe_completed_turn(session_id="learn-1", assistant_message_id="a-1")
    candidate = runtime.memories.list_candidates(owner_id=owner(runtime))[0]
    assert candidate.sensitivity == MemorySensitivity.PERSONAL
    assert chat_memory(runtime, "s-before", "Shorten my resume summary") == set()          # unconfirmed
    response = client.post(f"/memories/{candidate.memory_id}/confirm", json={"expected_version": candidate.version})
    assert response.status_code == 200
    item = runtime.memories.require(candidate.memory_id, owner_id=owner(runtime))
    assert chat_memory(runtime, "s-after", "Shorten my resume summary") == {(item.memory_id, item.version)}


def test_2_api_preference_is_the_same_version_in_chat_and_application_materials(stack):
    client, runtime = stack
    created = client.post("/memories", json={"scope": "user", "memory_key": "resume.summary.style",
        "memory_type": "preference", "display_text": "Resume summary uses plain active verbs",
        "content": {"style": "active verbs"}}).json()
    assert runtime.memories.require(created["memory_id"], owner_id=owner(runtime)).scope_id == owner(runtime)
    client.post(f"/memories/{created['memory_id']}/confirm", json={"expected_version": created["version"]})
    item = runtime.memories.require(created["memory_id"], owner_id=owner(runtime))
    ref = (item.memory_id, item.version)
    assert ref in chat_memory(runtime, "s-2", "Rewrite my resume summary")
    assert ref in material_memory(runtime)
    # A record written by earlier versions with the profile ID ("default") is the same owner.
    legacy = confirmed(runtime, "cover_letter.tone", "Cover letter tone stays formal", scope_id="default")
    assert (legacy.memory_id, legacy.version) in material_memory(runtime)


def test_3_project_and_application_memory_apply_only_in_their_scope(stack):
    _, runtime = stack
    project = confirmed(runtime, "writing.project_note", "Writing for this project stays concise",
                        scope=MemoryScope.PROJECT, scope_id=runtime.project_id)
    other_project = confirmed(runtime, "writing.other_note", "Writing for another project",
                              scope=MemoryScope.PROJECT, scope_id="another-project")
    app_a = confirmed(runtime, "resume.length", "Resume for application A is one page",
                      scope=MemoryScope.PROJECT, scope_id=application_scope_id("app-a"))
    chat = chat_memory(runtime, "s-3", "Writing concise project notes")
    assert (project.memory_id, project.version) in chat and (other_project.memory_id, other_project.version) not in chat
    assert (app_a.memory_id, app_a.version) in material_memory(runtime, "app-a")
    assert (app_a.memory_id, app_a.version) not in material_memory(runtime, "app-b")
    assert (app_a.memory_id, app_a.version) not in material_memory(runtime)
    # A legacy application scope is never read as an ordinary project.
    access = runtime.memory_access.context(MemoryPurpose.APPLICATION_MATERIALS)
    assert not access.in_scope(app_a) and access.in_scope(project)


def test_4_same_key_application_exception_does_not_leak_to_other_applications(stack):
    _, runtime = stack
    general = confirmed(runtime, "resume.tone", "Resume tone is formal")
    exception = confirmed(runtime, "resume.tone", "Resume tone for Stripe is conversational",
                          scope=MemoryScope.PROJECT, scope_id=application_scope_id("stripe"))
    service = MemoryService(runtime.memories)
    for application, expected in [("stripe", exception), ("acme", general), (None, general)]:
        context = runtime.memory_access.context(MemoryPurpose.APPLICATION_MATERIALS, application_id=application)
        result = service.effective_preferences(context, "application_materials")
        assert [e.item.memory_id for e in result.items if e.item.memory_key == "resume.tone"] == [expected.memory_id]


def test_5_supersede_then_delete_old_memory_keeps_the_history_link(stack):
    _, runtime = stack
    old = confirmed(runtime, "resume.summary.max_sentences", "Resume summary at most three sentences")
    replacement = runtime.memories.create_candidate(owner_id=owner(runtime), scope=MemoryScope.USER, scope_id=owner(runtime),
        memory_key="resume.summary.max_sentences", memory_type=MemoryType.PREFERENCE,
        display_text="Resume summary at most two sentences", content={"value": 2},
        provenance=[MemoryProvenance(source_type="explicit_user", actor_type="user")])
    superseded = runtime.memories.supersede(old.memory_id, replacement_memory_id=replacement.memory_id, owner_id=owner(runtime),
                                            expected_version=old.version, replacement_expected_version=replacement.version)
    deleted = runtime.memories.soft_delete(old.memory_id, owner_id=owner(runtime), expected_version=superseded.version)
    assert deleted.status == MemoryStatus.DELETED and deleted.superseded_by_memory_id == replacement.memory_id
    current = runtime.memories.require(replacement.memory_id, owner_id=owner(runtime))
    assert current.supersedes_memory_id == old.memory_id
    assert [e.event_type.value for e in runtime.memories.events(old.memory_id, owner_id=owner(runtime))][-2:] == ["superseded", "deleted"]
    assert material_memory(runtime) == {(current.memory_id, current.version)}


def test_6_deleted_rejected_expired_sensitive_and_other_owner_memory_never_reach_model_context(stack):
    _, runtime = stack
    text = "Resume summary keyword alpha"
    valid = confirmed(runtime, "resume.valid", text)
    deleted = confirmed(runtime, "resume.deleted", text)
    runtime.memories.soft_delete(deleted.memory_id, owner_id=owner(runtime), expected_version=deleted.version)
    rejected = runtime.memories.create_candidate(owner_id=owner(runtime), scope=MemoryScope.USER, scope_id=owner(runtime),
        memory_key="resume.rejected", memory_type=MemoryType.PREFERENCE, display_text=text, content={},
        provenance=[MemoryProvenance(source_type="explicit_user", actor_type="user")])
    runtime.memories.reject(rejected.memory_id, owner_id=owner(runtime), expected_version=rejected.version)
    expiring = confirmed(runtime, "resume.expired", text, expires_at=datetime.now(UTC) + timedelta(seconds=1))
    sensitive = confirmed(runtime, "resume.sensitive", text, sensitivity=MemorySensitivity.SENSITIVE)
    stranger = confirmed(runtime, "resume.stranger", text, owner_id="someone-else")
    import time; time.sleep(1.2)
    seen = {m for m, _ in chat_memory(runtime, "s-6", "Resume summary keyword alpha")}
    seen_materials = {m for m, _ in material_memory(runtime)}
    for leaked in (deleted, rejected, expiring, sensitive, stranger):
        assert leaked.memory_id not in seen and leaked.memory_id not in seen_materials
    assert valid.memory_id in seen and valid.memory_id in seen_materials
    # A session owned by someone else gets no memory at all.
    assert chat_memory(runtime, "s-6-other", "Resume summary keyword alpha", user_id="someone-else") == set()


def test_7_concurrent_changes_with_a_stale_version_are_rejected(stack):
    _, runtime = stack
    item = runtime.memories.create_candidate(owner_id=owner(runtime), scope=MemoryScope.USER, scope_id=owner(runtime),
        memory_key="resume.concurrent", memory_type=MemoryType.PREFERENCE, display_text="Concurrent edit target",
        content={}, provenance=[MemoryProvenance(source_type="explicit_user", actor_type="user")])
    runtime.memories.confirm(item.memory_id, owner_id=owner(runtime), expected_version=item.version, confirmed_by_user=True)
    with pytest.raises(StaleMemoryError):
        runtime.memories.soft_delete(item.memory_id, owner_id=owner(runtime), expected_version=item.version)


def test_legacy_and_owner_scoped_duplicates_cannot_both_be_confirmed(stack):
    _, runtime = stack
    confirmed(runtime, "resume.font", "Resume uses a serif font", scope_id="default")
    duplicate = runtime.memories.create_candidate(owner_id=owner(runtime), scope=MemoryScope.USER, scope_id=owner(runtime),
        memory_key="resume.font", memory_type=MemoryType.PREFERENCE, display_text="Resume uses a sans font", content={},
        provenance=[MemoryProvenance(source_type="explicit_user", actor_type="user")])
    from eregion.runtime.memory.errors import MemoryAlreadyExistsError
    with pytest.raises(MemoryAlreadyExistsError):
        runtime.memories.confirm(duplicate.memory_id, owner_id=owner(runtime), expected_version=0, confirmed_by_user=True)


def test_existing_conflicts_are_reported_not_silently_merged(stack, tmp_path):
    _, runtime = stack
    # Two confirmed records that only differ by legacy/owner scope ID (possible in databases
    # written before stage 1; the repository now refuses to create this).
    from eregion.runtime.memory.repository import MemoryRepository
    plain = MemoryRepository(runtime.database.session_factory)
    for scope_id, text in [("default", "Writing tone warm"), (owner(runtime), "Writing tone neutral")]:
        item = plain.create_candidate(owner_id=owner(runtime), scope=MemoryScope.USER, scope_id=scope_id,
            memory_key="writing.tone", memory_type=MemoryType.PREFERENCE, display_text=text, content={},
            provenance=[MemoryProvenance(source_type="explicit_user", actor_type="user")])
        plain.confirm(item.memory_id, owner_id=owner(runtime), expected_version=0, confirmed_by_user=True)
    context = runtime.memory_access.context(MemoryPurpose.APPLICATION_MATERIALS)
    result = MemoryService(runtime.memories).effective_preferences(context, "application_materials")
    assert [c["memory_key"] for c in result.conflicts] == ["writing.tone"]
    assert len([e for e in result.items if e.item.memory_key == "writing.tone"]) == 1


def test_migration_preview_is_read_only_apply_is_repeatable_and_conflicts_stay(stack):
    _, runtime = stack
    from eregion.runtime.memory.migration import apply, preview
    from eregion.runtime.memory.repository import MemoryRepository
    plain = MemoryRepository(runtime.database.session_factory)
    def add(scope_id, key, text, *, confirm=True):
        item = plain.create_candidate(owner_id=owner(runtime), scope=MemoryScope.USER, scope_id=scope_id, memory_key=key,
            memory_type=MemoryType.PREFERENCE, display_text=text, content={},
            provenance=[MemoryProvenance(source_type="explicit_user", actor_type="user")])
        return plain.confirm(item.memory_id, owner_id=owner(runtime), expected_version=0, confirmed_by_user=True) if confirm else item
    legacy = add("default", "resume.length", "Resume one page")
    pending = add("default", "resume.font", "Resume font serif", confirm=False)
    clash_old = add("default", "writing.tone", "Writing tone warm")
    clash_new = add(owner(runtime), "writing.tone", "Writing tone neutral")
    before = {i.memory_id: (i.scope_id, i.version) for i in plain.list_items(owner_id=owner(runtime))}
    plan = preview(runtime.database.session_factory, owner_id=owner(runtime))
    assert {i.memory_id: (i.scope_id, i.version) for i in plain.list_items(owner_id=owner(runtime))} == before
    assert {c.memory_id for c in plan.changes} == {legacy.memory_id, pending.memory_id}
    assert [c["memory_key"] for c in plan.conflicts] == ["writing.tone"]
    assert apply(runtime.database.session_factory, plan) == 2
    moved = plain.require(legacy.memory_id, owner_id=owner(runtime))
    assert moved.scope_id == owner(runtime) and moved.status == MemoryStatus.CONFIRMED
    event = plain.events(legacy.memory_id, owner_id=owner(runtime))[-1]
    assert event.event_type.value == "scope_migrated" and event.payload["from_scope_id"] == "default"
    for untouched in (clash_old, clash_new):
        assert plain.require(untouched.memory_id, owner_id=owner(runtime)).scope_id == untouched.scope_id
    again = preview(runtime.database.session_factory, owner_id=owner(runtime))
    assert again.changes == [] and len(again.conflicts) == 1
    with pytest.raises(RuntimeError, match="changed after the preview"):
        apply(runtime.database.session_factory, plan)          # stale plan
    assert (moved.memory_id, moved.version) in material_memory(runtime)


def test_future_supersede_api_returns_conflict_without_changing_memory(stack):
    client, runtime = stack
    old = confirmed(runtime, "job.location_mode", "remote")
    new = runtime.memories.create_candidate(owner_id=owner(runtime), scope=MemoryScope.USER,
        scope_id=owner(runtime), memory_key="job.location_mode", memory_type=MemoryType.PREFERENCE,
        display_text="onsite", content={"value": "onsite"},
        valid_from=datetime.now(UTC)+timedelta(days=30),
        provenance=[MemoryProvenance(source_type="explicit_user", actor_type="user")])
    response = client.post(f"/memories/{old.memory_id}/supersede", json={
        "expected_version": old.version, "replacement_memory_id": new.memory_id,
        "replacement_expected_version": new.version})
    assert response.status_code == 409, response.text
    assert "not currently valid" in response.text
    assert runtime.memories.require(old.memory_id, owner_id=owner(runtime)) == old
    assert runtime.memories.require(new.memory_id, owner_id=owner(runtime)) == new
