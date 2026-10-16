"""Replays of saved real-run state (Adobe Workday, 2026-10-16), tool level with a scripted
model. They prove tool behaviour, not model behaviour: which decision a real model makes
from these observations needs a model replay."""
import json
from pathlib import Path

import pytest

from eregion.runtime.auto_apply.agent import AgentTools
from eregion.runtime.auto_apply.runner import ApplicationHost, FIELD_ATTEMPT_LIMIT
from eregion.runtime.core.engine import Turn
from test_auto_apply import FakeBrowser, database  # noqa: F401  (fixture)

REPLAY = json.loads((Path(__file__).parent / 'fixtures/replays/adobe_field_of_study.json').read_text(encoding='utf-8'))


def session_for(monkeypatch):
    monkeypatch.setenv('TYPESAFE_ENABLED', 'false')
    field = dict(REPLAY['field'])
    record = {'id': REPLAY['binding']['education_id'], 'school': 'Cornell University', 'degree': 'M.Eng.',
              'field_of_study': REPLAY['profile_fact']}
    binding = {**REPLAY['binding'], 'field': {k: field.get(k) for k in ('key', 'dom_id', 'label', 'group')}}
    browser = FakeBrowser(''); browser.fields = [field]
    searches = []
    def inspect_options(key, query=None):
        searches.append(query)
        browser.fields[0]['options'] = [{'label': o, 'value': o} for o in REPLAY['observed_searches'].get(query or '', [])]
        return browser.fields[0]['options']
    browser.inspect_options = inspect_options
    browser.field = lambda key: browser.fields[0]
    state = {'profile': {'education': [record]}, 'answers': {}, 'education_bindings': {field['key']: binding}}
    session = AgentTools(browser, state, autofill=lambda: {}, active=lambda: True)
    session.observe()
    host = ApplicationHost(session, object(), active=lambda: True, reserve=lambda _: True, report=lambda *a: None)
    return browser, session, host, searches


def run(host, name, args):
    turn = Turn('', {}, [])
    call = {'id': name + str(len(host.history)), 'name': name, 'args': args}
    outcome = host.execute(call, turn)
    host.after_action(call, outcome, turn)
    return outcome


def test_changing_search_results_do_not_reset_the_fields_attempts(monkeypatch):
    browser, session, host, searches = session_for(monkeypatch)
    key = REPLAY['field']['key']
    ref = f"education:{REPLAY['binding']['education_id']}:field_of_study"
    for query in ['Systems', 'Engineering', 'Systems', 'Engineering', 'Systems', 'Engineering']:
        browser.inspect_options(key, query)  # each search shows a different candidate set
        run(host, 'search_options', {'observation_id': session.observation_id, 'field_key': key, 'source_ref': ref})
    assert host.attempts(session.fields[0]) == FIELD_ATTEMPT_LIMIT
    refused = run(host, 'search_options', {'observation_id': session.observation_id, 'field_key': key, 'source_ref': ref})
    assert refused['error_code'] == 'field_attempt_limit'


def test_the_limit_leaves_the_models_own_decisions_available(monkeypatch):
    browser, session, host, searches = session_for(monkeypatch)
    key = REPLAY['field']['key']
    for _ in range(FIELD_ATTEMPT_LIMIT):
        host.count_attempt(session.fields[0])
    # Leaving the optional field empty is the model's explicit, immediately saved decision.
    obs = session.observe()
    left = run(host, 'resolve_field', {'observation_id': obs['observation_id'], 'field_key': key, 'value': '',
        'mode': 'leave_empty', 'source_refs': [], 'reason': 'No listed field of study represents Systems Engineering.'})
    assert left['left_empty'] and session.state['field_decisions'][key]['mode'] == 'leave_empty'
    # A new fact (the user names a listed option) is a grounded new plan: the count resets.
    session.state['answers'][key] = 'Industrial Engineering'
    assert host.attempts(session.fields[0]) == 0


def test_a_required_field_cannot_be_left_empty_by_the_model(monkeypatch):
    browser, session, host, searches = session_for(monkeypatch)
    browser.fields[0]['required'] = True
    obs = session.observe()
    from eregion.runtime.auto_apply.browser import BrowserBlocked
    with pytest.raises(BrowserBlocked, match='optional'):
        run(host, 'resolve_field', {'observation_id': obs['observation_id'], 'field_key': REPLAY['field']['key'],
            'value': '', 'mode': 'leave_empty', 'source_refs': [], 'reason': 'No option.'})
    assert REPLAY['field']['key'] not in session.state.get('field_decisions', {})


