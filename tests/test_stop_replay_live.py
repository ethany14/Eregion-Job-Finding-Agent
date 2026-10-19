"""Explicit paid stop replay: does the real main model continue or stop a search sensibly?

Restores a saved live sample after every scored job was decided, removes its final
reply, and lets the main model choose the next step from the recorded facts. Work tools
(search, read, score) are recorded, not run: the first one ends the replay as
"continued". A scenario may change only recorded coverage facts, so the same saved
postings, decisions and resume support both "a further step is clearly worth it" and
"searching further is not worth it". Behaviour is checked, never exact wording.
"""
import json
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

WORK_TOOLS = {'search_jobs', 'screen_jobs', 'read_job', 'import_job', 'verify_and_score', 'verify_and_score_jobs',
              'score_job', 'score_jobs'}
MAX_MODEL_CALLS = 4
LIMITS = {'JOB_AGENT_JOB_API_FIRST': '1', 'JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS': '100', 'JOB_AGENT_TASK_MAX_INPUT_TOKENS': '1500000',
          'JOB_AGENT_LINKEDIN_MAX_CALLS': '100', 'JOB_AGENT_ANALYSIS_WARMUP': '0'}


def scenario(state, name):
    """Recorded coverage facts for each scenario (the saved jobs and decisions are unchanged)."""
    cache = state['search_request_cache']
    log = (state.get('linkedin_mcp_state') or {}).get('log') or []
    first = next(r for r in log if r.get('tool') == 'search_jobs')
    for entry in cache['requests'].values():
        linked = entry['result'].get('linkedin_mcp') or {}
        # Backfill what the current code records for each search (the run predates it).
        linked.setdefault('request', {'query': first['arguments']['keywords'], 'pages': first['arguments']['max_pages'],
                                      'cards': sum(1 for j in entry['result'].get('jobs', []) if 'linkedin.com' in j.get('url', ''))})
        if name == 'exhausted':
            # The provider reports no more results than were already seen.
            linked['total'] = linked['request']['cards']
    if name == 'budget_low':
        # Only a few LinkedIn calls remain for this task.
        source = state['linkedin_mcp_state']
        source['calls_used'] = max(source.get('calls_used', 0), source.get('call_limit', 100) - 3)
    return state


