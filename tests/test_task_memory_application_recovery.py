"""E2: task memory and auto-apply recovery, on a temp DB with the real CareerHarness,
ApplicationLoop (fake browser) and TaskMemoryService. No model calls, no network.

Task memory is recovery context only. The auto-apply run's persisted state stays the authority
for bindings, decisions, failures, documents and budget; these tests check that the summary
reads it live (application_id -> linked run_id), never caches executable state, and keeps
blockers and the user's latest words ahead of bulk context."""
import json
import pytest

from eregion.runtime.auto_apply.decisions import active_decision
from eregion.runtime.auto_apply.autofill import desired_answer
from eregion.runtime.auto_apply.repository import ApplyRepository
from eregion.runtime.task_memory.service import TaskMemoryService
from eregion.runtime.task_memory.types import TaskMemoryConfig
from test_career_flow import career, prepare, approve, wait
from test_task_memory_end_to_end import install


def start(c, app, text='Apply to this job and stop at review.'):
    """A new conversation (stamped with the task-memory owner) about one application."""
    state, _ = c.repository.create(text, f'task-{text[:12]}', context={'application_id': app})
    c.state = state
    return state


def run_entries(c, state):
    memory = c.harness.observe(c.repository.get(state['task_id'])).get('task_memory') or {}
    return memory, [e for e in memory.get('entries', []) if e['kind'] == 'application_run']


def applied(c, job, pack):
    from test_career_flow import execute
    run = execute(c, 'start_application', application_id=job['application_id'], pack_id=pack['pack_id'])
    return wait(c.applications, run['run_id'])


@pytest.fixture
def setup(career):
    c = career
    job, pack = prepare(c); approve(c, pack)
    app = job['application_id']
    service = install(c, app)
    state = start(c, app)
    run = applied(c, job, pack)
    return c, job, pack, app, service, state, run


def test_same_application_restart_keeps_the_run_its_decisions_failures_and_documents(setup):
    c, job, pack, app, service, state, run = setup
    # Persisted execution facts (what a paused run carries), written through the run's own repository.
    saved = c.applications.repository.save(c.applications.repository.get(run['run_id']),
        field_decisions={'text:x:1': {'field': {}, 'mode': 'leave_empty', 'value': None, 'sources': {}, 'original_answer': None, 'reason': 'r'}},
        autofill_failures={'text:y:1': {'signature': 's', 'code': 'value_not_retained'}},
        document_uploads={'resume': {'fingerprint': 'f', 'filename': 'resume.pdf', 'status': 'verified', 'before': 0, 'after': 1,
                                     'material_ref': {'pack_id': pack['pack_id'], 'version': 1}}})
    # Restart: new repositories and a new memory service over the same durable DB.
    c.applications.repository = ApplyRepository(c.runtime.database.session_factory)
    install(c, app)
    memory, runs = run_entries(c, state)
    assert [r['source'] for r in runs] == [f"run:{run['run_id']}"]
    entry = runs[0]
    assert entry['current'] and entry['version'] == saved['version'] and entry['status'] == 'awaiting_review'
    assert entry['owned_by_this_conversation'] is True and 'ownership_note' not in entry
    assert entry['saved_decisions']['by_mode'] == {'leave_empty': 1} and entry['failed_fields'] == 1
    assert entry['documents']['resume']['status'] == 'verified'
    assert 'observe the live page' in entry['browser'] and 'Never submit' in entry['next']
    assert 'text:x:1' not in json.dumps(memory)  # no element ids or field keys in the summary
    again = applied(c, job, pack)
    assert again['run_id'] == run['run_id']  # continuing reuses the run, never a second one


def test_a_new_conversation_finds_the_existing_run_and_does_not_create_another(setup):
    c, job, pack, app, service, state, run = setup
    fresh = start(c, app, 'Where is my application?')
    memory, runs = run_entries(c, fresh)
    assert [r['source'] for r in runs] == [f"run:{run['run_id']}"] and runs[0]['current']
    assert runs[0]['owned_by_this_conversation'] is False and 'another conversation' in runs[0]['ownership_note']
    # Starting it again from the new conversation is refused without touching the run.
    before = c.applications.repository.get(run['run_id'])
    with pytest.raises(ValueError, match='already owned by another conversation'):
        applied(c, job, pack)
    assert c.applications.repository.get(run['run_id'])['version'] == before['version']
    from sqlalchemy import text
    with c.runtime.database.session_factory() as db:
        assert db.execute(text('select count(*) from auto_applications')).scalar() == 1


