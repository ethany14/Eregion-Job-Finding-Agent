"""W6 review: the whole-task provider budget covers the career agent, application worker
threads, nested calls and retries; it refuses before sending; unknown usage is never zero."""
import pytest

from eregion.runtime.provider_usage import (TaskProviderBudgetExceeded, UsageLedger, provider_call,
                                             summarize, usage_scope)
from eregion.runtime.auto_apply.loop import ApplicationLoop
from eregion.runtime.auto_apply.repository import ApplyRepository
from test_auto_apply import FakeBrowser, database, request, wait  # noqa: F401  (fixture)


def attempt(sent, *, reservation=1000, usage=None, purpose='agent'):
    """One provider attempt: `sent` records that the request body actually ran."""
    with usage_scope(purpose=purpose):
        with provider_call('fixture-model', input_token_reservation=reservation) as details:
            sent.append(purpose)
            if usage:
                details.update(input_tokens=usage, output_tokens=10)


def task_rows(repo, task_id):
    import json
    from sqlalchemy import select
    from eregion.runtime.provider_usage import ProviderUsageRow
    with repo.sessions() as db:
        return [json.loads(r.details_json) for r in db.scalars(select(ProviderUsageRow).where(ProviderUsageRow.task_id == task_id))]


@pytest.fixture
def loop(database, monkeypatch):
    repo = ApplyRepository(database.session_factory)
    loop = ApplicationLoop(repo, browser_factory=FakeBrowser)
    yield loop
    loop.close()


def test_career_and_application_worker_share_one_attempt_cap_checked_before_sending(loop, monkeypatch):
    monkeypatch.setenv('JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS', '9')
    repo = loop.repository
    run = loop.start(request())['run_id']; state = wait(loop, run)
    repo.save(state, origin_task_id='task-A')
    sent = []
    ledger = UsageLedger(repo.sessions)
    with usage_scope(ledger=ledger, task_id='task-A'):  # the career agent
        for _ in range(3):
            attempt(sent, usage=100)
    monkeypatch.setattr(loop, '_active', lambda run_id: repo.get(run_id))
    def worker():  # the application thread: its own calls, a nested decision and a retry
        attempt(sent, usage=100, purpose='application')
        attempt(sent, usage=100, purpose='jev:form_options')
        with usage_scope(attempt=2):
            attempt(sent, usage=100, purpose='application')
    loop._metered(run, worker)
    rows = task_rows(repo, 'task-A')
    assert len(rows) == 6 and summarize(rows)['retries'] == 1
    # Pause/continue starts a new worker scope; the same task's rows still count. Tool and
    # application work stops at cap - REPLY_RESERVE (3), keeping the last attempts for the
    # career agent's reply; the refusal happens before the request body runs.
    def resumed():
        for _ in range(5):
            attempt(sent, usage=100, purpose='application')
    loop._metered(run, resumed)
    assert len(sent) == 6
    state = repo.get(run)
    assert state['status'] == 'needs_input' and state['agent_reason'] == 'application_budget'
    with usage_scope(ledger=ledger, task_id='task-A'):  # the career agent's reply
        for _ in range(3):
            attempt(sent, usage=100)
        with pytest.raises(TaskProviderBudgetExceeded):
            attempt(sent, usage=100)  # the 10th attempt overall: refused, never sent
    assert len(sent) == 9
    rows = task_rows(repo, 'task-A')  # a refusal rolls back its reservation: no row, nothing sent
    assert len(rows) == 9 and all(r.get('status') == 'completed' for r in rows)


def test_unknown_usage_is_charged_its_reservation_and_the_input_cap_refuses_before_sending(loop, monkeypatch):
    monkeypatch.setenv('JOB_AGENT_TASK_MAX_INPUT_TOKENS', '5000')
    repo = loop.repository
    sent = []
    with usage_scope(ledger=UsageLedger(repo.sessions), task_id='task-B'):
        attempt(sent, reservation=3000)            # provider reported no usage
        rows = task_rows(repo, 'task-B')
        assert summarize(rows)['budget_input_tokens'] == 3000 and summarize(rows)['unknown_usage_calls'] == 1
        with pytest.raises(TaskProviderBudgetExceeded):
            attempt(sent, reservation=3000)        # 3000 + 3000 > 5000: never sent
        with pytest.raises(TaskProviderBudgetExceeded):
            attempt(sent, reservation=None)        # an unmeasurable request is refused too
    assert len(sent) == 1
