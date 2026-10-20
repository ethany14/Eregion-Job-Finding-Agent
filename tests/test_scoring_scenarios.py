"""F2: "saved scores recover" and "unscored budget" are separate scenarios with asserted premises.

Replay B tested the budget planner on jobs another conversation had already scored, so the
planner never ran. Each scenario here first asserts its premise (scenario_state) and then that
its target path actually ran. Offline: fake fit scorer or Jev over a mock transport, real
harness, real usage ledger."""
import asyncio
import sys
import threading
from pathlib import Path

import httpx
import pytest

from eregion.runtime.provider_usage import (ReplyBudgetReserved, TaskProviderBudgetExceeded, UsageLedger,
                                            provider_call, usage_scope)
from test_career_flow import career, execute  # noqa: F401  (fixture)
from test_qualified_batch import GOAL, posting, prepare

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools' / 'scripts'))
from scenario_state import scoring_state  # noqa: E402


def spy_dispatch(c, monkeypatch):
    """Jobs actually sent to the fit scorer (a saved analysis is never sent)."""
    service = c.harness.analysis._fit_service
    original, sent = service.analyze_many, []
    async def analyze_many(**kwargs):
        sent.append([j['id'] for j in kwargs['jobs']])
        return await original(**kwargs)
    monkeypatch.setattr(service, 'analyze_many', analyze_many)
    return sent


def spy_planner(c, monkeypatch):
    original, plans = c.harness.affordable_scoring, []
    def plan(task_id, identifiers, *, cached=(), observed=None):
        result = original(task_id, identifiers, cached=cached, observed=observed)
        plans.append({'asked': list(identifiers), 'free': list(cached), 'now': result[0], 'deferred': result[1], 'observed': observed})
        return result
    monkeypatch.setattr(c.harness, 'affordable_scoring', plan)
    return plans


def new_conversation(c, ids, key):
    fresh, _ = c.repository.create(GOAL, key)
    for application_id in ids:
        c.repository.link(fresh['task_id'], 'jobs', application_id)
    c.state = c.repository.get(fresh['task_id'])
    return c.state


def test_saved_scores_recover_in_a_new_conversation_without_paying(career, monkeypatch):
    c = career
    prepare(c, monkeypatch, [posting(i) for i in range(4)])
    ids = [j['application_id'] for j in execute(c, 'verify_and_score_jobs', urls=[posting(i)['url'] for i in range(4)])['jobs']]
    fresh = new_conversation(c, ids, 'recover-saved-scores')
    premise = scoring_state(c.harness, fresh['task_id'], ids)
    # Premise: persisted and reusable everywhere, but this task's index is empty and nothing is recommended.
    assert premise['counts'] == {'persisted_score': 4, 'reusable': 4, 'task_index': 0, 'recommended': 0}
    sent, plans = spy_dispatch(c, monkeypatch), spy_planner(c, monkeypatch)
    # Main route (the fixture scorer is not Jev): 7 attempts leave no room for one paid job (5).
    monkeypatch.setenv('JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS', '7')
    ledger = UsageLedger(c.repository.sessions)
    with usage_scope(ledger=ledger, task_id=fresh['task_id']):
        result = execute(c, 'score_jobs', application_ids=ids)
    assert len(result['jobs']) == 4 and 'unscored_for_budget' not in result
    assert plans and sorted(plans[0]['free']) == sorted(ids) and plans[0]['deferred'] == []  # target path ran
    assert sent == [] and ledger.summary(task_id=fresh['task_id'])['calls'] == 0  # nothing paid again
    after = scoring_state(c.harness, fresh['task_id'], ids)
    # The task index is rebuilt from the saved results; a score is still not a recommendation.
    assert after['counts']['task_index'] == 4 and after['counts']['recommended'] == 0


