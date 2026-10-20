from copy import deepcopy
from types import SimpleNamespace
import pytest
from test_apply_experience_context import session
from eregion.runtime.auto_apply.record_actions import expand
from eregion.runtime.auto_apply.agent import TOOLS
from eregion.runtime.auto_apply.runner import ApplicationHost
from eregion.runtime.core.engine import validate_call, ToolArgumentsError


def setup():
    browser, s = session()
    for i, field in enumerate(browser.fields): field['dom_id'] = f'workExperience-1--field{i}'
    s.observe()
    key = s.fields[0]['key']
    call = {'id': 'record', 'name': 'apply_record', 'args': {'observation_id': s.observation_id, 'record': 'work:one',
        'bindings': [{'field_key': key, 'component': 'start_month'}, {'field_key': 'current', 'component': 'current'}],
        'decisions': [{'field_key': 'current', 'mode': 'equivalent', 'value': 'true',
            'source_refs': ['experience:one:current'], 'reason': 'The model interprets Present as current.'}], 'fill_fields': [key]}}
    return browser, s, call


def test_plan_has_no_effect_until_existing_tools_execute():
    browser, s, call = setup()
    before = deepcopy(s.state)
    children = expand(s, call)
    assert s.state == before and browser.fields[1]['value'] is False
    ApplicationHost.validate_batch(SimpleNamespace(session=s), children, None)
    for child in children: validate_call(child, TOOLS)
    assert [c['name'] for c in children] == ['plan_records', 'bind_experience', 'resolve_field', 'autofill']
    assert children[-1]['args']['literal_only'] is True
    for child in children[:-1]: s.execute(child['name'], child['args'])
    assert browser.fields[1]['value'] is True
    assert s.state['field_decisions']['current']['value'] is True


@pytest.mark.parametrize('issue', ['stale', 'foreign_source', 'missing_source', 'second_row', 'unknown_record', 'bad_control', 'existing_other_record', 'shared_section'])
def test_invalid_plan_refused_without_effect(issue):
    browser, s, call = setup()
    if issue == 'stale': call['args']['observation_id'] = 'stale'
    if issue == 'foreign_source': call['args']['decisions'][0]['source_refs'] = ['experience:two:end_date']
    if issue == 'missing_source': call['args']['decisions'][0]['source_refs'] = ['experience:one:missing']
    if issue == 'second_row': s.fields[1]['dom_id'] = 'workExperience-2--field'
    if issue == 'shared_section':
        for field in s.fields:
            field.pop('dom_id', None)
            field['group'] = 'Work Experience'
    if issue == 'unknown_record': call['args']['record'] = 'work:missing'
    if issue == 'bad_control': call['args']['then_control_id'] = 'unknown'
    if issue == 'existing_other_record':
        s._bind_experience({'experience_id': 'two', 'fields': [{'field_key': s.fields[0]['key'], 'component': 'start_month'}]})
    before = deepcopy(s.state)
    with pytest.raises(ToolArgumentsError): expand(s, call)
    assert s.state == before and browser.fields[1]['value'] is False


def test_overlap_and_bad_components_rejected_before_execution():
    _, s, call = setup()
    call['args']['fill_fields'].append('current')
    host = SimpleNamespace(session=s)
    ApplicationHost.validate_batch(host, expand(s, call), None)
    (remaining, dropped), = host.autofill_trims.values()  # the decided field leaves the literal fill
    assert dropped == ['current'] and 'current' not in remaining
    call['args']['fill_fields'].pop()
    call['args']['bindings'][0]['component'] = 'invented'
    with pytest.raises(ToolArgumentsError):
        for child in expand(s, call): validate_call(child, TOOLS)
    assert not s.state.get('experience_bindings')


def test_leave_empty_does_not_claim_nonempty_page_value_verified():
    _, s, _ = setup()
    field = s.fields[0]
    field['required'] = False
    from eregion.runtime.auto_apply.browser import BrowserBlocked
    with pytest.raises(BrowserBlocked, match='cannot clear'):
        s._resolve_field(field, {'mode': 'leave_empty', 'value': '', 'source_refs': [], 'reason': 'Optional missing mapping'})
    assert field['value'] == '2'
    assert s.state['field_decisions'][field['key']]['mode'] == 'leave_empty'


def test_literal_autofill_does_not_refresh_observation_or_make_semantic_calls(monkeypatch):
    _, s, call = setup()
    s.fast_choices = lambda *a, **k: pytest.fail('No nested model request in literal mode')
    # Use the real fast_options function, so an accidental observe() before an
    # empty options batch is caught too (dynamic-02 production failure).
    s.observe = lambda: pytest.fail('Literal autofill must not issue a new observation id')
    result = s.execute('autofill', {'field_keys': call['args']['fill_fields'], 'literal_only': True})
    assert s.observation_id == call['args']['observation_id']
    assert result['unresolved_fields']  # It does not claim missing bindings are done.


def test_record_rows_expose_structure_without_assigning_profile_or_answers():
    browser, session, _ = setup()
    snapshot = session.observe()
    row = snapshot['record_rows'][0]
    assert row['row_id'] == 'workExperience-1'
    assert row['field_keys'] == [f['key'] for f in session.fields]
    assert row['pending_field_keys'] == row['field_keys']
    assert row['bound_records'] == [] and row['batch_tool'] == 'apply_record'
    assert not session.state.get('experience_bindings')
    assert not session.state.get('field_decisions')
    session.execute('bind_experience', {'observation_id':session.observation_id,
       'experience_id':'one','fields':[{'field_key':'current','component':'current'}]})
    row = session.observe()['record_rows'][0]
    assert row['bound_records'] == ['work:one']
    assert 'current' in row['pending_field_keys']  # binding never decides the boolean


def test_scoped_detail_preserves_other_record_blockers_and_saved_decisions():
    from eregion.runtime.auto_apply.observation import focus_observation
    fields=[{'key':'a','dom_id':'workExperience-1--title'}, {'key':'b','dom_id':'workExperience-2--title'}]
    state={'observation_scope':['a'],'observation_scope_page':['u','h'],
           'field_decisions':{'a':{'reason':'A'},'b':{'reason':'B'}}}
    snapshot={'page':{'url':'u','heading':'h'},'fields':[{'key':'a','label':'A'},{'key':'b','label':'B'}],
        'field_decisions':deepcopy(state['field_decisions']),'completed_fields':['a','b'],
        'record_rows':[{'field_keys':['a'],'row_id':'1'},{'field_keys':['b'],'row_id':'2','pending_field_keys':['b']}],
        'next_step_blockers':[{'key':'b'}]}
    result=focus_observation(snapshot,state,fields)
    assert list(result['field_decisions'])==['a']
    assert result['next_step_blockers']==[{'key':'b'}]
    assert result['field_index'][0]['key']=='b'
    assert result['record_rows'][1]['pending_field_keys']==['b']
    assert state['field_decisions']['b']['reason']=='B'
