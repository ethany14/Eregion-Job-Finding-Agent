"""Offline tool replay of the apply-test-05 failure pattern; no real model/site."""
import pytest
from test_auto_apply import FakeBrowser, field
from eregion.runtime.auto_apply.agent import AgentTools
from eregion.runtime.auto_apply.autofill import desired_answer
from eregion.runtime.auto_apply.browser import BrowserBlocked
from eregion.runtime.auto_apply.education import new_browser_bindings
from eregion.runtime.request_context import ContextArchive


def session():
    browser = FakeBrowser('')
    month = field('text:workExperience--startDate-dateSectionMonth-input:1', label='From*')
    month['value'] = '2'
    current = field('current', kind='checkbox', label='I currently work here', required=False)
    current['value'] = False
    browser.fields = [month, current]
    browser.type_field = lambda key, value: browser.fill(next(f for f in browser.fields if f['key']==key), value)
    state = {'profile': {'experience': [
        {'id':'one','company':'Example University','title':'Research','start_date':'Aug 2026','current':'Present'},
        {'id':'two','company':'Other Employer','title':'Intern','start_date':'Jul 2022','end_date':'Aug 2022'}]}, 'answers': {}}
    tools = AgentTools(browser, state, autofill=lambda: {}, active=lambda: True)
    tools.observe()
    return browser, tools


def bind(tools):
    return tools.execute('bind_experience', {'observation_id':tools.observation_id, 'experience_id':'experience:one',
        'fields':[{'field_key':tools.fields[0]['key'],'component':'start_month'},
                  {'field_key':'current','component':'current'}]})


def test_saved_failure_pattern_model_binds_and_resolves_current_employment():
    browser, tools = session()
    before = tools.observe()
    assert all(not f['write_verified'] for f in before['fields'])
    bind(tools)
    obs = tools.observe()
    month, current = tools.fields
    assert desired_answer(month, tools.state) == '08'
    from eregion.runtime.auto_apply.experience import gaps
    assert len(gaps(tools.state, tools.fields)) == 2
    assert desired_answer(current, tools.state) is None  # Present never becomes true in code.
    assert any(b['reason']=='bound_fact_needs_model_decision' for b in obs['next_step_blockers'])
    assert obs['experience_coverage'][1]['bound_fields'] == []
    result = tools.execute('resolve_field', {'observation_id':tools.observation_id,'field_key':'current',
        'mode':'equivalent','value':'true','source_refs':['experience:one:current'],
        'reason':'The resume states Present for this employment.'})
    assert result['verified'] and browser.fields[1]['value'] is True
    tools.observe()
    result = tools.execute('type_from_source', {'observation_id':tools.observation_id,
        'field_key':month['key'],'source_ref':'experience:one:start_month'})
    assert browser.fields[0]['value'] == '08'
    assert desired_answer(browser.fields[0], tools.state) == '08'
    assert gaps(tools.state, browser.observe()) == []


def test_binding_cannot_cross_records_or_survive_browser_replacement():
    browser, tools = session(); bind(tools)
    with pytest.raises(BrowserBlocked):
        tools._bind_experience({'experience_id':'two','fields':[{'field_key':tools.fields[0]['key'],'component':'start_month'}]})
    reset = new_browser_bindings(tools.state)
    assert reset['experience_bindings'] == {}


def test_binding_rejects_missing_facts_atomically():
    browser, tools = session()
    with pytest.raises(BrowserBlocked):
        tools._bind_experience({'experience_id':'one','fields':[
            {'field_key':tools.fields[0]['key'],'component':'start_month'},
            {'field_key':'current','component':'end_month'}]})
    assert not tools.state.get('experience_bindings')


def test_unchanged_page_reuses_archive_but_keeps_latest_action_handle():
    archive = ContextArchive()
    data = {'page_evidence_hash':'page-1','url':'https://example.com/apply','text':'x'*7000}
    first = archive.tool_result({**data,'observation_id':'old'})
    second = archive.tool_result({**data,'observation_id':'new'})
    assert first['context_ref'] == second['context_ref']
    assert second['observation_id'] == 'new'
    changed = archive.tool_result({**data,'text':'y'*7000,'observation_id':'third'})
    assert changed['context_ref'] != second['context_ref']


def test_bound_work_field_refuses_another_records_source():
    browser, tools = session(); bind(tools)
    with pytest.raises(BrowserBlocked, match='bound work-experience'):
        tools.source(tools.fields[0], 'experience:two:start_month')


def test_field_identity_change_invalidates_work_binding():
    browser, tools = session(); bind(tools)
    browser.fields[0]['group'] = 'Another row'
    assert desired_answer(browser.fields[0], tools.state) is None


def test_negative_checkbox_decision_is_verified_not_treated_as_empty():
    browser, tools = session()
    tools.state['answers']['current'] = False
    obs = tools.observe()
    # Completed values retain a compact done entry, including false.
    assert next(f for f in obs['fields'] if f['key']=='current')['done'] is True


