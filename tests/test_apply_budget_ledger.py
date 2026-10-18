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
    rows = task_rows(repo, 'task-A')  # a refusal releases its reservation: an audit row, nothing sent
    # Both refusals are audit rows (nothing sent); the 9 sent attempts are all completed.
    assert [r.get('status') for r in rows].count('completed') == 9
    assert [r.get('status') for r in rows].count('refused_before_send') == 2 and summarize(rows)['calls'] == 9


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


def test_an_experiment_budget_alone_still_reserves_every_requests_input(monkeypatch):
    # Model replay: with only JOB_AGENT_EXPERIMENT_* set, no reservation was computed and the
    # experiment ledger refused the first request as unknown_input_size.
    from langchain_core.messages import HumanMessage
    from eregion.domain.model import ProviderCompatibleChatOpenAI
    monkeypatch.delenv('JOB_AGENT_TASK_MAX_INPUT_TOKENS', raising=False)
    model = ProviderCompatibleChatOpenAI(model='fixture', api_key='fixture-key')
    monkeypatch.delenv('JOB_AGENT_EXPERIMENT_ID', raising=False)
    assert model._budget_sizes([HumanMessage('hello')], None, {}) == {}
    monkeypatch.setenv('JOB_AGENT_EXPERIMENT_ID', 'replay')
    sizes = model._budget_sizes([HumanMessage('hello')], None, {})
    assert sizes['input_token_reservation'] > len('hello')


def test_experiment_refusal_releases_reservation_and_keeps_career_reply_budget(loop, monkeypatch, tmp_path):
    from eregion.runtime.provider_usage import experiment_summary
    path=tmp_path/'refused-experiment.sqlite3'
    monkeypatch.setenv('JOB_AGENT_EXPERIMENT_ID','refused-reply-test')
    monkeypatch.setenv('JOB_AGENT_EXPERIMENT_LEDGER',str(path))
    monkeypatch.setenv('JOB_AGENT_EXPERIMENT_MAX_PROVIDER_ATTEMPTS','9')
    monkeypatch.setenv('JOB_AGENT_EXPERIMENT_MAX_INPUT_TOKENS','10000')
    monkeypatch.setenv('JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS','9')
    monkeypatch.setenv('JOB_AGENT_TASK_MAX_INPUT_TOKENS','10000')
    ledger=UsageLedger(loop.repository.sessions);sent=[]
    with usage_scope(ledger=ledger,task_id='reply-task'):
        for _ in range(6):attempt(sent,usage=100,purpose='application')
        # Each refusal is persisted in the experiment, but sends nothing.
        for _ in range(2):
            with pytest.raises(TaskProviderBudgetExceeded):
                attempt(sent,usage=100,purpose='application')
        total=experiment_summary(path,'refused-reply-test')
        assert total['calls']==6 and total['refused_before_send']==2
        assert total['budget_input_tokens']==600 and total['usage_complete']
        for _ in range(3):attempt(sent,usage=100,purpose='agent')
        with pytest.raises(TaskProviderBudgetExceeded):attempt(sent,usage=100)
    total=experiment_summary(path,'refused-reply-test')
    assert len(sent)==9 and total['calls']==9 and total['completed']==9
    assert total['input_tokens']==900 and total['unknown_usage_calls']==0
    # Two task refusals plus the experiment cap's own refusal of the 10th attempt.
    assert total['refused_before_send']==3


def test_refusal_is_distinct_from_sent_failed_and_pending_attempts():
    total=summarize([
        {'status':'refused_before_send','reserved_input_tokens':900,'input_tokens':0,'output_tokens':0},
        {'status':'failed','reserved_input_tokens':100},
        {'status':'pending','reserved_input_tokens':200},
        {'status':'completed','input_tokens':30,'output_tokens':5},
    ])
    assert total['refused_before_send']==1 and total['calls']==3
    assert total['budget_input_tokens']==330 and total['unknown_usage_calls']==2
    assert total['failed']==1 and total['pending']==1 and total['completed']==1


def test_experiment_cap_refusal_is_recorded_without_sending(monkeypatch, tmp_path):
    from eregion.runtime.provider_usage import experiment_summary
    path=tmp_path/'zero-cap.sqlite3'
    monkeypatch.setenv('JOB_AGENT_EXPERIMENT_ID','zero-cap')
    monkeypatch.setenv('JOB_AGENT_EXPERIMENT_LEDGER',str(path))
    monkeypatch.setenv('JOB_AGENT_EXPERIMENT_MAX_PROVIDER_ATTEMPTS','0')
    monkeypatch.setenv('JOB_AGENT_EXPERIMENT_MAX_INPUT_TOKENS','0')
    sent=[]
    with pytest.raises(TaskProviderBudgetExceeded):
        attempt(sent, usage=100, purpose='jev:job_screening')
    total=experiment_summary(path,'zero-cap')
    # A hidden call refused by a zero-call experiment is visible in the ledger, never sent.
    assert sent==[] and total['refused_before_send']==1 and total['calls']==0 and total['budget_input_tokens']==0


def test_task_cap_refusal_is_recorded_in_the_task_ledger(loop, monkeypatch):
    monkeypatch.setenv('JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS','1')
    ledger=UsageLedger(loop.repository.sessions);sent=[]
    with usage_scope(ledger=ledger,task_id='capped-task'):
        attempt(sent,usage=100)
        with pytest.raises(TaskProviderBudgetExceeded):attempt(sent,usage=100)
    rows=task_rows(loop.repository,'capped-task')
    assert len(sent)==1 and [r['status'] for r in rows].count('refused_before_send')==1
    refused=next(r for r in rows if r['status']=='refused_before_send')
    assert refused['refusal_reason']=='provider_attempts' and refused['input_tokens']==0
    total=summarize(rows)
    assert total['calls']==1 and total['refused_before_send']==1
