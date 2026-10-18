"""Mechanical affordances exposed to the model; no semantic answers inferred."""
import pytest
from test_apply_experience_context import session
from test_auto_apply import field

@pytest.mark.parametrize('kind,choice,expected', [('text',False,'literal:'),('text',True,'picker:'),('combobox',False,'picker:'),('select',True,'choice:'),('checkbox',False,'checkbox:')])
def test_observation_explains_mechanics_without_choosing_value(kind,choice,expected):
    browser, tools = session()
    f=field('education',label='School',kind=kind,required=False)
    f.update(value='',choice_control=choice)
    browser.fields=[f]
    snapshot=tools.observe()
    item=next(f for f in snapshot['fields'] if f['key']=='education')
    assert item['control_input'].startswith(expected)
    assert not tools.state.get('field_decisions')
    assert not tools.state.get('education_bindings')
    assert not item.get('write_verified')
    assert browser.fields[0]['value']==''


def test_explicit_empty_is_done_in_observation_but_unverified_or_changed_fields_are_not():
    browser, tools=session()
    f=field('major',label='Field of Study',required=False);f['value']=''
    browser.fields=[f]; tools.observe()
    tools._resolve_field(f,{'mode':'leave_empty','value':'','source_refs':[],'reason':'No grounded option.'})
    item=next(x for x in tools.observe()['fields'] if x['key']=='major')
    assert item['done'] and item['completion']=='model_decided_leave_empty'
    assert item['reason']=='No grounded option.'
    assert 'major' in tools.observe()['completed_fields']
    for change in ({'value':'unexpected'}, {'value':'','invalid':True}, {'value':'','invalid':False,'required':True}):
        f.update(change)
        assert not next(x for x in tools.observe()['fields'] if x['key']=='major').get('done',False)
    f.update(value='',invalid=False,required=False)
    tools.state['answers']['major']='New user answer'
    assert not next(x for x in tools.observe()['fields'] if x['key']=='major').get('done',False)


def test_attempt_refusal_reports_a_verified_saved_omission(monkeypatch):
    from test_apply_replays import session_for,run,REPLAY
    from eregion.runtime.auto_apply.runner import FIELD_ATTEMPT_LIMIT
    browser,s,host,searches=session_for(monkeypatch)
    key=REPLAY['field']['key']
    s.execute('resolve_field',{'observation_id':s.observation_id,'field_key':key,'mode':'leave_empty','value':'','source_refs':[],'reason':'No reliable option.'})
    for _ in range(FIELD_ATTEMPT_LIMIT):host.count_attempt(s.fields[0])
    outcome=run(host,'inspect_field',{'observation_id':s.observation_id,'field_key':key})
    assert outcome['resolved'] and outcome['saved_decision']['mode']=='leave_empty'
    assert searches==[]
    assert 'Further searching is not required' in outcome['error']
    # A new explicit model choice remains possible; the host does not force omission.
    browser.fields[0]['options']=[{'label':'Systems Science','value':'Systems Science'}]
    s._cached_fields=None  # Fixture changed outside the tool execution.
    s.observe()
    assert s.fields[0]['options'][0]['value']=='Systems Science'
    browser.choose_option=lambda key,value: browser.fill(browser.fields[0],value)
    ref=f"education:{REPLAY['binding']['education_id']}:field_of_study"
    decided=s.execute('resolve_field',{'observation_id':s.observation_id,'field_key':key,'mode':'equivalent','value':'Systems Science','source_refs':[ref],'reason':'Scripted new decision for control-flow test, not a real mapping recommendation.'})
    assert decided['verified'] and s.state['field_decisions'][key]['mode']=='equivalent'