def test_a_changed_resume_makes_saved_scores_unusable_so_the_budget_applies(career, monkeypatch):
    c = career
    prepare(c, monkeypatch, [posting(i) for i in range(3)])
    ids = [j['application_id'] for j in execute(c, 'verify_and_score_jobs', urls=[posting(i)['url'] for i in range(3)])['jobs']]
    fresh = new_conversation(c, ids, 'changed-resume')
    saved, text = c.harness.candidate()
    c.harness.candidate = lambda: (saved, text + '\nNew experience: Spark pipelines.')
    premise = scoring_state(c.harness, fresh['task_id'], ids)
    assert premise['counts']['persisted_score'] == 3 and premise['counts']['reusable'] == 0
    monkeypatch.setenv('JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS', '7')
    with usage_scope(ledger=UsageLedger(c.repository.sessions), task_id=fresh['task_id']):
        with pytest.raises(ReplyBudgetReserved):
            execute(c, 'score_jobs', application_ids=ids)


def test_unscored_budget_plans_from_the_ledger_and_a_hard_stop_resumes_without_rescoring(career, monkeypatch):
    c = career
    jobs = [posting(i) for i in range(6)]
    prepare(c, monkeypatch, jobs)
    service = c.harness.analysis._fit_service
    original, sent = service.analyze_many, []
    # Variable stages: the second job costs 8 requests, more than the main-route estimate of 5 per job.
    # Each request is a real ledger attempt, so the hard cap governs, not the estimate.
    async def staged(**kwargs):
        sent.append([j['id'] for j in kwargs['jobs']])
        for job, cost in zip(kwargs['jobs'], [5, 8, 5, 5, 5, 5]):
            for _ in range(cost):
                with usage_scope(purpose='fit:stage'):
                    with provider_call('fit-fixture', input_token_reservation=100) as details:
                        details.update(input_tokens=100, output_tokens=5)
        return await original(**kwargs)
    monkeypatch.setattr(service, 'analyze_many', staged)
    plans = spy_planner(c, monkeypatch)
    monkeypatch.setenv('JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS', '18')  # 18 - reply reserve 3 = 15 -> 3 planned
    ledger = UsageLedger(c.repository.sessions)
    with usage_scope(ledger=ledger, task_id=c.state['task_id']):
        stopped = execute(c, 'verify_and_score_jobs', urls=[j['url'] for j in jobs])
    ids = list(c.repository.get(c.state['task_id'])['jobs'])
    assert len(plans) == 1 and len(plans[0]['now']) == 3 and len(plans[0]['deferred']) == 3  # the planner ran
    # The stop at the reply reserve is a result, not a lost tool call: nothing scored, all six listed.
    assert stopped['jobs'] == [] and len(stopped['unscored_for_budget']) == 6 and len(stopped['interrupted_chunk']) == 3
    assert 'cached' in stopped['budget_note'] and 'Not recommendations' in stopped['budget_note']
    premise = scoring_state(c.harness, c.state['task_id'], ids)
    assert len(ids) == 6 and premise['counts']['persisted_score'] == 0  # imports kept, nothing half-scored
    usage = ledger.summary(task_id=c.state['task_id'])
    # 5 + 8 + 2 = 15 sent; the 16th (tool work inside the reply reserve) was refused before sending.
    assert usage['calls'] == 15 and usage['refused_before_send'] == 1 and usage['input_tokens'] == 1500
    monkeypatch.setenv('JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS', '400')
    with usage_scope(ledger=ledger, task_id=c.state['task_id']):
        result = execute(c, 'verify_and_score_jobs', urls=[j['url'] for j in jobs])
        assert len(result['jobs']) == 6 and 'unscored_for_budget' not in result
        again = execute(c, 'score_jobs', application_ids=ids)
    # The interrupted chunk saved nothing; the retry scored each job once (chunks 4 + 2); the repeat sent nothing.
    retried = [a for chunk in sent[1:] for a in chunk]
    assert [len(g) for g in sent] == [3, 4, 2] and sorted(retried) == sorted(ids) and len(set(retried)) == 6
    assert len(again['jobs']) == 6


