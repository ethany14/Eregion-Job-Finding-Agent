"""Deterministic, model-free task graph and isolation tests."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from eregion.api.db import create_database
from eregion.runtime.coordination.context import AgentContextPolicy
from eregion.runtime.coordination.errors import InvalidPlanError, LeaseConflictError, TaskConflictError
from eregion.runtime.coordination.policy import AgentPlanValidator
from eregion.runtime.coordination.registry import AgentWorkerRegistry
from eregion.runtime.coordination.repository import AgentTaskRepository
from eregion.runtime.coordination.scheduler import AgentTaskScheduler
from eregion.runtime.coordination.types import (
    AgentPlan, AgentTaskResult, DependencyType, MultiAgentBudget, OutputArtifact,
    TaskDependencySpec, TaskSpec, TaskStatus,
)
from eregion.runtime.sessions.clock import FakeClock
from eregion.runtime.sessions.events import SessionEvent, SessionEventType
from eregion.runtime.sessions.repository import SessionRepository
from eregion.runtime.sessions.state import SessionMessageDraft, SessionState
from eregion.runtime.tools.messages import AgentMessage


class EchoWorker:
    task_type = "echo"
    def execute(self, task, context):
        return AgentTaskResult(summary="Done.", output_artifacts=[OutputArtifact(
            role="echo", content={"visible_inputs": sorted(context.input_artifacts),
                "parent_message_ids": list(context.parent_message_ids)})])


class FailingWorker:
    task_type = "fail"
    def execute(self, task, context):
        raise RuntimeError("secret raw provider error")


@pytest.fixture
def setup(tmp_path):
    db = create_database(f"sqlite:///{tmp_path / 'agent.db'}", create_schema_for_tests=True)
    clock = FakeClock()
    sessions = SessionRepository(db.session_factory)
    parent = SessionState(session_id="parent", user_id="local-user", allowed_tools=frozenset())
    sessions.create(parent, SessionEvent(session_id="parent", event_type=SessionEventType.SESSION_CREATED),
        messages=[SessionMessageDraft(message=AgentMessage(role="system", content="hidden")),
                  SessionMessageDraft(message=AgentMessage(role="user", content="public"))])
    workers = AgentWorkerRegistry()
    workers.register(EchoWorker())
    workers.register(FailingWorker())
    tasks = AgentTaskRepository(db.session_factory, clock=clock)
    contexts = AgentContextPolicy(tasks=tasks, sessions=sessions, registered_tools=frozenset(),
        task_type_tools={"echo": frozenset(), "fail": frozenset()}, parent_allowed_tools=frozenset())
    scheduler = AgentTaskScheduler(tasks=tasks, sessions=sessions, workers=workers, contexts=contexts)
    yield db, clock, tasks, sessions, workers, scheduler
    scheduler.stop()
    db.close()


def make_plan(*, second: bool = False, task_type: str = "echo", key: str = "plan"):
    specs = [TaskSpec(key="root", task_type=task_type, agent_role="tester",
        recent_parent_message_limit=2, max_attempts=2)]
    deps = []
    if second:
        specs.append(TaskSpec(key="child", task_type="echo", agent_role="reader",
            parent_key="root", input_from_tasks={"echo": "root"}))
        deps.append(TaskDependencySpec(task_key="child", depends_on_key="root",
            dependency_type=DependencyType.REQUIRES_SUCCESS))
    return AgentPlan(template_id="test", parent_session_id="parent", root_key="root",
        tasks=specs, dependencies=deps, idempotency_key=key)


def validate(plan):
    return AgentPlanValidator(worker_types=frozenset({"echo", "fail"}),
        registered_tools=frozenset(), parent_tools=frozenset(),
        session_tools=frozenset()).validate(plan)


@pytest.mark.parametrize("limit,expected_calls", [(0, 0), (1, 1)])
def test_model_budget_is_reserved_before_calls_and_survives_retry(setup, limit, expected_calls):
    from eregion.runtime.coordination.model_usage import invoke_model
    _, _, tasks, _, workers, scheduler = setup
    calls = []
    def provider():
        calls.append(1)
        raise RuntimeError("Provider failed after accepting the request")
    workers.get("echo").execute = lambda *args: invoke_model(provider)
    plan = make_plan()
    plan.budget = MultiAgentBudget(max_model_calls=limit)
    root = tasks.create_plan(plan, validate(plan))
    failed = scheduler.run_one(root.task_id)
    assert failed.status == TaskStatus.FAILED
    assert len(calls) == expected_calls
    tasks.retry(root.task_id, expected_version=failed.version)
    failed = scheduler.run_one(root.task_id)
    assert failed.error_code == "budget_exceeded"
    assert len(calls) == expected_calls


def test_parallel_workers_share_one_model_budget(setup):
    from concurrent.futures import ThreadPoolExecutor
    from eregion.runtime.coordination.model_usage import invoke_model
    _, _, tasks, _, workers, scheduler = setup
    calls = []
    def provider():
        calls.append(1)
        return "Done"
    workers.get("echo").execute = lambda *args: AgentTaskResult(summary=invoke_model(provider))
    plan = make_plan(second=True)
    plan.dependencies = []
    plan.tasks[1].input_from_tasks = {}
    plan.budget = MultiAgentBudget(max_model_calls=1)
    root = tasks.create_plan(plan, validate(plan))
    ids = [root.task_id, tasks.children(root.task_id)[0].task_id]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(scheduler.run_one, ids))
    assert len(calls) == 1
    assert sorted(task.status.value for task in results) == ["failed", "succeeded"]
    success = next(task for task in results if task.status == TaskStatus.SUCCEEDED)
    assert success.result_summary["usage"]["model_calls"] == 1


@pytest.mark.parametrize("status_code", [200, 500])
def test_provider_usage_and_retry_cap_are_applied_at_http_boundary(setup, status_code):
    import json
    import httpx
    from eregion.domain.model import ProviderCompatibleChatOpenAI, invoke_structured
    from eregion.domain.schemas import ResumeAnalysis
    from eregion.runtime.coordination.model_usage import invoke_model
    _, _, tasks, _, workers, scheduler = setup
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        if status_code == 500:
            return httpx.Response(500, json={"error": {"message": "Unavailable", "type": "server_error"}})
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
            "model": "test-model", "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps({"summary": "Developer", "skills": [],
                    "education": [], "evidence": []})}}],
            "usage": {"prompt_tokens": 17, "completion_tokens": 9, "total_tokens": 26}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = ProviderCompatibleChatOpenAI(model="test-model", api_key="test-key",
            base_url="https://provider.test/v1", http_client=client, max_retries=3,
            use_responses_api=False)
        def execute(*args):
            result = invoke_model(invoke_structured, model, ResumeAnalysis, "Extract", "Developer")
            return AgentTaskResult(summary=result.summary)
        workers.get("echo").execute = execute
        plan = make_plan()
        plan.budget = MultiAgentBudget(max_model_calls=1, max_output_tokens=50)
        root = tasks.create_plan(plan, validate(plan))
        result = scheduler.run_one(root.task_id)
    assert len(requests) == 1
    assert requests[0].get("max_completion_tokens", requests[0].get("max_tokens")) == 50
    assert tasks.plan_usage(root.task_id)["model_calls"] == 1
    if status_code == 200:
        assert result.status == TaskStatus.SUCCEEDED
        assert result.result_summary["usage"] == {"model_calls": 1, "input_tokens": 17, "output_tokens": 9}
    else:
        assert result.status == TaskStatus.FAILED
        assert tasks.plan_usage(root.task_id)["output_tokens"] == 50


def test_plan_validation_and_idempotency(setup):
    _, _, tasks, _, _, _ = setup
    plan = make_plan(second=True)
    root = tasks.create_plan(plan, validate(plan))
    assert root.status == TaskStatus.READY
    assert tasks.create_plan(plan, validate(plan)).task_id == root.task_id
    changed = plan.model_copy(deep=True)
    changed.tasks[0].priority = 5
    with pytest.raises(TaskConflictError):
        tasks.create_plan(changed, validate(changed))
    assert tasks.children(root.task_id)[0].status == TaskStatus.BLOCKED
    with pytest.raises(InvalidPlanError):
        validate(AgentPlan(template_id="bad", parent_session_id="parent", root_key="root",
            tasks=[TaskSpec(key="root", task_type="unknown", agent_role="x")], idempotency_key="bad"))
    with pytest.raises(InvalidPlanError):
        validate(AgentPlan(template_id="cycle", parent_session_id="parent", root_key="root",
            tasks=plan.tasks, dependencies=plan.dependencies + [TaskDependencySpec(
                task_key="root", depends_on_key="child")], idempotency_key="cycle"))


def test_dependency_artifact_and_child_isolation(setup):
    _, _, tasks, sessions, _, scheduler = setup
    plan = make_plan(second=True)
    root = tasks.create_plan(plan, validate(plan))
    done = scheduler.run_one(root.task_id)
    assert done.status == TaskStatus.SUCCEEDED
    child = tasks.children(root.task_id)[0]
    assert child.status == TaskStatus.READY
    assert len(tasks.artifact_links(child.task_id)) == 1
    assert scheduler.run_one(child.task_id).status == TaskStatus.SUCCEEDED
    child_session = sessions.require(tasks.require(child.task_id).child_session_id)
    assert child_session.parent_session_id == "parent"
    assert child_session.task_id == child.task_id
    messages = sessions.messages(child_session.session_id)
    assert len(messages) == 1 and messages[0].message.role == "system"
    assert "public" not in messages[0].message.content
    assert tasks.events(child.task_id)[-1].event_type.value == "task_succeeded"


def test_failure_retry_and_stale_attempt(setup):
    _, _, tasks, _, _, scheduler = setup
    plan = make_plan(task_type="fail")
    root = tasks.create_plan(plan, validate(plan))
    assert scheduler.run_one(root.task_id).status == TaskStatus.FAILED
    failed = tasks.require(root.task_id)
    assert failed.error_code == "worker_failed"
    assert "secret" not in str([event.model_dump() for event in tasks.events(root.task_id)])
    retried = tasks.retry(root.task_id, expected_version=failed.version)
    assert retried.status == TaskStatus.READY
    claimed = tasks.claim(root.task_id, expected_version=retried.version, worker_id="old")
    with pytest.raises(LeaseConflictError):
        tasks.claim(root.task_id, expected_version=claimed.version, worker_id="new")


def test_expired_claim_recovery_fences_old_worker(setup):
    _, clock, tasks, _, _, _ = setup
    plan = make_plan()
    root = tasks.create_plan(plan, validate(plan))
    old = tasks.claim(root.task_id, expected_version=root.version, worker_id="old", lease_seconds=30)
    clock.advance(seconds=31)
    new = tasks.claim(root.task_id, expected_version=old.version, worker_id="new", lease_seconds=30)
    assert new.active_attempt_id != old.active_attempt_id
    with pytest.raises(LeaseConflictError):
        tasks.start(root.task_id, old.active_attempt_id, child_session_id="stale",
            context_snapshot_id="stale", context_hash="0" * 64, manifest={})


def test_cancellation_and_atomic_output(setup):
    db, _, tasks, _, _, scheduler = setup
    plan = make_plan()
    root = tasks.create_plan(plan, validate(plan))
    cancelled = tasks.cancel(root.task_id, expected_version=root.version)
    assert cancelled.status == TaskStatus.CANCELLED
    assert not tasks.artifact_links(root.task_id)
    with pytest.raises(TaskConflictError):
        tasks.retry(root.task_id, expected_version=cancelled.version)


def test_root_cancellation_propagates_to_descendants(setup):
    _, _, tasks, _, _, _ = setup
    plan = make_plan(second=True)
    root = tasks.create_plan(plan, validate(plan))
    child = tasks.children(root.task_id)[0]
    tasks.cancel(root.task_id, expected_version=root.version)
    assert tasks.require(root.task_id).status == TaskStatus.CANCELLED
    assert tasks.require(child.task_id).status == TaskStatus.CANCELLED


def test_migration_from_populated_schema(tmp_path):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0017_application_pack")
    old = create_engine(url)
    with old.begin() as connection:
        connection.execute(text("""INSERT INTO runs
            (run_id, thread_id, status, backend, backend_source, resume_text,
             job_description, created_at, updated_at)
            VALUES ('old-run', 'old-run', 'approved', 'custom', 'stored',
                    'synthetic resume', 'synthetic JD', '2026-01-01', '2026-01-01')"""))
    old.dispose()
    command.upgrade(config, "head")
    db = create_database(url)
    from sqlalchemy import inspect, text
    names = inspect(db.engine).get_table_names()
    assert {"agent_tasks", "agent_task_attempts", "agent_task_dependencies",
            "agent_task_artifacts", "agent_plans"} <= set(names)
    with db.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM runs WHERE run_id='old-run'")) == 1
    db.close()


def test_task_tool_context_and_scope(setup):
    _, _, tasks, sessions, _, _ = setup
    root = tasks.create_plan(make_plan(), validate(make_plan()))
    claimed = tasks.claim(root.task_id, expected_version=root.version, worker_id="test")
    contexts = AgentContextPolicy(tasks=tasks, sessions=sessions,
        registered_tools=frozenset(), task_type_tools={"echo": frozenset()},
        parent_allowed_tools=frozenset())
    context, manifest = contexts.prepare(claimed, attempt_id=claimed.active_attempt_id,
        child_session_id="child", budget=MultiAgentBudget())
    assert context.tool_context().task_id == root.task_id
    assert context.tool_context().attempt_id == claimed.active_attempt_id
    assert manifest["included_message_ids"] == [sessions.messages("parent")[-1].message_id]
    assert manifest["memory"] == [] and manifest["evidence"] == []


def test_plan_rejects_unapproved_tool_and_depth(setup):
    plan = make_plan()
    plan.tasks[0].allowed_tools = frozenset({"unsafe_write"})
    with pytest.raises(InvalidPlanError):
        validate(plan)
    plan = make_plan(second=True)
    plan.budget.max_depth = 0
    with pytest.raises(InvalidPlanError):
        validate(plan)


def test_failed_dependency_stays_blocked(setup):
    _, _, tasks, _, _, scheduler = setup
    plan = make_plan(second=True, task_type="fail")
    root = tasks.create_plan(plan, validate(plan))
    assert scheduler.run_one(root.task_id).status == TaskStatus.FAILED
    child = tasks.children(root.task_id)[0]
    assert child.status == TaskStatus.BLOCKED
    assert child.error_code == "dependency_failed"


def test_running_cancel_fences_result(setup):
    _, _, tasks, sessions, workers, scheduler = setup
    class CancelWorker:
        task_type = "cancel_self"
        def execute(self, task, context):
            current = tasks.require(task.task_id)
            tasks.cancel(task.task_id, expected_version=current.version)
            return AgentTaskResult(summary="late", output_artifacts=[
                OutputArtifact(role="late", content={"secret": "should not persist"})])
    workers.register(CancelWorker())
    scheduler.contexts.task_type_tools["cancel_self"] = frozenset()
    plan = make_plan(task_type="cancel_self")
    depths = AgentPlanValidator(worker_types=workers.names(), registered_tools=frozenset(),
        parent_tools=frozenset(), session_tools=frozenset()).validate(plan)
    root = tasks.create_plan(plan, depths)
    assert scheduler.run_one(root.task_id).status == TaskStatus.CANCELLED
    assert not tasks.artifact_links(root.task_id)


def test_deadline_during_worker_fences_result(setup):
    _, clock, tasks, _, workers, scheduler = setup
    class DeadlineWorker:
        task_type = "deadline"
        def execute(self, task, context):
            clock.advance(seconds=10)
            return AgentTaskResult(summary="late", output_artifacts=[
                OutputArtifact(role="late", content={"value": 1})])
    workers.register(DeadlineWorker())
    scheduler.contexts.task_type_tools["deadline"] = frozenset()
    plan = make_plan(task_type="deadline")
    plan.tasks[0].deadline_at = clock.now().replace(second=5)
    depths = AgentPlanValidator(worker_types=workers.names(), registered_tools=frozenset(),
        parent_tools=frozenset(), session_tools=frozenset()).validate(plan)
    root = tasks.create_plan(plan, depths)
    assert scheduler.run_one(root.task_id).status == TaskStatus.TIMED_OUT
    assert not tasks.artifact_links(root.task_id)


def test_paused_task_deadline_expires_without_running_worker(setup):
    _, clock, tasks, _, _, scheduler = setup
    plan = make_plan()
    plan.tasks[0].deadline_at = clock.now().replace(second=5)
    root = tasks.create_plan(plan, validate(plan))
    clock.advance(seconds=6)
    assert scheduler.dispatch_ready() == 0
    assert tasks.require(root.task_id).status == TaskStatus.TIMED_OUT


def test_root_deadline_bounds_child_deadline(setup):
    from datetime import timedelta
    _, clock, tasks, _, _, _ = setup
    plan = make_plan(second=True)
    plan.budget.deadline_at = clock.now() + timedelta(seconds=5)
    plan.tasks[1].deadline_at = clock.now() + timedelta(seconds=30)
    root = tasks.create_plan(plan, validate(plan))
    child = tasks.children(root.task_id)[0]
    assert child.deadline_at == plan.budget.deadline_at


def test_template_restricted_api_and_safe_task_response(setup):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from eregion.api.main import create_app
    from eregion.api.session_dependencies import get_session_runtime
    from eregion.runtime.coordination.templates import AgentPlanTemplateRegistry
    _, _, tasks, sessions, workers, scheduler = setup
    templates = AgentPlanTemplateRegistry()
    templates.register("safe_echo", lambda session_id, key: AgentPlan(
        template_id="safe_echo", parent_session_id=session_id, root_key="root",
        tasks=[TaskSpec(key="root", task_type="echo", agent_role="reader")],
        idempotency_key=key))
    runtime = SimpleNamespace(agent_tasks=tasks, agent_workers=workers,
        agent_plan_templates=templates, sessions=sessions,
        registry=SimpleNamespace(names=lambda: frozenset()), workspace=None, skills=None,
        agent_scheduler=scheduler)
    app = create_app(run_service=object())
    app.dependency_overrides[get_session_runtime] = lambda: runtime
    with TestClient(app) as client:
        denied = client.post("/api/agent-plans", json={"template_id": "python.module",
            "parent_session_id": "parent", "idempotency_key": "x", "expected_session_version": 0})
        assert denied.status_code == 422
        created = client.post("/api/agent-plans", json={"template_id": "safe_echo",
            "parent_session_id": "parent", "idempotency_key": "x", "expected_session_version": 0})
        assert created.status_code == 200
        body = created.json()
        assert body["status"] == "ready"
        assert "input_spec" not in body and "system_prompt" not in str(body)
        assert client.get(f"/api/agent-tasks/{body['task_id']}").status_code == 200
        assert client.get(f"/api/agent-tasks/{body['task_id']}/events").status_code == 200
        assert client.get(f"/api/agent-tasks/{body['task_id']}/artifacts").json() == []
        assert client.get("/api/agent-tasks/missing").status_code == 404
        cancelled = client.post(f"/api/agent-tasks/{body['task_id']}/cancel",
            json={"expected_version": body["version"]})
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"


def test_scheduler_restart_scans_expired_claim(setup):
    _, clock, tasks, sessions, workers, scheduler = setup
    plan = make_plan()
    root = tasks.create_plan(plan, validate(plan))
    old = tasks.claim(root.task_id, expected_version=root.version, worker_id="old", lease_seconds=20)
    clock.advance(seconds=21)
    replacement = AgentTaskScheduler(tasks=tasks, sessions=sessions, workers=workers,
        contexts=scheduler.contexts)
    try:
        assert replacement.dispatch_ready() == 1
        for future in list(replacement._futures):
            assert future.result(timeout=5).status == TaskStatus.SUCCEEDED
        assert tasks.require(root.task_id).attempt_count == 2
        assert any(event.event_type.value == "task_recovered" for event in tasks.events(root.task_id))
    finally:
        replacement.stop()


def test_expired_attempt_limit_fails_without_reexecution(setup):
    _, clock, tasks, sessions, workers, scheduler = setup
    plan = make_plan()
    plan.tasks[0].max_attempts = 1
    root = tasks.create_plan(plan, validate(plan))
    tasks.claim(root.task_id, expected_version=root.version, worker_id="old", lease_seconds=20)
    clock.advance(seconds=21)
    assert scheduler.dispatch_ready() == 0
    failed = tasks.require(root.task_id)
    assert failed.status == TaskStatus.FAILED
    assert failed.error_code == "attempt_limit_reached"


def test_budget_exceeded_does_not_publish_artifact(setup):
    _, _, tasks, _, workers, scheduler = setup
    class CostlyWorker:
        task_type = "costly"
        def execute(self, task, context):
            return AgentTaskResult(summary="expensive", usage={"model_calls": 2},
                output_artifacts=[OutputArtifact(role="x", content={"value": 1})])
    workers.register(CostlyWorker())
    scheduler.contexts.task_type_tools["costly"] = frozenset()
    plan = make_plan(task_type="costly")
    plan.budget.max_model_calls = 1
    depths = AgentPlanValidator(worker_types=workers.names(), registered_tools=frozenset(),
        parent_tools=frozenset(), session_tools=frozenset()).validate(plan)
    root = tasks.create_plan(plan, depths)
    result = scheduler.run_one(root.task_id)
    assert result.status == TaskStatus.FAILED and result.error_code == "budget_exceeded"
    assert tasks.artifact_links(root.task_id) == []


def test_child_session_is_not_exposed_by_public_session_api(setup):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from eregion.api.main import create_app
    from eregion.api.session_dependencies import get_session_runtime
    _, _, tasks, sessions, workers, scheduler = setup
    plan = make_plan()
    root = tasks.create_plan(plan, validate(plan))
    child_id = scheduler.run_one(root.task_id).child_session_id
    runtime = SimpleNamespace(sessions=sessions, agent_tasks=tasks,
        agent_workers=workers, agent_plan_templates=None)
    app = create_app(run_service=object())
    app.dependency_overrides[get_session_runtime] = lambda: runtime
    with TestClient(app) as client:
        assert client.get(f"/sessions/{child_id}").status_code == 403
        assert client.get(f"/sessions/{child_id}/messages").status_code == 403
        assert client.get("/sessions").status_code == 200
        assert child_id not in str(client.get("/sessions").json())


def test_independent_tasks_overlap_with_bounded_parallelism(setup):
    from threading import Barrier, Lock
    _, _, tasks, sessions, workers, scheduler = setup
    barrier = Barrier(2)
    guard = Lock()
    measurements = {"active": 0, "peak": 0}
    class ParallelWorker:
        task_type = "parallel"
        def execute(self, task, context):
            with guard:
                measurements["active"] += 1
                measurements["peak"] = max(measurements["peak"], measurements["active"])
            barrier.wait(timeout=5)
            with guard:
                measurements["active"] -= 1
            return AgentTaskResult(summary="ok")
    workers.register(ParallelWorker())
    scheduler.contexts.task_type_tools["parallel"] = frozenset()
    plan = AgentPlan(template_id="parallel", parent_session_id="parent", root_key="root",
        tasks=[TaskSpec(key="root", task_type="parallel", agent_role="root"),
               TaskSpec(key="child", task_type="parallel", agent_role="child", parent_key="root")],
        idempotency_key="parallel", budget=MultiAgentBudget(max_parallel_tasks=2))
    depths = AgentPlanValidator(worker_types=workers.names(), registered_tools=frozenset(),
        parent_tools=frozenset(), session_tools=frozenset()).validate(plan)
    root = tasks.create_plan(plan, depths)
    assert scheduler.dispatch_ready() == 2
    for future in list(scheduler._futures):
        assert future.result(timeout=10).status == TaskStatus.SUCCEEDED
    assert measurements["peak"] == 2


def test_database_reopen_recovers_expired_task(tmp_path):
    url = f"sqlite:///{tmp_path / 'restart.db'}"
    clock = FakeClock()
    db = create_database(url, create_schema_for_tests=True)
    sessions = SessionRepository(db.session_factory)
    sessions.create(SessionState(session_id="parent"),
        SessionEvent(session_id="parent", event_type=SessionEventType.SESSION_CREATED))
    tasks = AgentTaskRepository(db.session_factory, clock=clock)
    plan = make_plan()
    root = tasks.create_plan(plan, validate(plan))
    tasks.claim(root.task_id, expected_version=root.version, worker_id="before-restart", lease_seconds=10)
    db.close()
    clock.advance(seconds=11)
    reopened = create_database(url)
    try:
        resumed_sessions = SessionRepository(reopened.session_factory)
        resumed_tasks = AgentTaskRepository(reopened.session_factory, clock=clock)
        workers = AgentWorkerRegistry(); workers.register(EchoWorker())
        contexts = AgentContextPolicy(tasks=resumed_tasks, sessions=resumed_sessions,
            registered_tools=frozenset(), parent_allowed_tools=frozenset(),
            task_type_tools={"echo": frozenset()})
        scheduler = AgentTaskScheduler(tasks=resumed_tasks, sessions=resumed_sessions,
            workers=workers, contexts=contexts)
        try:
            recovered = scheduler.recover_expired(root.task_id)
            assert recovered.status == TaskStatus.SUCCEEDED
            assert recovered.attempt_count == 2
            assert len(resumed_tasks.artifact_links(root.task_id)) == 1
        finally:
            scheduler.stop()
    finally:
        reopened.close()


def test_duplicate_output_rolls_back_artifacts_and_success_transition(setup):
    _, _, tasks, _, workers, scheduler = setup
    class DuplicateOutputWorker:
        task_type = "duplicate_output"
        def execute(self, task, context):
            return AgentTaskResult(summary="duplicate", output_artifacts=[
                OutputArtifact(role="same", content={"a": 1}),
                OutputArtifact(role="same", content={"a": 2})])
    workers.register(DuplicateOutputWorker())
    scheduler.contexts.task_type_tools["duplicate_output"] = frozenset()
    plan = make_plan(task_type="duplicate_output")
    depths = AgentPlanValidator(worker_types=workers.names(), registered_tools=frozenset(),
        parent_tools=frozenset(), session_tools=frozenset()).validate(plan)
    root = tasks.create_plan(plan, depths)
    failed = scheduler.run_one(root.task_id)
    assert failed.status == TaskStatus.FAILED
    assert tasks.artifact_links(root.task_id) == []
    assert not any(event.event_type.value == "task_succeeded" for event in tasks.events(root.task_id))


def test_approval_worker_pauses_and_resumes_with_persisted_tool_binding(tmp_path):
    from pydantic import BaseModel
    from eregion.runtime.executor import ToolExecutor
    from eregion.runtime.policy import ToolPolicy
    from eregion.runtime.registry import ToolRegistry
    from eregion.runtime.repository import ToolCallRepository
    from eregion.runtime.types import ToolCallRequest, ToolExecutionStatus, ToolResult, ToolRiskLevel
    class Input(BaseModel):
        value: str
    class WriteTool:
        name = "fake_write"
        version = "1"
        description = "Test-only local write."
        risk_level = ToolRiskLevel.LOCAL_WRITE
        input_schema = Input
        calls = 0
        def execute(self, arguments, context):
            self.calls += 1
            return ToolResult(output={"ok": arguments.value})
    db = create_database(f"sqlite:///{tmp_path / 'approval.db'}", create_schema_for_tests=True)
    sessions = SessionRepository(db.session_factory)
    sessions.create(SessionState(session_id="parent", allowed_tools=frozenset({"fake_write"})),
        SessionEvent(session_id="parent", event_type=SessionEventType.SESSION_CREATED))
    tasks = AgentTaskRepository(db.session_factory)
    registry = ToolRegistry(); tool = WriteTool(); registry.register(tool)
    executor = ToolExecutor(registry, policy=ToolPolicy(),
        repository=ToolCallRepository(db.session_factory))
    class ApprovalWorker:
        task_type = "approval"
        call_id = None
        def execute(self, task, context):
            record = executor.execute(ToolCallRequest(tool_name="fake_write",
                arguments={"value": "synthetic"}, idempotency_key="one"), context.tool_context())
            self.call_id = record.call_id
            if record.status == ToolExecutionStatus.APPROVAL_REQUIRED:
                return AgentTaskResult(summary="Waiting for approval.", awaiting_approval=True)
            assert record.status == ToolExecutionStatus.COMPLETED
            return AgentTaskResult(summary="Approved test write completed.")
    worker = ApprovalWorker(); workers = AgentWorkerRegistry(); workers.register(worker)
    contexts = AgentContextPolicy(tasks=tasks, sessions=sessions,
        registered_tools=frozenset({"fake_write"}), parent_allowed_tools=frozenset({"fake_write"}),
        task_type_tools={"approval": frozenset({"fake_write"})})
    scheduler = AgentTaskScheduler(tasks=tasks, sessions=sessions, workers=workers, contexts=contexts)
    try:
        plan = AgentPlan(template_id="approval", parent_session_id="parent", root_key="root",
            tasks=[TaskSpec(key="root", task_type="approval", agent_role="tester",
                allowed_tools=frozenset({"fake_write"}), max_attempts=2)], idempotency_key="approval")
        depths = AgentPlanValidator(worker_types=workers.names(), registered_tools=registry.names(),
            parent_tools=frozenset({"fake_write"}), session_tools=frozenset({"fake_write"})).validate(plan)
        root = tasks.create_plan(plan, depths)
        paused = scheduler.run_one(root.task_id)
        assert paused.status == TaskStatus.AWAITING_APPROVAL and tool.calls == 0
        assert tasks.require(root.task_id).status == TaskStatus.AWAITING_APPROVAL
        call = executor._repository.require(worker.call_id)
        executor.approve(call.call_id, expected_version=call.version)
        resumed = tasks.resume(root.task_id, expected_version=paused.version)
        resumed_result = scheduler.run_one(resumed.task_id)
        assert resumed_result.status == TaskStatus.SUCCEEDED
        assert tool.calls == 1
        assert executor._repository.require(call.call_id).status == ToolExecutionStatus.COMPLETED
        second_plan = plan.model_copy(deep=True)
        second_plan.idempotency_key = "approval-cancel"
        other = tasks.create_plan(second_plan, depths)
        waiting = scheduler.run_one(other.task_id)
        assert waiting.status == TaskStatus.AWAITING_APPROVAL
        pending_call_id = worker.call_id
        cancelled = tasks.cancel(other.task_id, expected_version=waiting.version)
        assert cancelled.status == TaskStatus.CANCELLED
        assert executor._repository.require(pending_call_id).status == ToolExecutionStatus.DENIED
    finally:
        scheduler.stop(); db.close()


def test_context_rejects_cross_session_memory_and_unlinked_interview_evidence(setup):
    from types import SimpleNamespace
    from eregion.runtime.memory.types import MemoryScope, MemorySensitivity, MemoryStatus
    from eregion.runtime.evidence.types import EvidenceSourceType, EvidenceStatus
    _, _, tasks, sessions, _, _ = setup
    plan = make_plan()
    plan.tasks[0].input_spec = {"memory_ids": ["other-session-memory"],
        "evidence_ids": ["interview-other-job"]}
    root = tasks.create_plan(plan, validate(plan))
    claimed = tasks.claim(root.task_id, expected_version=root.version, worker_id="test")
    memory = SimpleNamespace(memory_id="other-session-memory", version=1, owner_id="local-user",
        status=MemoryStatus.CONFIRMED, sensitivity=MemorySensitivity.NORMAL,
        scope=MemoryScope.SESSION, scope_id="sibling", memory_key="preference.test",
        display_text="Private sibling data", content={}, expires_at=None, valid_from=None, valid_to=None)
    evidence = SimpleNamespace(status=EvidenceStatus.CONFIRMED,
        current=SimpleNamespace(source_type=EvidenceSourceType.INTERVIEW,
            evidence_version_id="v1", content_hash="x" * 64, claim_text="Private"))
    memories = SimpleNamespace(get=lambda *_args, **_kwargs: memory)
    vault = SimpleNamespace(get=lambda *_args: evidence,
        list_for_application=lambda *_args: [])
    policy = AgentContextPolicy(tasks=tasks, sessions=sessions,
        registered_tools=frozenset(), parent_allowed_tools=frozenset(),
        task_type_tools={"echo": frozenset()}, memories=memories, evidence=vault)
    with pytest.raises(TaskConflictError, match="another scope"):
        policy.prepare(claimed, attempt_id=claimed.active_attempt_id,
            child_session_id="child", budget=MultiAgentBudget())
    memory.scope_id = "parent"
    with pytest.raises(TaskConflictError, match="another Application"):
        policy.prepare(claimed, attempt_id=claimed.active_attempt_id,
            child_session_id="child", budget=MultiAgentBudget())
