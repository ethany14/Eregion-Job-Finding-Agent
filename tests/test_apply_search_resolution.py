"""Adobe-10 unresolved preselection and grounded query regressions; no providers."""
import pytest
from test_apply_replays import session_for, run, REPLAY
from test_auto_apply import FakeBrowser, field
from eregion.runtime.auto_apply.browser import BrowserBlocked
from eregion.runtime.auto_apply.autofill import autofill
from eregion.runtime.auto_apply.runner import FIELD_ATTEMPT_LIMIT


def test_preselected_but_unmapped_picker_counts_attempts_and_explains_decision(monkeypatch):
    browser,s,host,searches=session_for(monkeypatch)
    f=browser.fields[0];f.update(value='Systems Science',value_committed=True,choice_control=True)
    obs=s.observe();item=obs['fields'][0]
    assert item['mapping']=='undecided' and not item['write_verified']
    assert item['selected_option_decision'] == 'committed_option'
    assert 'already an observed option' in obs['field_notes']['committed_option']
    for _ in range(FIELD_ATTEMPT_LIMIT):
        run(host,'inspect_field',{'observation_id':s.observation_id,'field_key':f['key']})
    assert host.attempts(f)==FIELD_ATTEMPT_LIMIT
    # One read past the limit is evidence for the decision (Adobe-35); an unchanged repeat is not.
    first=run(host,'inspect_field',{'observation_id':s.observation_id,'field_key':f['key']})
    assert first.get('error_code')!='field_attempt_limit'
    outcome=run(host,'inspect_field',{'observation_id':s.observation_id,'field_key':f['key']})
    assert outcome['error_code']=='field_attempt_limit' and 'already read' in outcome['error']
    searched=run(host,'search_options',{'observation_id':s.observation_id,'field_key':f['key'],
        'source_ref':f"education:{REPLAY['binding']['education_id']}:field_of_study",'query':'Systems','query_reason':'Discipline term.'})
    assert searched['error_code']=='field_attempt_limit'  # searching is still an attempt
    assert not s.state.get('field_decisions')  # Guard does not decide equivalence or omission.


def test_grounded_alternative_query_is_search_only(monkeypatch):
    browser,s,host,searches=session_for(monkeypatch)
    key=browser.fields[0]['key'];ref=f"education:{REPLAY['binding']['education_id']}:field_of_study"
    result=run(host,'search_options',{'observation_id':s.observation_id,'field_key':key,
        'source_ref':ref,'query':'Systems','query_reason':'Search the discipline term rather than the full degree label.'})
    assert searches==['Systems']
    assert s.state['profile']['education'][0]['field_of_study']==REPLAY['profile_fact']
    assert not s.state.get('field_decisions') and not s.state['answers']
    assert s.state['option_search_log'][key]['queries']==['Systems']
    assert 'full catalogue' in s.state['option_search_log'][key]['coverage']
    assert result['options']


@pytest.mark.parametrize('query,reason',[('', 'reason'),('Systems',''),('x'*121,'reason')])
def test_bad_alternative_search_is_refused_without_browser_write(monkeypatch,query,reason):
    browser,s,host,searches=session_for(monkeypatch)
    with pytest.raises(BrowserBlocked):
        s.execute('search_options',{'observation_id':s.observation_id,'field_key':browser.fields[0]['key'],
            'source_ref':f"education:{REPLAY['binding']['education_id']}:field_of_study",'query':query,'query_reason':reason})
    assert searches==[]


@pytest.mark.parametrize('retained',[True,False])
def test_final_autofill_reconciles_only_verified_target(retained):
    browser=FakeBrowser('');f=field('date',label='From');f['value']='';browser.fields=[f]
    def write(target,value):
        f['value']=value if retained else 'wrong'
        raise BrowserBlocked('A sibling date changed while writing.',code='date_group_changed')
    browser.fill_plain=write
    state={'profile':{},'answers':{'date':'2026'},'automation_mode':'hybrid','autofill_scope':['date']}
    result=autofill(browser,state,lambda:b'')
    assert bool(result['failed']) is (not retained)
    assert ('date' in result['filled']) is retained


def test_repeating_exact_source_as_query_needs_no_extra_explanation(monkeypatch):
    browser,s,host,searches=session_for(monkeypatch)
    s.execute('search_options',{'observation_id':s.observation_id,'field_key':browser.fields[0]['key'],
        'source_ref':f"education:{REPLAY['binding']['education_id']}:field_of_study",'query':REPLAY['profile_fact']})
    # The exact fact first; with no literal match its words follow in the same call (2026-10-20).
    assert searches[0]==REPLAY['profile_fact'] and searches[1:]==['Systems','Engineering']
    assert not s.state.get('field_decisions')