@pytest.mark.skipif(os.getenv('JOB_AGENT_LIVE_STOP_REPLAY') != '1', reason='Explicit paid live opt-in')
def test_stop_replay(tmp_path, monkeypatch):
    from eregion.api.db import create_database
    from eregion.runtime.career.repository import TaskRepository
    from eregion.runtime.career.service import CareerService
    from eregion.runtime.career.harness import CareerHarness
    from eregion.runtime.provider_usage import UsageLedger
    from eregion.runtime.tasks.loop import TaskModel
    from test_career_flow import (JobWorkspaceRepository, CareerEvidenceRepository, PackRepository,
                                  ApplicationLoop, ApplyRepository, upgrade_database)

    source = Path(os.environ['REPLAY_SOURCE'])
    name = os.environ['STOP_SCENARIO']
    output = Path(os.environ['OPT_LIVE_OUTPUT'])
    assert not output.exists(), 'Never overwrite or automatically repeat a live sample'
    output.mkdir(parents=True)
    for key, value in LIMITS.items():
        monkeypatch.setenv(key, value)
    database = tmp_path / 'career.db'
    with sqlite3.connect(f'file:{(source / "career.sqlite3").as_posix()}?mode=ro', uri=True) as original, \
            sqlite3.connect(database) as copy:
        original.backup(copy)
    url = f'sqlite:///{database.as_posix()}'
    upgrade_database(url)
    db = create_database(url)
    runtime = SimpleNamespace(database=db, workspace=JobWorkspaceRepository(db.session_factory),
                              evidence=CareerEvidenceRepository(db.session_factory),
                              packs=PackRepository(db.session_factory), pack_workflow=None, mock_interviewer=None)
    repository = TaskRepository(db.session_factory)
    applications = ApplicationLoop(ApplyRepository(db.session_factory), browser_factory=lambda url: None)
    harness = CareerHarness(runtime, applications, repository)
    # Same scorer configuration as the live sample, so its saved scores stay current.
    from dataclasses import replace
    from eregion.api.services.fit_analysis_service import FitAnalysisService
    from eregion.domain.jev_fit.settings import Settings
    harness.analysis._fit_service = FitAnalysisService(jev_enabled=True,
        jev_config=replace(Settings.load(cache_path=tmp_path / 'fit.sqlite3'), resolve_unknowns=False))
    task_id = next(s['task_id'] for s in repository.list())
    state = scenario(repository.get(task_id), name)
    scored = set(state.get('scored_job_evidence') or {})
    decided = set(state.get('job_decisions') or {}) | {r['application_id'] for r in state.get('recommendations', [])}
    assert scored and scored <= decided, 'The sample must have every scored job decided before its final reply.'
    # Remove only the final reply and its finish step; keep the search trajectory.
    history = [h for h in state['history'] if h['tool'] != 'finish_task']
    messages = [m for m in state['messages'] if m.get('role') == 'user']
    if os.getenv('STOP_FOLLOW_UP'):
        # The recorded reply stays; the user then asks to continue (a new user turn).
        history = state['history']
        messages = [*state['messages'], {'role': 'user', 'text': os.environ['STOP_FOLLOW_UP'], 'id': 'follow-up-1'}]
    repository.change(task_id, search_request_cache=state['search_request_cache'], history=history, messages=messages,
                      linkedin_mcp_state=state['linkedin_mcp_state'],
                      status='queued', error=None, pause_requested=False, progress_guard={}, loop_runtime=None,
                      input_request=None, wait=None, stop_reason=None,
                      chat_revision=state.get('chat_revision', 0) + (1 if os.getenv('STOP_FOLLOW_UP') else 0))

    attempted, invocations = [], []
    original_execute = harness.execute

    def execute(task, tool, args, *, call_id):
        if tool in WORK_TOOLS:
            attempted.append({'tool': tool, 'arguments': args})
            repository.change(task, pause_requested=True)
            return {'replay_not_executed': True, 'note': 'Recorded by the stop replay; not executed.'}
        return original_execute(task, tool, args, call_id=call_id)
    harness.execute = execute

    class Counted:
        def __init__(self):
            self.model = TaskModel()

        def invoke(self, messages, tools):
            if len(invocations) >= MAX_MODEL_CALLS:
                repository.change(task_id, pause_requested=True)
                raise RuntimeError('Replay model-call cap reached.')
            invocations.append(time.monotonic())
            if os.getenv('REPLAY_REASONING'):
                # Only the main model's continue/stop call; scores and qualification caches
                # keep their fingerprint (an env setting would mark them stale).
                return self.model.model.bind_tools(tools).invoke(
                    messages, timeout=self.model.timeout, max_tokens=4000, reasoning_effort=os.environ['REPLAY_REASONING'])
            return self.model.invoke(messages, tools)

    ledger = UsageLedger(db.session_factory)
    before = ledger.summary(task_id=task_id)
    service = CareerService(runtime, applications, harness=harness, model_factory=Counted, start_alert_scheduler=False)
    started = time.monotonic()
    try:
        service.loop.schedule(task_id)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            current = repository.get(task_id)
            if task_id not in service.loop.running and current['status'] != 'queued':
                break
            time.sleep(.5)
    finally:
        service.close()
    final = repository.get(task_id)
    after = ledger.summary(task_id=task_id)
    outcome = 'continued' if attempted else 'finished' if final['status'] == 'completed' else 'undecided'
    result = {'source': str(source), 'scenario': name, 'reasoning': os.getenv('REPLAY_REASONING') or 'default',
              'outcome': outcome, 'next_step': attempted[:1],
              'stop_reason': final.get('stop_reason'), 'model_calls': len(invocations),
              'tools': [h['tool'] for h in final.get('history', [])[len(history):]],
              'final': next((m['text'] for m in reversed(final['messages']) if m['role'] == 'assistant'), None),
              'usage_delta': {k: after.get(k, 0) - before.get(k, 0) for k in
                              ('calls', 'input_tokens', 'output_tokens', 'unknown_usage_calls')},
              'seconds': round(time.monotonic() - started, 2), 'error': final.get('error'), 'cost_usd': None}
    (output / 'stop-replay.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    db.close()
    assert outcome != 'undecided', result
