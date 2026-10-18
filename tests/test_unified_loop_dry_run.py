"""Unified loop M6: restart dry-run on database copies built at the current schema head."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

from eregion.runtime.auto_apply.repository import ApplyRepository
from eregion.runtime.career.repository import TaskRepository
from eregion.runtime.sessions.events import SessionEvent, SessionEventType
from eregion.runtime.sessions.repository import SessionRepository
from eregion.runtime.sessions.state import SessionMessageDraft, SessionState, SessionStatus
from eregion.runtime.tools.messages import AgentMessage, NormalizedToolCall
from eregion.api.db import create_database, upgrade_database
from test_auto_apply import request
from test_multi_agent import make_plan, validate
from test_career_flow import career  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('unified_loop_dry_run', ROOT / 'tools/scripts/unified_loop_dry_run.py')
dry = importlib.util.module_from_spec(spec); spec.loader.exec_module(dry)


def _session(repo, session_id, status, **extra):
    state = repo.create(SessionState(session_id=session_id, allowed_tools=frozenset({'read'})),
                        SessionEvent(session_id=session_id, event_type=SessionEventType.SESSION_CREATED),
                        messages=[SessionMessageDraft(message=AgentMessage(
                            message_id=f'{session_id}-system', role='system', content='policy'))])
    if status == SessionStatus.ACTIVE and not extra:
        return state
    messages = extra.pop('messages', [])
    return repo.save_transition(SessionState.model_validate({**state.model_dump(), 'status': status, **extra}),
        SessionEvent(session_id=session_id, event_type=SessionEventType.SESSION_RESUMED),
        expected_version=state.version, messages=[SessionMessageDraft(message=m) for m in messages])


def build_source(path: Path) -> Path:
    url = f"sqlite:///{path.as_posix()}"
    upgrade_database(url)  # real migrations, not create_all
    db = create_database(url)
    try:
        tasks = TaskRepository(db.session_factory)
        for key, status in [('queued', 'queued'), ('running', 'running'), ('waiting', 'waiting'),
                            ('asking', 'needs_input'), ('done', 'completed'), ('paused', 'running')]:
            state, _ = tasks.create(f'goal {key}', key)
            tasks.change(state['task_id'], status=status, **({'pause_requested': True} if key == 'paused' else {}))
        applications = ApplyRepository(db.session_factory)
        for n, (status, mode) in enumerate([('running', 'hybrid'), ('submitting', 'hybrid'),
                                            ('submitted', 'hybrid'), ('awaiting_review', 'autofill')]):
            state, _ = applications.create(request(automation_mode=mode, idempotency_key=f'req-{n:04d}',
                                                   url=f'https://jobs.lever.co/example/job{n}'))
            applications.save(state, status=status)
        sessions = SessionRepository(db.session_factory)
        _session(sessions, 'parent', SessionStatus.ACTIVE)
        _session(sessions, 's-done', SessionStatus.COMPLETED, terminal_reason='completed')
        call = NormalizedToolCall(tool_call_id='c1', tool_name='read', arguments={'text': 'x'})
        assistant = AgentMessage(message_id='s-run-a1', role='assistant', tool_calls=[call])
        _session(sessions, 's-running', SessionStatus.RUNNING, messages=[assistant],
                 pending_assistant_message_id='s-run-a1', pending_tool_call_ids=['c1'])
        from eregion.runtime.coordination.repository import AgentTaskRepository
        agent_tasks = AgentTaskRepository(db.session_factory)
        plan = make_plan(); agent_tasks.create_plan(plan, validate(plan))
    finally:
        db.close()
    return path


@pytest.fixture
def source(tmp_path):
    return build_source(tmp_path / 'source.db')


def _clean(section):
    return not (section['missing'] or section['duplicate'] or section['unexpected'])


def test_restart_dry_run_covers_every_in_flight_object_and_keeps_history(source, tmp_path):
    first = dry.dry_run(source, tmp_path / 'run-1')
    second = dry.dry_run(source, tmp_path / 'run-2')
    assert first == second  # repeatable on fresh copies
    assert first['source_unchanged'] and first['history_unchanged'] and first['coverage_complete']
    assert first['schema_head'] and first['schema_head'].startswith('0039')  # task memory pilot schema
    assert first['career_task']['expected'] == 2               # queued + running; paused is not resumed
    app = first['application_run']
    assert app['resume']['expected'] == 1 and _clean(app['resume'])             # hybrid running only
    assert app['manual_submission_unknown']['expected'] == 1                      # never auto-resubmitted
    assert first['session']['evidence'].startswith('classification only')
    assert first['session']['expected'] == 1 and first['session']['pending_calls'] == {'s-running': ['c1']}
    assert first['agent_task']['evidence'].startswith('classification only')
    assert first['agent_task']['expected'] == 1 and not first['agent_task']['listing_truncated']
    if os.getenv('UNIFIED_LOOP_DRY_RUN_REPORT'):
        Path(os.environ['UNIFIED_LOOP_DRY_RUN_REPORT']).write_text(json.dumps(first, indent=2, sort_keys=True))


def test_an_owner_that_misses_an_object_fails_the_coverage_check(source, tmp_path, monkeypatch):
    # The application owner "forgets" its resumable run: coverage must report it.
    monkeypatch.setattr(ApplyRepository, 'recover', lambda self: [])
    report = dry.dry_run(source, tmp_path / 'missing')
    assert report['application_run']['resume']['missing'] and not report['coverage_complete']


def test_more_than_500_ready_agent_tasks_are_all_covered_and_truncation_fails(tmp_path):
    from eregion.runtime.coordination.repository import AgentTaskRepository
    path = tmp_path / 'many.db'
    upgrade_database(f"sqlite:///{path.as_posix()}")
    db = create_database(f"sqlite:///{path.as_posix()}")
    try:
        _session(SessionRepository(db.session_factory), 'parent', SessionStatus.ACTIVE)
        tasks = AgentTaskRepository(db.session_factory)
        for n in range(505):
            plan = make_plan(key=f'plan-{n}')
            tasks.create_plan(plan, validate(plan))
    finally:
        db.close()
    full = dry.dry_run(path, tmp_path / 'full')
    assert full['agent_task']['expected'] == 505 and _clean(full['agent_task']) and full['coverage_complete']
    truncated = dry.dry_run(path, tmp_path / 'truncated', agent_listing_limit=500)
    assert truncated['agent_task']['listing_truncated'] and not truncated['coverage_complete']


def test_pending_interview_input_and_feedback_are_covered(career, tmp_path):
    from test_remaining_interview_fifo import prepared
    c = career
    controller, identifier = prepared(c)
    mid = 'interview-msg-1'
    c.repository.append(c.state['task_id'], role='user', text='A1: Durable Python answer.', id=mid,
                        context={'interview_id': identifier})
    state = c.repository.get(c.state['task_id'])
    c.harness.interview_inputs.route(state, next(m for m in state['messages'] if m['id'] == mid))
    question = controller.interviews.get(identifier).current_question_id
    controller.answer_deferred(identifier, 'A0: earlier answer.', question_id=question,
                               idempotency_key='dry-run-a0', dispatch=lambda: None)
    source = tmp_path / 'career.db'
    report = dry.dry_run(source, tmp_path / 'interview')
    assert report['interview_inputs']['expected'] == 1 and _clean(report['interview_inputs'])
    assert report['interview_feedback']['expected'] == 1 and _clean(report['interview_feedback'])
    assert report['coverage_complete'] and report['source_unchanged']


def test_second_restart_on_the_same_copy_keeps_unknown_submissions_unknown(source, tmp_path):
    first = dry.dry_run(source, tmp_path / 'once')
    copy = tmp_path / 'once' / 'dry-run-copy.db'
    again = dry.dry_run(copy, tmp_path / 'twice')
    assert first['application_run']['manual_submission_unknown']['expected'] == 1
    assert again['history_unchanged'] and again['coverage_complete']
    assert again['application_run']['manual_submission_unknown']['expected'] == 0  # already unknown
    assert again['application_run']['transitions'] == {}       # no further state change on re-run


def test_the_user_database_is_refused(monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['dry', '--source', str(dry.USER_DATABASE), '--work', 'unused'])
    assert dry.main() == 2