def test_runs_and_quotes_of_another_application_never_appear(setup):
    c, job, pack, app, service, state, run = setup
    other = c.runtime.workspace.save_workspace(cleaned_job_description='Go required', source_url='https://example.com/other',
                                               title='Other role').application.application_id
    from test_career_flow import execute
    foreign = execute(c, 'open_application_url', url='https://jobs.lever.co/example/other456')['run_id']
    wait(c.applications, foreign)
    c.repository.link(state['task_id'], 'jobs', other)
    c.repository.change(state['task_id'], lambda s: {'application_links': {**s['application_links'],
        foreign: {'application_id': other, 'materials': {}}}})
    c.repository.append(state['task_id'], role='user', text='For the other role, use my Go projects.',
                        context={'application_id': other})
    c.repository.append(state['task_id'], role='user', text='For this role keep my Python title.',
                        context={'application_id': app})
    memory, runs = run_entries(c, c.repository.get(state['task_id']))
    assert [r['source'] for r in runs] == [f"run:{run['run_id']}"]
    dumped = json.dumps(memory)
    assert foreign not in dumped and 'Go projects' not in dumped and 'Python title' in dumped


def test_a_user_correction_wins_and_retires_the_old_decision(setup):
    c, job, pack, app, service, state, run = setup
    field = {'key': 'text:city:1', 'label': 'City', 'kind': 'text', 'group': '', 'value': '', 'options': []}
    from eregion.runtime.auto_apply.decisions import signature
    data = {'profile': {'city': 'Ithaca, NY'}, 'answers': {'text:city:1': 'Ithaca'},
            'field_decisions': {'text:city:1': {'field': signature(field), 'mode': 'format', 'value': 'Ithaca',
                'sources': {'answer:text:city:1': 'Ithaca'}, 'original_answer': 'Ithaca', 'reason': 'part'}}}
    assert active_decision(field, data)
    data['answers']['text:city:1'] = 'Brooklyn'  # the user corrects the answer
    assert active_decision(field, data) is None and desired_answer(field, data) == 'Brooklyn'
    c.repository.append(state['task_id'], role='user', text='Actually I moved: use Brooklyn.', context={'application_id': app})
    memory, _ = run_entries(c, c.repository.get(state['task_id']))
    quotes = [e['text'] for e in memory['entries'] if e.get('authority') == 'historical_user_quote']
    assert quotes[0] == 'Actually I moved: use Brooklyn.'  # newest user words first


def test_changed_materials_are_owed_not_assumed_uploaded_and_not_regenerated(setup):
    c, job, pack, app, service, state, run = setup
    packs_before = len(c.runtime.packs.list_for_application(app))
    current = c.applications.repository.get(run['run_id'])
    c.applications.repository.save(current, document_uploads={'resume': {'fingerprint': 'old', 'filename': 'resume.pdf',
        'status': 'verified', 'before': 0, 'after': 1}})
    changed = c.applications.repository.replace_documents(c.applications.repository.get(run['run_id']), b'%PDF new', None)
    assert changed['document_refresh']['resume'] is True
    assert changed['document_uploads']['resume']['fingerprint'] == 'old'  # the old upload stays a record of the old version
    c.applications.cancel(run['run_id'], c.applications.repository.get(run['run_id'])['version'])
    memory, runs = run_entries(c, state)
    assert runs[0]['documents_owed'] == ['resume']
    assert len(c.runtime.packs.list_for_application(app)) == packs_before