def test_an_interrupted_jev_batch_pays_each_decision_once(tmp_path, monkeypatch):
    from eregion.api.db import create_database
    from test_jev_fit_production import JevEndpoint, JevProductionTests
    case = JevProductionTests()
    def run(cache, endpoint, cap=None):
        from dataclasses import replace
        case.config = replace(case.config, cache_path=tmp_path / cache)
        case.endpoint = endpoint
        jobs = [{'id': str(i), 'source_version': str(i), 'job_description': case.jd + f'Required: Skill{i}\n'} for i in range(3)]
        return asyncio.run(case.service().analyze_many(resume_text=case.resume, jobs=jobs))
    from eregion.domain.jev_fit.settings import Settings
    case.config = Settings(key='test-only', cache_path=tmp_path / 'unused.sqlite3')
    full = JevEndpoint()
    run('full.sqlite3', full)
    db = create_database('sqlite:///' + (tmp_path / 'ledger.sqlite3').as_posix(), create_schema_for_tests=True)
    try:
        ledger = UsageLedger(db.session_factory)
        first, second = JevEndpoint(), JevEndpoint()
        # Stop midway: at most 6 attempts disables the reply reserve, so the cap itself is the stop.
        assert len(full.requests) >= 2
        monkeypatch.setenv('JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS', str(min(6, len(full.requests) - 1)))
        with usage_scope(ledger=ledger, task_id='interrupted'):
            with pytest.raises(TaskProviderBudgetExceeded):
                run('resume.sqlite3', first)
        monkeypatch.setenv('JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS', '400')
        with usage_scope(ledger=ledger, task_id='interrupted'):
            results = run('resume.sqlite3', second)
        assert set(results) == {'0', '1', '2'} and 0 < len(first.requests) < len(full.requests)
        # Decisions paid before the stop come from the cache; together the two runs ask what one full run asks.
        asked = lambda endpoint: sorted(key for payload in endpoint.requests for key in payload['questions'])
        assert sorted(asked(first) + asked(second)) == asked(full)
        usage = ledger.summary(task_id='interrupted')
        assert usage['calls'] == len(first.requests) + len(second.requests) and usage['refused_before_send'] >= 1
    finally:
        db.close()


def test_refusals_never_overwrite_a_sent_attempt_and_concurrent_refusals_stay_distinct(tmp_path):
    from eregion.api.db import create_database
    from eregion.runtime.provider_usage import summarize
    db = create_database('sqlite:///' + (tmp_path / 'u.sqlite3').as_posix(), create_schema_for_tests=True)
    try:
        ledger = UsageLedger(db.session_factory)
        scope, limits = {'task_id': 't'}, {'provider_attempts': 1}
        ledger.start('sent', scope, {'purpose': 'agent', 'status': 'pending', 'reserved_input_tokens': 500}, limits=limits)
        with pytest.raises(TaskProviderBudgetExceeded):  # same id, refused: the pending sent row is untouched
            ledger.start('sent', scope, {'purpose': 'agent'}, limits=limits)
        errors = []
        def refuse(i):
            try:
                ledger.start(f'c{i}', scope, {'purpose': 'agent'}, limits=limits)
            except TaskProviderBudgetExceeded:
                pass
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=refuse, args=(i,)) for i in range(6)]
        [t.start() for t in threads]; [t.join() for t in threads]
        from sqlalchemy import select
        import json
        from eregion.runtime.provider_usage import ProviderUsageRow
        with db.session_factory() as session:
            rows = {r.call_id: json.loads(r.details_json) for r in session.scalars(select(ProviderUsageRow))}
        assert not errors and rows['sent']['status'] == 'pending' and rows['sent']['reserved_input_tokens'] == 500
        refused = [r for r in rows.values() if r['status'] == 'refused_before_send']
        assert len(refused) == 7 and all(r['input_tokens'] == 0 for r in refused)
        total = summarize(list(rows.values()))
        # The sent attempt with unknown usage is charged its reservation, never zero.
        assert total['calls'] == 1 and total['refused_before_send'] == 7 and total['budget_input_tokens'] == 500
        assert total['unknown_usage_calls'] == 1
    finally:
        db.close()