def test_a_leave_empty_decision_is_saved_at_once_and_lets_the_page_advance(monkeypatch):
    browser, session, host, searches = session_for(monkeypatch)
    key = REPLAY['field']['key']
    page = {'url': 'https://adobe.wd5.myworkdayjobs.com/apply', 'heading': 'My Experience', 'text': '', 'can_submit': False,
            'controls': [{'id': 'next', 'label': 'Next', 'kind': 'next', 'disabled': False}]}
    browser.page_state = lambda: page
    clicked = []
    browser.click_page_action = lambda control: clicked.append(control['label'])
    from eregion.runtime.auto_apply.browser import BrowserBlocked
    obs = session.observe()
    with pytest.raises(BrowserBlocked, match='Field of Study'):  # undecided: the step is held
        session.execute('click_page_action', {'observation_id': obs['observation_id'], 'control_id': 'next'})
    session.execute('resolve_field', {'observation_id': obs['observation_id'], 'field_key': key, 'value': '',
        'mode': 'leave_empty', 'source_refs': [], 'reason': 'No listed field of study represents Systems Engineering.'})
    saved = session.state['field_decisions'][key]
    assert saved['mode'] == 'leave_empty' and saved['reason'].startswith('No listed')  # saved before any review
    obs = session.observe()
    session.execute('click_page_action', {'observation_id': obs['observation_id'], 'control_id': 'next'})
    assert clicked == ['Next']


STOP = json.loads((Path(__file__).parent / 'fixtures/replays/adobe_page_budget_stop.json').read_text(encoding='utf-8'))


def test_the_saved_page_budget_stop_gives_the_career_agent_reason_blockers_and_budget(database, monkeypatch):
    # The career agent spent 10 calls after this stop without reporting it. Its stop notice
    # now carries the reason, blocking fields and remaining budget, and asks it to report.
    from types import SimpleNamespace
    from eregion.runtime.career.harness import CareerHarness
    from eregion.runtime.provider_usage import UsageLedger, provider_call, usage_scope
    monkeypatch.setenv('JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS', '60')
    monkeypatch.setenv('JOB_AGENT_TASK_MAX_INPUT_TOKENS', '1000000')
    task_id = STOP['application']['origin_task_id']
    with usage_scope(ledger=UsageLedger(database.session_factory), task_id=task_id):
        per_call = STOP['ledger']['input_tokens'] // STOP['ledger']['provider_attempts']
        for _ in range(STOP['ledger']['provider_attempts']):
            with provider_call('fixture', input_token_reservation=per_call) as details:
                details.update(input_tokens=per_call, output_tokens=1)
    harness = CareerHarness.__new__(CareerHarness)
    harness.applications = SimpleNamespace(repository=SimpleNamespace(get=lambda run_id: STOP['application']))
    harness.repository = SimpleNamespace(sessions=database.session_factory)
    stopped = harness.capability_stopped({'kind': 'application', 'id': STOP['application']['run_id']})
    assert stopped['agent_reason'] == 'page_budget' and stopped['message'].startswith('This page used')
    assert stopped['blocking_fields'] == []  # every required field on the page was filled
    assert stopped['budget']['provider_attempts_used'] == 56 and stopped['budget']['provider_attempts_left'] == 4
    assert stopped['budget']['input_tokens_left'] == 1_000_000 - per_call * 56
    assert 'remaining budget' in stopped['next'] and 'reply_application' not in stopped['next']
    view = harness.application(STOP['application']['run_id'])  # the default view stays small
    assert 'fields' not in view and len(json.dumps(view)) < 1500


def test_a_committed_equivalent_outside_the_visible_options_can_be_accepted(monkeypatch):
    # Replay dry run: Workday committed "Computer and Information Science" for a "Computer
    # Science" fact; the value was outside the visible page of the long list, so resolve_field
    # refused it and the education check held the page with an unnamed "Field of Study".
    from eregion.runtime.auto_apply.education import gaps, normalize_records
    monkeypatch.setenv('TYPESAFE_ENABLED', 'false')
    record = normalize_records([{'school': 'University of Wisconsin-Madison', 'degree': 'B.A.', 'field_of_study': 'Computer Science'}])[0]
    field = {'key': 'text:education--fieldOfStudy:2', 'label': 'Field of Study', 'kind': 'text', 'choice_control': True,
             'required': False, 'value': 'Computer and Information Science', 'group': 'From', 'section': '',
             'options': [{'label': 'Accounting', 'value': 'Accounting'}]}
    browser = FakeBrowser(''); browser.fields = [field]
    binding = {'education_id': record['id'], 'component': 'field_of_study', 'source_ref': f"education:{record['id']}:field_of_study",
               'field': {k: field.get(k) for k in ('key', 'dom_id', 'label', 'group')}}
    state = {'profile': {'education': [record]}, 'answers': {}, 'education_bindings': {field['key']: binding}}
    session = AgentTools(browser, state, autofill=lambda: {}, active=lambda: True)
    assert gaps(state, session.observe() and session.fields, None) == ['Field of Study (text:education--fieldOfStudy:2)']
    obs = session.observe()
    session.execute('resolve_field', {'observation_id': obs['observation_id'], 'field_key': field['key'],
        'value': 'Computer and Information Science', 'mode': 'equivalent', 'source_refs': [binding['source_ref']],
        'reason': 'Workday lists Computer Science as Computer and Information Science.'})
    session.observe()
    assert gaps(state, session.fields, None) == []