@pytest.mark.parametrize('status,reason,expected', [
    ('cancelled', None, 'Resume only if the user asks'),
    ('needs_input', 'application_budget', 'the budget is not reset'),
    ('awaiting_review', None, 'Never submit from memory'),
])
def test_stopped_runs_are_reported_not_resumed_or_reset(setup, status, reason, expected):
    c, job, pack, app, service, state, run = setup
    from sqlalchemy import text
    current = c.applications.repository.get(run['run_id'])
    stored = c.applications.repository.save(current, agent_reason=reason, status=status) if status != 'awaiting_review' else current
    before = c.applications.repository.get(run['run_id'])
    memory, runs = run_entries(c, state)
    after = c.applications.repository.get(run['run_id'])
    assert expected in runs[0]['next']
    assert (after['status'], after['version'], after.get('agent_calls')) == (before['status'], before['version'], before.get('agent_calls'))
    with c.runtime.database.session_factory() as db:  # building the summary made no model call
        assert db.execute(text('select count(*) from provider_usage')).scalar() == 0


def test_a_closed_browser_leaves_memory_readable_and_sign_in_with_the_user(setup):
    c, job, pack, app, service, state, run = setup
    for browser in c.browsers:browser.closed = True
    c.applications.browsers.clear()
    c.applications.repository.save(c.applications.repository.get(run['run_id']), status='needs_input',
        account_request={'origin': 'https://x.example', 'mode': 'login', 'stage': 'password'})
    memory, runs = run_entries(c, state)
    assert runs[0]['account_handoff'] is True and 'observe the live page' in runs[0]['browser']
    assert 'password' not in json.dumps(runs[0]).replace('"stage"', '')


def test_overflow_keeps_the_run_blocker_and_latest_correction(setup):
    c, job, pack, app, service, state, run = setup
    c.applications.repository.save(c.applications.repository.get(run['run_id']), status='needs_input',
        input_request={'id': 'q', 'text': 'Which start date should I use?', 'field_keys': ['k'], 'fields': [{'key': 'k', 'label': 'Start date'}]})
    for n in range(40):
        c.repository.append(state['task_id'], role='user', text=f'Old note {n}: ' + 'detail ' * 30, context={'application_id': app})
    c.repository.append(state['task_id'], role='user', text='Correction: my start date is June 2027.', context={'application_id': app})
    memory, runs = run_entries(c, c.repository.get(state['task_id']))
    from eregion.runtime.context.budget import estimate_tokens
    assert estimate_tokens(memory) <= 800
    assert runs and runs[0]['pending_question'] == 'Which start date should I use?' and runs[0]['question_fields'] == ['Start date']
    texts = [e.get('text') for e in memory['entries']]
    assert 'Correction: my start date is June 2027.' in texts
    assert memory['omitted_entries'] > 0 and sum(memory['omitted_by_kind'].values()) == memory['omitted_entries']
    assert 'not evidence' in memory['omission_note']


def test_a_run_that_changes_while_building_is_not_injected(setup):
    c, job, pack, app, service, state, run = setup
    real = c.applications.repository.get
    calls = {'n': 0}
    def moving(run_id):
        calls['n'] += 1
        current = real(run_id)
        return {**current, 'version': current['version'] + calls['n']}  # newer on every read
    payload = service.refresh(c.repository.get(state['task_id']), runs=moving)
    assert payload is None  # no stale summary when the run moved after it was read


def test_modes_off_shadow_and_allowlist(setup):
    c, job, pack, app, service, state, run = setup
    latest = c.repository.get(state['task_id'])
    c.runtime.task_memory = TaskMemoryService(c.runtime, TaskMemoryConfig('on', frozenset({'another-app'})))
    assert 'task_memory' not in c.harness.observe(latest)  # on, but not allowlisted
    c.runtime.task_memory = TaskMemoryService(c.runtime, TaskMemoryConfig('shadow', frozenset({app})))
    assert 'task_memory' not in c.harness.observe(latest)  # shadow builds, never injects
    c.runtime.task_memory = TaskMemoryService(c.runtime, TaskMemoryConfig())
    assert 'task_memory' not in c.harness.observe(latest)  # off
    c.runtime.task_memory = TaskMemoryService(c.runtime, TaskMemoryConfig('on', frozenset({app})))
    assert 'task_memory' in c.harness.observe(latest)
