"""Unified loop M4/M5: one owner for background work in the process."""
import re
import threading
from pathlib import Path

import pytest

from eregion.runtime.execution import ExecutionClosed, ExecutionService

ROOT = Path(__file__).resolve().parents[1]


def test_same_key_runs_once_until_it_finishes():
    service = ExecutionService()
    service.register_kind('k', max_workers=2)
    gate, calls = threading.Event(), []
    first = service.submit('k', 'a', lambda: (calls.append(1), gate.wait(5)))
    assert first is not None
    assert service.submit('k', 'a', lambda: calls.append(2)) is None  # de-duplicated
    other = service.submit('k', 'b', lambda: calls.append(3))
    other.result(5)
    gate.set(); first.result(5)
    service.submit('k', 'a', lambda: calls.append(4)).result(5)  # key released after finishing
    assert sorted(calls) == [1, 3, 4]
    service.close(wait=True)


def test_pool_is_bounded_per_kind():
    service = ExecutionService()
    service.register_kind('k', max_workers=2)
    lock, active, peak, gate = threading.Lock(), [0], [0], threading.Event()

    def job():
        with lock:
            active[0] += 1; peak[0] = max(peak[0], active[0])
        gate.wait(5)
        with lock:
            active[0] -= 1

    futures = [service.submit('k', None, job) for _ in range(6)]
    threading.Timer(0.3, gate.set).start()
    for future in futures:
        future.result(5)
    assert peak[0] == 2
    service.close(wait=True)


def test_recovery_runs_once_and_conflicting_bounds_are_rejected():
    service, runs = ExecutionService(), []
    service.register_kind('k', max_workers=1, recover=lambda: runs.append(1))
    assert service.recover('k') and not service.recover('k')
    assert runs == [1]
    with pytest.raises(ValueError):
        service.register_kind('k', max_workers=3)
    owner = service.register_owner('browser', recover=lambda: runs.append(2), role='affine')
    assert owner.pool is None and service.recover('browser') and runs == [1, 2]
    with pytest.raises(ValueError):
        service.submit('browser', 'x', lambda: None)
    service.close()


def test_closed_service_rejects_work_and_close_is_idempotent():
    service = ExecutionService()
    service.register_kind('k', max_workers=1)
    service.close(); service.close()
    with pytest.raises(ExecutionClosed):
        service.submit('k', 'a', lambda: None)


def test_composition_root_shares_one_execution_service(tmp_path, monkeypatch):
    from eregion.api.session_dependencies import create_session_runtime
    from eregion.runtime.auto_apply.loop import ApplicationLoop
    from eregion.runtime.auto_apply.repository import ApplyRepository
    runtime = create_session_runtime(migrate_schema=True, database_url=f"sqlite:///{(tmp_path / 'r.db').as_posix()}",
                                     model=object())
    try:
        assert runtime.agent_scheduler.execution is runtime.execution
        loop = ApplicationLoop(ApplyRepository(runtime.database.session_factory), execution=runtime.execution)
        try:
            inventory = runtime.execution.inventory()
            assert {'agent_task', 'application_run'} <= set(inventory['kinds'])
            assert 'agent-task-scheduler' in inventory['resources']
        finally:
            loop.close()
    finally:
        runtime.close()
    assert runtime.execution.closed


def test_task_owners_do_not_create_private_pools():
    """Pools for task ownership live in ExecutionService; other modules may not start their own."""
    owners = ['backend/eregion/runtime/tasks/loop.py', 'backend/eregion/runtime/career/service.py',
              'backend/eregion/runtime/career/interview_queue.py', 'backend/eregion/runtime/coordination/scheduler.py',
              'backend/eregion/runtime/auto_apply/loop.py']
    for name in owners:
        text = (ROOT / name).read_text(encoding='utf-8')
        assert not re.search(r'ThreadPoolExecutor\(', text), name


def test_startup_recovery_is_once_per_process_even_if_a_loop_is_rebuilt(tmp_path):
    from eregion.runtime.career.repository import TaskRepository
    from eregion.runtime.tasks.loop import TaskLoop
    from eregion.api.db import create_database
    db = create_database(f"sqlite:///{(tmp_path / 't.db').as_posix()}", create_schema_for_tests=True)
    repository = TaskRepository(db.session_factory)
    state, _ = repository.create('find jobs', 'key-1')
    repository.change(state['task_id'], status='queued')
    driven = []

    class Recording(TaskLoop):
        def schedule(self, task_id):
            driven.append(task_id)  # record instead of running a model

    service = ExecutionService()
    first = Recording(repository, harness=object(), execution=service)
    second = Recording(repository, harness=object(), execution=service)  # e.g. a rebuilt service object
    try:
        assert driven == [state['task_id']]
        assert service.inventory()['kinds']['career_task']['recovered'] is True
        assert 'task-events' in service.inventory()['resources']
    finally:
        first.close(); second.close(); service.close(wait=True); db.close()
