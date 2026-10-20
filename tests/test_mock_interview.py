"""Mock interview state-machine tests use scripted models, never paid calls."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from eregion.runtime.context.repository import ContextSnapshotRepository
from eregion.runtime.mock_interview.controller import MockInterviewController
from eregion.runtime.mock_interview.worker import MockInterviewerWorker
from eregion.runtime.mock_interview.errors import MockInterviewConflict
from eregion.runtime.mock_interview.policy import build_plan, should_follow_up, validate_evaluation, validate_question
from eregion.runtime.mock_interview.repository import MockInterviewRepository
from eregion.runtime.mock_interview.types import InterviewMode, MockAnswerEvaluation, MockInterviewQuestion, MockStatus
from eregion.runtime.sessions.repository import SessionRepository
from eregion.runtime.application_pack.types import PackStatus
from eregion.runtime.workspace.repository import JobWorkspaceRepository
from eregion.runtime.workspace.types import ArtifactType
from eregion.api.db import create_database, upgrade_database


class ScriptedModel:
    def __init__(self):
        self.questions = 0
        self.evaluations = 0

    def question(self, context):
        self.questions += 1
        item = context["plan_item"]
        return MockInterviewQuestion(question_id=str(uuid4()),
            plan_item_id=item["plan_item_id"], question_text="What did you personally do?",
            question_type=item["question_type"], competency=item["competency"],
            related_requirement_id=item["related_requirement_id"],
            related_evidence_ids=item["related_evidence_ids"],
            reason_for_asking="Understand a specific contribution.",
            expected_answer_elements=["personal action", "result"])

    def evaluate(self, context):
        self.evaluations += 1
        return MockAnswerEvaluation(evaluation_id=str(uuid4()),
            answer_id=context["previous_turn_untrusted"]["answer_id"],
            overall_score=3, relevance_score=3, specificity_score=3,
            evidence_grounding_score=3, structure_score=3,
            communication_score=3, strengths=["You described your work."],
            improvement_areas=["Explain the outcome."],
            exact_answer_quotes=[context["answer_untrusted"]],
            missing_answer_elements=["result"] if self.evaluations == 1 else [],
            followup_needed=self.evaluations == 1,
            followup_reason="The personal result is unclear." if self.evaluations == 1 else None)


@pytest.fixture
def setup(tmp_path):
    url = f"sqlite:///{(tmp_path / 'mock.db').as_posix()}"
    upgrade_database(url)
    db = create_database(url)
    workspace = JobWorkspaceRepository(db.session_factory)
    app = workspace.save_workspace(cleaned_job_description="Python API engineer needed.",
        title="Engineer", company="Example").application
    model = ScriptedModel()
    snapshot = workspace.current_snapshot(app.application_id)
    pack = SimpleNamespace(pack_id=str(uuid4()), snapshot_id=snapshot.snapshot_id,
                           status=PackStatus.APPROVED, version=1)
    packs = SimpleNamespace(
        list_for_application=lambda application_id: [pack],
        snapshot=lambda pack_id: SimpleNamespace(items=[]),
        items=lambda pack_id: [])
    job = SimpleNamespace(artifact_type=ArtifactType.JOB_ANALYSIS,
        artifact_id=str(uuid4()),
        version=1,
        content={"title":"Engineer", "summary":"", "requirements":[],
                 "responsibilities":[]})
    workspace.list_artifacts = lambda application_id: [job]
    evidence = SimpleNamespace(get=lambda evidence_id: None,
                               create_candidate=lambda **kwargs: None)
    controller = MockInterviewController(
        interviews=MockInterviewRepository(db.session_factory),
        sessions=SessionRepository(db.session_factory), workspace=workspace,
        packs=packs, evidence=evidence,
        context_snapshots=ContextSnapshotRepository(db.session_factory),
        model=model)
    yield controller, model, app.application_id, db
    db.close()


def test_deterministic_bounded_plan_and_followup_policy():
    args = dict(interview_id="interview-1", snapshot_id="snapshot-1",
        snapshot_hash="abc", pack_id="pack-1", pack_version=1,
        evidence=[], requirement_ids=["req-1"], mode=InterviewMode.MIXED,
        target_count=5)
    assert build_plan(**args).items == build_plan(**args).items
    assert [item.competency for item in build_plan(**args).items][:3] == [
        "role_fit", "collaboration", "requirement_fit"]
    with pytest.raises(Exception):
        build_plan(**{**args, "target_count": 13})
    for mode in InterviewMode:
        selected = build_plan(**{**args, "mode": mode})
        assert len(selected.items) == 5
        assert len({item.plan_item_id for item in selected.items}) == 5
    assert build_plan(**{**args, "mode": InterviewMode.PROJECT_DEEP_DIVE}).items[0].competency == "personal_contribution"


def test_mock_interview_starts_without_an_approved_pack(setup):
    controller, _, application_id, _ = setup
    controller.packs.list_for_application = lambda _application_id: []
    started = controller.start(application_id, idempotency_key="direct-start",
        target_question_count=3)
    assert started.status == MockStatus.AWAITING_ANSWER
    plan = controller.interviews.plan(started.mock_interview_id)
    assert plan.source_mode == "job_analysis"
    assert plan.pack_id is None
    assert plan.pack_version is None


def test_answer_quote_and_numeric_grounding():
    question = MockInterviewQuestion(question_id="q", plan_item_id="p",
        question_text="What happened?", question_type="role_fit",
        competency="role_fit", reason_for_asking="Practice.")
    base = dict(evaluation_id="e", answer_id="a", overall_score=3,
        relevance_score=3, specificity_score=3, evidence_grounding_score=3,
        structure_score=3, communication_score=3)
    valid = MockAnswerEvaluation(**base, exact_answer_quotes=["Built Python APIs."])
    validate_evaluation(valid, "Built Python APIs.", question, "")
    with pytest.raises(Exception):
        validate_evaluation(MockAnswerEvaluation(**base,
            exact_answer_quotes=["Led 10 engineers."]), "Built Python APIs.", question, "")
    with pytest.raises(Exception):
        validate_evaluation(MockAnswerEvaluation(**base,
            strengths=["You improved revenue by 30%."]), "Built Python APIs.", question, "")
    assert not should_follow_up(valid, followups_used=2, maximum=2)
    item = build_plan(interview_id="i", snapshot_id="s", snapshot_hash="h",
        pack_id="p", pack_version=1, evidence=[], requirement_ids=[],
        mode=InterviewMode.MIXED, target_count=3).items[0]
    invented = MockInterviewQuestion(question_id="q2", plan_item_id=item.plan_item_id,
        question_text="How did you improve revenue by 25%?", question_type=item.question_type,
        competency=item.competency, reason_for_asking="Practice.")
    with pytest.raises(Exception):
        validate_question(invented, item, set(), "No revenue metric in source.")


def test_persisted_answer_followup_duplicate_and_report(setup):
    controller, model, application_id, db = setup
    started = controller.start(application_id, idempotency_key="start-1",
        target_question_count=3, max_followups_per_question=1)
    assert started.status == MockStatus.AWAITING_ANSWER
    first = controller.view(started.mock_interview_id)["question"]
    answered = controller.answer(started.mock_interview_id, "Built Python APIs.",
        expected_version=started.version, idempotency_key="answer-1")
    assert answered.status == MockStatus.AWAITING_ANSWER
    assert controller.view(started.mock_interview_id)["question"]["is_followup"]
    assert len(controller.interviews.turns(started.mock_interview_id)) == 2
    assert model.evaluations == 1
    with pytest.raises(MockInterviewConflict):
        controller.answer(started.mock_interview_id, "Different answer",
            expected_version=started.version, idempotency_key="answer-1")
    followup = controller.answer(started.mock_interview_id, "I wrote the validation tests.",
        expected_version=answered.version, idempotency_key="answer-2")
    assert followup.questions_completed == 1
    assert model.evaluations == 2
    ended = controller.end(started.mock_interview_id,
        expected_version=followup.version, idempotency_key="end-1")
    assert ended.status == MockStatus.COMPLETED
    report = controller.interviews.report(started.mock_interview_id)
    assert report["artifact_type"] == "interview_report"
    assert len(report["question_answer_summaries"]) == 3
    assert controller.end(started.mock_interview_id,
        expected_version=ended.version, idempotency_key="end-1").status == MockStatus.COMPLETED
    from sqlalchemy import text
    with db.session_factory() as session:
        assert session.execute(text("select count(*) from mock_interview_reports")).scalar_one() == 1
        assert session.execute(text("select count(*) from context_snapshots where status='used'")).scalar_one() >= 4


def test_restart_restores_pending_question(setup):
    controller, model, application_id, db = setup
    first = controller.start(application_id, idempotency_key="start-2")
    fresh = MockInterviewController(interviews=MockInterviewRepository(db.session_factory),
        sessions=SessionRepository(db.session_factory), workspace=controller.workspace,
        packs=controller.packs, evidence=controller.evidence,
        context_snapshots=ContextSnapshotRepository(db.session_factory), model=model)
    restored = fresh.resume(first.mock_interview_id,
        expected_version=first.version, idempotency_key="resume-1")
    assert restored.current_question_id == first.current_question_id
    assert model.questions == 1
    cancelled = fresh.cancel(first.mock_interview_id,
        expected_version=restored.version, idempotency_key="cancel-1")
    assert cancelled.status == MockStatus.CANCELLED


def test_start_idempotency_survives_completion(setup):
    controller, model, application_id, _ = setup
    first = controller.start(application_id, idempotency_key="same-start")
    controller.end(first.mock_interview_id, expected_version=first.version,
        idempotency_key="end-same-start")
    replay = controller.start(application_id, idempotency_key="same-start")
    assert replay.mock_interview_id == first.mock_interview_id
    assert replay.status == MockStatus.COMPLETED
    assert model.questions == 1


def test_skip_and_early_completion_are_bounded(setup):
    controller, model, application_id, _ = setup
    started = controller.start(application_id, idempotency_key="start-skip",
        target_question_count=3)
    next_state = controller.skip(started.mock_interview_id,
        expected_version=started.version, idempotency_key="skip-1")
    assert next_state.questions_completed == 1
    assert model.evaluations == 0
    assert controller.interviews.turns(started.mock_interview_id)[0]["answer"] is None
    completed = controller.end(started.mock_interview_id,
        expected_version=next_state.version, idempotency_key="early-end")
    assert completed.status == MockStatus.COMPLETED
    assert controller.interviews.report(started.mock_interview_id)["unanswered_question_ids"]


def test_invalid_evaluation_retries_once_then_fails_safely(setup):
    controller, model, application_id, _ = setup
    started = controller.start(application_id, idempotency_key="start-invalid")
    original = model.evaluate
    attempts = []

    def invalid(context):
        attempts.append(context)
        draft = original(context)
        return draft.model_copy(update={"exact_answer_quotes": ["Invented 50% revenue."]})

    model.evaluate = invalid
    failed = controller.answer(started.mock_interview_id, "Built Python APIs.",
        expected_version=started.version, idempotency_key="answer-invalid")
    assert failed.status == MockStatus.FAILED
    assert failed.error_code == "evaluation_failed"
    assert len(attempts) == 2
    assert "validation_feedback" in attempts[1]
    assert controller.interviews.turns(started.mock_interview_id)[0]["evaluation"] is None


def test_discovered_fact_remains_candidate_until_explicit_confirmation(setup):
    from eregion.runtime.evidence.repository import CareerEvidenceRepository
    controller, model, application_id, db = setup
    controller.evidence = CareerEvidenceRepository(db.session_factory)
    original = model.evaluate

    def discover(context):
        return original(context).model_copy(update={
            "discovered_fact_quote": context["answer_untrusted"]})

    model.evaluate = discover
    started = controller.start(application_id, idempotency_key="start-discovery")
    controller.answer(started.mock_interview_id,
        "I built a pytest suite for an internal Python service.",
        expected_version=started.version, idempotency_key="answer-discovery")
    ids = controller.interviews.candidate_ids(started.mock_interview_id)
    assert len(ids) == 1
    item = controller.evidence.get(ids[0])
    assert item.status.value == "candidate"
    assert item.current.source_type.value == "interview"
    assert item.current.exact_quote == "I built a pytest suite for an internal Python service."


def test_answer_replay_after_next_question_and_crash_boundary(setup):
    controller, model, application_id, db = setup
    started = controller.start(application_id, idempotency_key="start-crash",
        target_question_count=3, max_followups_per_question=0)
    answer, reused = controller.interviews.save_answer(started.mock_interview_id,
        started.current_question_id, "Built Python APIs.", started.version, "answer-crash")
    assert not reused
    fresh = MockInterviewController(interviews=MockInterviewRepository(db.session_factory),
        sessions=SessionRepository(db.session_factory), workspace=controller.workspace,
        packs=controller.packs, evidence=controller.evidence,
        context_snapshots=ContextSnapshotRepository(db.session_factory), model=model)
    evaluating = fresh.interviews.get(started.mock_interview_id)
    assert evaluating.status == MockStatus.EVALUATING
    next_state = fresh.resume(started.mock_interview_id,
        expected_version=evaluating.version, idempotency_key="recover-crash")
    assert next_state.current_question_id != started.current_question_id
    assert model.evaluations == 1
    replay = fresh.answer(started.mock_interview_id, "Built Python APIs.",
        expected_version=started.version, idempotency_key="answer-crash")
    assert replay.current_question_id == next_state.current_question_id
    assert model.evaluations == 1


def test_mock_api_and_worker_pause(setup):
    from fastapi.testclient import TestClient
    from eregion.api.main import create_app
    controller, model, application_id, _ = setup
    api = create_app(session_runtime=SimpleNamespace(mock_interviewer=controller))
    with TestClient(api) as client:
        started = client.post(f"/api/applications/{application_id}/mock-interviews",
            json={"idempotency_key": "api-start", "target_question_count": 3})
        assert started.status_code == 200
        state = started.json()["interview"]
        interview_id = state["mock_interview_id"]
        assert client.get(f"/api/mock-interviews/{interview_id}/plan").status_code == 200
        assert client.get(f"/api/mock-interviews/{interview_id}/turns").json()["turns"]
        assert client.get("/api/mock-interviews/not-found").status_code == 404
        duplicate = client.post(f"/api/mock-interviews/{interview_id}/answers", json={
            "answer": "Built Python APIs.", "expected_version": state["version"],
            "idempotency_key": "api-answer"})
        assert duplicate.status_code == 200
        assert client.post(f"/api/mock-interviews/{interview_id}/answers", json={
            "answer": "Different", "expected_version": state["version"],
            "idempotency_key": "api-answer"}).status_code == 409
        assert client.get(f"/api/mock-interviews/{interview_id}/report").json()["report"] is None
    task = SimpleNamespace(application_id=application_id, allowed_tools=frozenset(),
        result_summary={"pause_metadata": {"mock_interview_id": interview_id}},
        task_id="task-1", input_spec={})
    result = MockInterviewerWorker(controller).execute(task,
        SimpleNamespace(allowed_tools=frozenset()))
    assert result.awaiting_input
    assert result.pause_metadata["mock_interview_id"] == interview_id


def test_worker_starts_scoped_interview_without_tools(setup):
    controller, _, application_id, _ = setup
    task = SimpleNamespace(application_id=application_id, allowed_tools=frozenset(),
        result_summary={}, task_id=str(uuid4()), input_spec={"mode": "mixed"})
    worker = MockInterviewerWorker(controller)
    paused = worker.execute(task, SimpleNamespace(allowed_tools=frozenset()))
    assert paused.awaiting_input
    view = controller.view(paused.pause_metadata["mock_interview_id"])
    assert view["interview"]["root_task_id"] == task.task_id
    assert view["interview"]["application_id"] == application_id
    with pytest.raises(ValueError):
        worker.execute(SimpleNamespace(**{**task.__dict__, "allowed_tools": frozenset({"write"})}),
            SimpleNamespace(allowed_tools=frozenset()))


def test_migration_from_prior_populated_schema(tmp_path):
    from pathlib import Path
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect, text
    url = f"sqlite:///{(tmp_path / 'prior.db').as_posix()}"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0020_interview_match_cohorts")
    db = create_database(url)
    with db.engine.begin() as connection:
        connection.execute(text("""INSERT INTO runs
            (run_id, thread_id, status, backend, backend_source, resume_text,
             job_description, created_at, updated_at)
            VALUES ('old-run', 'old-run', 'approved', 'custom', 'stored',
                    'synthetic resume', 'synthetic JD', '2026-01-01', '2026-01-01')"""))
    db.close()
    command.upgrade(config, "head")
    db = create_database(url)
    assert {"mock_interviews", "mock_interview_plans", "mock_interview_plan_items",
            "mock_interview_questions", "mock_interview_answers",
            "mock_answer_evaluations", "mock_interview_reports",
            "mock_interview_candidates", "mock_interview_events"} <= set(
                inspect(db.engine).get_table_names())
    with db.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM runs WHERE run_id='old-run'")) == 1
    db.close()


@pytest.mark.parametrize("text, allowed", [
    ("How would you prevent a race condition when two workers update the same record?", True),
    ("Where could a data race appear in this queue, and how would you test for it?", True),
    ("How would you set max-age for cached API responses?", True),
    ("What is your race?", False),
    ("Which age group are you in?", False),
    ("Do you have a disability we should know about?", False),
    ("Would your religion affect weekend on-call shifts?", False),
])
def test_protected_attribute_check_ignores_technical_terms_only(text, allowed):
    from eregion.runtime.mock_interview.errors import MockInterviewValidation
    item = build_plan(interview_id="i", snapshot_id="s", snapshot_hash="h", evidence=[],
        requirement_ids=[], mode=InterviewMode.MIXED, target_count=3).items[0]
    question = MockInterviewQuestion(question_id="q", plan_item_id=item.plan_item_id, question_text=text,
        question_type=item.question_type, competency=item.competency, reason_for_asking="Practice.")
    if allowed:
        validate_question(question, item, set(), "")
    else:
        with pytest.raises(MockInterviewValidation, match="protected"):
            validate_question(question, item, set(), "")


def test_plan_pins_project_and_collaboration_questions_to_answerable_evidence():
    evidence = [{"evidence_id": "degree", "category": "education"},
                {"evidence_id": "skills", "category": "skill"},
                {"evidence_id": "intern", "category": "experience"},
                {"evidence_id": "agent", "category": "project"},
                {"evidence_id": "lab", "category": "experience"},
                {"evidence_id": "dashboard", "category": "project"}]
    mixed = build_plan(interview_id="i", snapshot_id="s", snapshot_hash="h", evidence=evidence,
        requirement_ids=["r"], mode=InterviewMode.MIXED, target_count=10).items
    pinned = {(item.competency, tuple(item.related_evidence_ids)) for item in mixed}
    assert ("collaboration", ("intern",)) in pinned and ("collaboration", ("lab",)) in pinned
    assert ("project_deep_dive", ("agent",)) in pinned and ("project_deep_dive", ("dashboard",)) in pinned
    assert not {"degree", "skills"} & {e for item in mixed for e in item.related_evidence_ids}
    deep = build_plan(interview_id="i", snapshot_id="s", snapshot_hash="h", evidence=evidence[:3],
        requirement_ids=[], mode=InterviewMode.PROJECT_DEEP_DIVE, target_count=4).items
    assert [item.related_evidence_ids for item in deep] == [["intern"]] * 4  # no project: experience
