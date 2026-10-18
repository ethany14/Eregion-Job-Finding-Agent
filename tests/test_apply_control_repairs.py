import pytest
from test_apply_autofill_scope import make
from eregion.runtime.auto_apply.autofill import failure_signature, autofill
from eregion.runtime.auto_apply.runner import attempt_target
from eregion.runtime.auto_apply.observation import focus_observation


def test_date_equivalent_targets_keep_attempt_and_failure_identity():
    f={'key':'text:x-dateSectionMonth-input:1','kind':'text','value':'08'}
    a={'profile':{},'answers':{f['key']:'Aug'}}
    b={'profile':{},'answers':{f['key']:'August'}}
    from eregion.runtime.auto_apply.autofill import desired_answer
    assert desired_answer(f,a)=='08'
    assert attempt_target(a,f)==attempt_target(b,f)
    assert failure_signature(f,'08')==failure_signature({**f,'value':'8'},'8')
    from eregion.runtime.auto_apply.runner import attempt_record
    from eregion.runtime.telemetry import outcome_fingerprint
    a['field_attempts']={f['key']:{'v':2,'count':4,'target':outcome_fingerprint(['08',None])}}
    assert attempt_record(a,f)==(4,'migrated')


def test_read_field_does_not_open_or_write_and_ignores_attempt_limit():
    browser,state,session=make()
    browser.inspect_options=lambda *a,**k:pytest.fail('Read must not open a menu')
    browser.fill=lambda *a,**k:pytest.fail('Read must not write')
    state['field_attempts']={'work':{'count':9}}
    result=session.execute('read_field',{'field_key':'work','observation_id':session.observation_id})
    assert result['read_only'] and result['target']=='confirmed work'


def test_failed_write_records_post_write_state_and_is_not_repeated():
    browser,state,session=make();writes=[]
    def broken(f,v):
        writes.append(f['key']);browser.fields[0]['value']='corrupted'
        raise RuntimeError('write failed after changing value')
    browser.fill=broken
    state['automation_mode']='hybrid'
    state['autofill_scope']=['work']
    autofill(browser,state,lambda:b'')
    autofill(browser,state,lambda:b'')
    assert writes==['work']


def test_local_observation_preserves_unreviewed_and_navigation_and_restores_full():
    browser,state,session=make()
    before=session.observe()
    session.execute('focus_fields',{'field_keys':['work']})
    focused=session.observe()
    assert [f['key'] for f in focused['fields']]==['work']
    assert focused['field_index'][0]['key']=='education'
    assert focused['next_step_blockers']==before['next_step_blockers']
    assert focused['unreviewed_checkboxes']==before['unreviewed_checkboxes']
    assert len(session.fields)==2
    session.execute('focus_fields',{'field_keys':[]})
    assert len(session.observe()['fields'])==2


def test_scope_keeps_whole_dom_record_and_falls_back_on_navigation():
    fields=[{'key':k,'dom_id':dom} for k,dom in [('a','workExperience-4--title'),('b','workExperience-4--current'),('c','education-8--school')]]
    state={'observation_scope':['a'],'observation_scope_page':['url','Experience']}
    def snap():return {'fields':fields.copy(),'page':{'url':'url','heading':'Experience','text':'large'},'next_step_blockers':[{'key':'c'}]}
    focused=focus_observation(snap(),state,fields)
    assert [f['key'] for f in focused['fields']]==['a','b']
    assert focused['next_step_blockers']==[{'key':'c'}]
    changed=snap();changed['page']['heading']='Questions'
    assert len(focus_observation(changed,state,fields)['fields'])==3


def test_browser_replacement_clears_observation_scope():
    from eregion.runtime.auto_apply.education import new_browser_bindings
    _,state,_=make();state['observation_scope']=['work'];state['observation_scope_page']=['url','Experience']
    reset=new_browser_bindings(state)
    assert reset['observation_scope'] is None and reset['observation_scope_page'] is None


@pytest.mark.parametrize('value,invalid,expected',[('8',False,True),('08',False,True),('9',False,False),('8',True,False)])
def test_read_field_reports_mechanical_date_match_without_deciding(value,invalid,expected):
    browser,state,session=make()
    f=session.fields[0];f.update(key='text:x-dateSectionMonth-input:1',dom_id='x-dateSectionMonth-input',label='Month',kind='text',value=value,invalid=invalid)
    state['profile']['experience']=[{'id':'dated','company':'Example','start_date':'Aug 2026'}]
    session._bind_experience({'experience_id':'dated','fields':[{'field_key':f['key'],'component':'start_month'}]})
    before=dict(state.get('field_decisions',{}))
    result=session.execute('read_field',{'field_key':f['key'],'observation_id':session.observation_id})
    assert result['value_matches_target'] is expected
    assert state.get('field_decisions',{})==before
    assert result['read_only']