def test_changed_page_field_value_does_not_reuse_old_archive():
    archive = ContextArchive()
    data = {'page_evidence_hash':'same-text-hash','url':'https://example.com/apply',
            'text':'x'*7000,'controls':[{'key':'current','value':False}]}
    first = archive.tool_result({**data,'observation_id':'old'})
    second = archive.tool_result({**data,'observation_id':'new','controls':[{'key':'current','value':True}]})
    assert first['context_ref'] != second['context_ref']


def test_model_resolution_cannot_write_a_different_bound_record():
    browser, tools = session(); bind(tools)
    before = browser.fields[0]['value']
    with pytest.raises(BrowserBlocked, match='bound work-experience'):
        tools.execute('resolve_field', {'observation_id':tools.observation_id,
            'field_key':tools.fields[0]['key'],'mode':'equivalent','value':'07',
            'source_refs':['experience:two:start_month'],'reason':'Use the other job month.'})
    assert browser.fields[0]['value'] == before


def test_restart_keeps_confirmed_work_fact_without_reusing_row_identity():
    browser, tools = session(); bind(tools)
    key = tools.fields[0]['key']
    tools.state['answers'][key] = '09'
    tools.state['answer_sources'] = {key: {'message_id':'user-correction'}}
    reset = new_browser_bindings(tools.state)
    assert key not in reset['answers']
    from eregion.runtime.auto_apply.experience import facts
    assert facts(tools.state['profile'], reset['experience_answer_overrides'])['experience:one:start_month'] == '09'


def test_confirmed_false_work_fact_survives_restart():
    browser, tools = session(); bind(tools)
    tools.state['answers']['current'] = False
    reset = new_browser_bindings(tools.state)
    from eregion.runtime.auto_apply.experience import facts
    assert facts(tools.state['profile'], reset['experience_answer_overrides'])['experience:one:current'] is False


def test_record_coverage_exposes_real_components_and_missing_binding_is_actionable():
    browser, tools = session()
    coverage=tools.observe()['experience_coverage']
    assert 'current' in coverage[0]['available_components']
    assert 'current' not in coverage[1]['available_components']
    assert 'end_date' in coverage[1]['available_components']
    with pytest.raises(BrowserBlocked, match='experience:two:current') as failure:
        tools._bind_experience({'experience_id':'two','fields':[{'field_key':'current','component':'current'}]})
    assert 'resolve_field' in str(failure.value)
    assert not tools.state.get('experience_bindings')


def test_signature_date_segments_type_the_components_recorded_for_them():
    # Intel-33: the user gave "2026/10/17"; the model recorded "10" for MM and "17" for DD,
    # then type_from_source(answer:<segment>) refused both and the run spent its budget.
    from eregion.runtime.auto_apply.education import date_parts, format_date
    browser = FakeBrowser('')
    prefix = 'text:selfIdentifiedDisabilityData--dateSignedOn-dateSection'
    browser.fields = [field(prefix+part+'-input:1', label='Date*') for part in ('Month','Day','Year')]
    browser.type_field = lambda key, value: browser.fill(next(f for f in browser.fields if f['key']==key), value)
    state = {'profile': {}, 'answers': {browser.fields[0]['key']:'10', browser.fields[1]['key']:'17', browser.fields[2]['key']:'2026'}}
    tools = AgentTools(browser, state, autofill=lambda: {}, active=lambda: True)
    for current in list(browser.fields):
        tools.observe()
        tools.execute('type_from_source', {'observation_id':tools.observation_id,'field_key':current['key'],'source_ref':'answer:'+current['key']})
    assert [f['value'] for f in browser.fields] == ['10','17','2026']
    for text in ('2026/10/17','2026.10.17','2026-10-17'):
        assert date_parts(text) == {'year':'2026','month':'10'}
        assert [format_date(f, text) for f in browser.fields] == ['10','17','2026']
    assert date_parts('2026/10.17') == {'year':'2026'}  # mixed separators stay unparsed


def test_voluntary_self_identification_is_not_left_empty_without_the_users_answer():
    # Intel-34: the Hispanic/Latino question was left empty instead of asking the user.
    browser = FakeBrowser('')
    hispanic = field('combobox:hispanicOrLatino:1', kind='combobox', label='Please indicate if you identify yourself as Hispanic or Latino.', required=False)
    hispanic['options'] = [{'label':'Yes','value':'1'},{'label':'No','value':'2'}]
    note = field('textarea:roleDescription:1', kind='textarea', label='Role Description', required=False)
    browser.fields = [hispanic, note]
    tools = AgentTools(browser, {'profile': {}, 'answers': {}}, autofill=lambda: {}, active=lambda: True)
    tools.observe()
    leave = lambda key, refs: tools.execute('resolve_field', {'observation_id':tools.observation_id,'field_key':key,
        'mode':'leave_empty','value':'','source_refs':refs,'reason':'No confirmed fact for this optional question.'})
    with pytest.raises(BrowserBlocked, match='Ask them once'):
        leave(hispanic['key'], [])
    assert leave(note['key'], [])['left_empty']  # ordinary optional fields are unaffected
    tools.state['answers'][hispanic['key']] = 'I prefer not to answer'
    tools.observe()
    assert leave(hispanic['key'], ['answer:'+hispanic['key']])['left_empty']