def test_finished_chunks_persist_and_measured_cost_sizes_the_next_chunk(career, monkeypatch):
    """Layer-2 B: one 12-job batch outran the estimate and saved nothing. Now a small first chunk is
    saved, its measured cost per job sizes the next chunk, and the reply reserve is never reached."""
    c = career
    jobs = [posting(i) for i in range(10)]
    prepare(c, monkeypatch, jobs)
    monkeypatch.setattr(c.harness.analysis, 'uses_jev', lambda: True)
    service = c.harness.analysis._fit_service
    original, sent = service.analyze_many, []
    async def jev_like(**kwargs):  # 8 shared requests per batch + 3 per job (the estimate says 2)
        sent.append([j['id'] for j in kwargs['jobs']])
        for _ in range(8 + 3 * len(kwargs['jobs'])):
            with usage_scope(purpose='jev:fit:stage'), provider_call('jev-fixture', input_token_reservation=100) as details:
                details.update(input_tokens=100, output_tokens=5)
        return await original(**kwargs)
    monkeypatch.setattr(service, 'analyze_many', jev_like)
    monkeypatch.setenv('JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS', '40')  # room 37: the estimate plans (37-8)//2 = 10
    ledger = UsageLedger(c.repository.sessions)
    with usage_scope(ledger=ledger, task_id=c.state['task_id']):
        result = execute(c, 'verify_and_score_jobs', urls=[j['url'] for j in jobs])
    # First chunk 4 (8+12=20 requests), measured 3/job -> room 17 fits (17-8)//3 = 3 -> 8+9 = 17. No refusal.
    assert [len(g) for g in sent] == [4, 3] and len(result['jobs']) == 7 and len(result['unscored_for_budget']) == 3
    usage = ledger.summary(task_id=c.state['task_id'])
    assert usage['calls'] == 37 and usage['refused_before_send'] == 0 and 'interrupted_chunk' not in result
    state = scoring_state(c.harness, c.state['task_id'], list(c.repository.get(c.state['task_id'])['jobs']))
    assert state['counts']['persisted_score'] == 7 and state['counts']['recommended'] == 0


def test_score_results_are_compact_with_every_jobs_status_and_survive_archiving(career, monkeypatch):
    """Layer-2 run 2: ten full detail views (384k characters) were archived and the unscored list sat behind a
    truncated preview. score_jobs now returns what a decision needs; the full report stays one inspect_job away."""
    from eregion.runtime.request_context import ContextArchive
    import json
    c = career
    jobs = [posting(i) for i in range(6)]
    prepare(c, monkeypatch, jobs)
    monkeypatch.setattr(c.harness.analysis, 'uses_jev', lambda: True)
    original = c.harness.analysis._fit_service.analyze_many
    async def jev_like(**kwargs):
        for _ in range(8 + 2 * len(kwargs['jobs'])):
            with usage_scope(purpose='jev:fit:stage'), provider_call('jev-fixture', input_token_reservation=10) as d:
                d.update(input_tokens=10, output_tokens=1)
        return await original(**kwargs)
    monkeypatch.setattr(c.harness.analysis._fit_service, 'analyze_many', jev_like)
    monkeypatch.setenv('JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS', '23')  # room 20: chunk of 4 costs 16, 4 left -> 2 unscored
    with usage_scope(ledger=UsageLedger(c.repository.sessions), task_id=c.state['task_id']):
        result = execute(c, 'verify_and_score_jobs', urls=[j['url'] for j in jobs])
    scored, unscored = result['jobs'], result.get('unscored') or []
    assert scored and unscored and result['scoring']['scored'] == len(scored) == 4
    assert result['scoring']['unscored_for_budget'] == len(unscored) == 2
    # Guidance shared by every scored job is stated once beside the list, not repeated per job.
    assert 'inspect_job' in result['detail'] and 'Scoring is complete' in result['decision_brief_note']
    for entry in scored:
        assert entry['scoring_status'] == 'scored' and 'detail' not in entry and 'note' not in entry['decision_brief']
        assert 'match_report' not in entry and 'description' not in entry and entry['match_score'] is not None
    assert all(u['scoring_status'] == 'unscored_for_budget' and 'Not recommendations' in u['reason'] for u in unscored)
    # The full report is still one call away.
    full = execute(c, 'inspect_job', application_id=scored[0]['application_id'])
    assert full.get('match_report') or full.get('match_overview')
    # Archived or not, the scoring state stays visible.
    summary = ContextArchive().tool_result(json.loads(json.dumps(result, default=str)))
    assert {'scoring', 'unscored', 'detail', 'decision_brief_note'} <= set(summary)
    assert all(j.get('scoring_status') == 'scored' for j in summary['jobs'])
