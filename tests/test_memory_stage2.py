"""Memory upgrade stage 2: conversation statements become scoped, sourced, updatable records.

Extractor outputs are fixed (scripted observations); this verifies the write and read flow,
not a model's extraction accuracy (evaluated separately with labelled cases).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from eregion.api.main import create_app
from eregion.api.session_dependencies import create_session_runtime
from eregion.runtime.learning.repository import ConversationLearningRepository
from eregion.runtime.learning.service import ConversationLearningService
from eregion.runtime.learning.types import ExperienceSignal, ExperienceSignalType, LearningObservation
from eregion.runtime.memory import MemoryProvenance, MemoryScope, MemoryType
from eregion.runtime.memory.access import MemoryPurpose
from eregion.runtime.memory.service import MemoryService
from eregion.runtime.memory.types import MemoryStatus
from eregion.runtime.sessions.events import SessionEvent, SessionEventType
from eregion.runtime.sessions.state import SessionMessageDraft, SessionState
from eregion.runtime.tools.messages import AgentMessage


class Clock:
    def __init__(self, now):
        self.value = now

    def now(self):
        return self.value


@pytest.fixture
def stack(tmp_path):
    runtime = create_session_runtime(database_url=f"sqlite:///{(tmp_path / 'stage2.sqlite').as_posix()}", model=object())
    app = create_app(run_service=object(), session_runtime=runtime)
    with TestClient(app, raise_server_exceptions=False):
        yield runtime
    runtime.close()


def owner(runtime):
    return runtime.owner_resolver.resolve().owner_id


class Turns:
    """Runs one user turn through the real learning service with a fixed observation."""

    def __init__(self, runtime, applications=None):
        self.runtime, self.count, self.queue = runtime, 0, []
        observer = self
        self.service = ConversationLearningService(sessions=runtime.sessions,
            repository=ConversationLearningRepository(runtime.database.session_factory), analyzer=observer,
            memories=runtime.memories, feedback=runtime.feedback, owner_id=owner(runtime),
            application_for_session=(applications or {}).get)

    def analyze(self, *, user_message, assistant_message, message_date=None):
        self.dates = getattr(self, "dates", []) + [message_date]
        return self.queue.pop(0)

    def say(self, text, *signals, session_id=None):
        self.count += 1
        session_id = session_id or f"session-{self.count}"
        user = AgentMessage(message_id=f"u-{self.count}", role="user", content=text)
        assistant = AgentMessage(message_id=f"a-{self.count}", role="assistant", content="Noted.")
        self.runtime.sessions.create(SessionState(session_id=session_id, user_id=owner(self.runtime)),
            SessionEvent(session_id=session_id, event_type=SessionEventType.SESSION_CREATED),
            messages=[SessionMessageDraft(message=user), SessionMessageDraft(message=assistant)])
        self.queue.append(LearningObservation(signals=list(signals)))
        return self.service.observe_completed_turn(session_id=session_id, assistant_message_id=assistant.message_id)


def signal(key, value, quote, **extra):
    return ExperienceSignal(signal_type=extra.pop("signal_type", ExperienceSignalType.PREFERENCE), canonical_key=key,
                            summary=extra.pop("summary", quote), evidence_quote=quote, normalized_value=value,
                            confidence=0.95, **extra)


def live(runtime, key):
    return [i for i in runtime.memories.list_items(owner_id=owner(runtime), statuses={MemoryStatus.CANDIDATE, MemoryStatus.CONFIRMED})
            if i.memory_key == key]


def confirm(runtime, item):
    return runtime.memories.confirm(item.memory_id, owner_id=owner(runtime), expected_version=item.version, confirmed_by_user=True)


def effective(runtime, application_id=None, task="job_search", clock=None):
    context = runtime.memory_access.context(MemoryPurpose.APPLICATION_MATERIALS, application_id=application_id)
    return {e.item.memory_key: e.item for e in MemoryService(runtime.memories, clock=clock).effective_preferences(context, task).items}


def test_remote_only_with_a_stripe_hybrid_exception(stack):
    runtime = stack
    turns = Turns(runtime, applications={"stripe-chat": "app-stripe"})
    turns.say("只找远程的工作。", signal("job.location_mode", "远程", "只找远程的工作"))
    general = confirm(runtime, live(runtime, "job.location_mode")[0])
    turns.say("这次 Stripe 可以 hybrid。", signal("job.location_mode", "hybrid", "这次 Stripe 可以 hybrid", scope_hint="application"),
              session_id="stripe-chat")
    exception, = [i for i in live(runtime, "job.location_mode") if i.scope == MemoryScope.APPLICATION]
    assert general.content["value"] == "remote" and exception.scope_id == "app-stripe"
    assert exception.content["narrows_memory_ids"] == [general.memory_id]       # a local exception, not a replacement
    assert runtime.memories.require(general.memory_id, owner_id=owner(runtime)).status == MemoryStatus.CONFIRMED
    confirm(runtime, exception)
    assert effective(runtime, "app-stripe")["job.location_mode"].content["value"] == "hybrid"
    assert effective(runtime, "app-acme")["job.location_mode"].content["value"] == "remote"
    assert effective(runtime)["job.location_mode"].content["value"] == "remote"


def test_a_future_relocation_does_not_change_current_conditions(stack):
    runtime = stack
    turns = Turns(runtime)
    turns.say("下个月开始可以搬到纽约。", signal("job.relocation", "New York", "下个月开始可以搬到纽约", valid_from="2026-11-01"))
    item, = live(runtime, "job.relocation")
    assert item.valid_from == datetime(2026, 11, 1, tzinfo=UTC) and item.valid_to is None
    assert turns.dates[0]                                 # the extractor received the message date
    confirm(runtime, item)
    assert "job.relocation" not in effective(runtime, clock=Clock(datetime(2026, 10, 17, tzinfo=UTC)))
    assert effective(runtime, clock=Clock(datetime(2026, 11, 2, tzinfo=UTC)))["job.relocation"].content["value"] == "New York"


def test_saying_the_same_preference_twice_keeps_one_record_with_both_sources(stack):
    runtime = stack
    turns = Turns(runtime)
    first = turns.say("Remote only, please.", signal("job.location_mode", "remote", "Remote only"))
    second = turns.say("Again: remote only.", signal("Job Location Mode", "Remote only", "remote only"))
    item, = live(runtime, "job.location_mode")
    assert {p.source_id for p in item.provenance} == {first.experience_id, second.experience_id}


def test_a_new_value_waits_for_confirmation_and_then_replaces_the_old(stack):
    runtime = stack
    turns = Turns(runtime)
    turns.say("I only want remote jobs.", signal("job.location_mode", "remote", "only want remote jobs"))
    old = confirm(runtime, live(runtime, "job.location_mode")[0])
    turns.say("Actually I prefer onsite now.", signal("job.location_mode", "onsite", "I prefer onsite now"))
    new, = [i for i in live(runtime, "job.location_mode") if i.status == MemoryStatus.CANDIDATE]
    assert new.content["replaces_memory_id"] == old.memory_id
    assert effective(runtime)["job.location_mode"].content["value"] == "remote"        # old still in use
    runtime.memories.supersede(old.memory_id, replacement_memory_id=new.memory_id, owner_id=owner(runtime),
                               expected_version=old.version, replacement_expected_version=new.version)
    assert effective(runtime)["job.location_mode"].content["value"] == "onsite"


def test_quoted_job_description_requirements_are_not_learned_as_the_users_facts(stack):
    runtime = stack
    turns = Turns(runtime)
    text = "The JD says it requires five years of experience. I have three years of Python."
    turns.say(text,
              signal("experience.years", 5, "requires five years of experience", signal_type=ExperienceSignalType.CAREER_FACT,
                     attribution="quoted_other"),
              signal("experience.python", 3, "I have three years of Python", signal_type=ExperienceSignalType.CAREER_FACT))
    claims = [e.current.claim_text for e in runtime.evidence.list(status="candidate", limit=50)]
    assert claims == ["I have three years of Python"]
    assert live(runtime, "experience.years") == [] and live(runtime, "experience.python") == []   # facts are not memory


def test_one_message_with_several_preferences_keeps_a_source_for_each(stack):
    runtime = stack
    turns = Turns(runtime)
    text = "Keep my resume summary to two sentences, and make cover letters formal."
    experience = turns.say(text, signal("resume.summary.max_sentences", "two", "resume summary to two sentences"),
                           signal("cover_letter.tone", "formal", "make cover letters formal"))
    summary, = live(runtime, "resume.summary.max_sentences")
    tone, = live(runtime, "cover_letter.tone")
    assert summary.content["value"] == 2 and tone.content["value"] == "formal"
    assert summary.content["source_quote"] == "resume summary to two sentences"
    assert tone.content["source_quote"] == "make cover letters formal"
    assert summary.provenance[0].source_id == tone.provenance[0].source_id == experience.experience_id


def test_chinese_and_english_forms_of_one_preference_are_one_concept(stack):
    runtime = stack
    turns = Turns(runtime)
    turns.say("只要远程。", signal("job.work_mode", "远程", "只要远程"))
    turns.say("Remote only.", signal("Job Location Mode", "remote only", "Remote only"))
    item, = live(runtime, "job.location_mode")
    assert item.content["value"] == "remote" and len(item.provenance) == 2


def test_temporary_and_unlinked_application_statements_stay_in_the_conversation(stack):
    runtime = stack
    turns = Turns(runtime)
    turns.say("Today, use a formal tone.", signal("writing.style", "formal", "Today, use a formal tone", durability="temporary"),
              session_id="chat-temp")
    turns.say("For this one, keep it short.", signal("writing.length", "short", "keep it short", scope_hint="application"),
              session_id="chat-no-app")
    style, = live(runtime, "writing.style")
    length, = live(runtime, "writing.length")
    assert (style.scope, style.scope_id) == (MemoryScope.SESSION, "chat-temp")
    assert (length.scope, length.scope_id) == (MemoryScope.SESSION, "chat-no-app")


def test_unknown_or_contradictory_dates_stay_unknown(stack):
    runtime = stack
    turns = Turns(runtime)
    turns.say("Starting soon I can travel.", signal("job.travel", "yes", "Starting soon I can travel", valid_from="soon"))
    turns.say("Only between these dates.", signal("job.window", "x", "Only between these dates",
                                                  valid_from="2026-12-01", valid_to="2026-11-01"))
    assert live(runtime, "job.travel")[0].valid_from is None
    window, = live(runtime, "job.window")
    assert window.valid_from is None and window.valid_to is None


def test_migration_moves_legacy_application_exceptions_to_the_application_scope(stack):
    runtime = stack
    from eregion.runtime.memory.migration import apply, preview
    item = runtime.memories.create_candidate(owner_id=owner(runtime), scope=MemoryScope.PROJECT, scope_id="application:app-9",
        memory_key="resume.length", memory_type=MemoryType.PREFERENCE, display_text="One page for app 9", content={"value": 1},
        provenance=[MemoryProvenance(source_type="explicit_user", actor_type="user")])
    item = confirm(runtime, item)
    plan = preview(runtime.database.session_factory, owner_id=owner(runtime))
    change, = [c for c in plan.changes if c.memory_id == item.memory_id]
    assert (change.to_scope, change.to_scope_id) == ("application", "app-9")
    apply(runtime.database.session_factory, plan)
    moved = runtime.memories.require(item.memory_id, owner_id=owner(runtime))
    assert (moved.scope, moved.scope_id) == (MemoryScope.APPLICATION, "app-9")
    assert effective(runtime, "app-9", task="application_materials")["resume.length"].memory_id == item.memory_id
    assert "resume.length" not in effective(runtime, "app-8", task="application_materials")
