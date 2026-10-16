"""Scoped autofill must not mutate or charge semantic decisions for other fields."""
import pytest
from test_auto_apply import FakeBrowser, field
from eregion.runtime.auto_apply.agent import AgentTools
from eregion.runtime.auto_apply.autofill import autofill
from eregion.runtime.auto_apply.fast_options import fill_options
from eregion.runtime.auto_apply.browser import BrowserBlocked


def make():
    browser=FakeBrowser(''); browser.fields=[field('work'),field('education')]
    state={'profile':{},'answers':{'work':'confirmed work','education':'other fact'},
           'autofill_failures':{'education':{'signature':'old','code':'option_mapping_required'}}}
    session=AgentTools(browser,state,autofill=lambda:autofill(browser,state,lambda:b''),active=lambda:True)
    session.observe()
    return browser,state,session


def test_explicit_scope_fills_work_preserves_unrelated_failure_and_excludes_it_from_result():
    browser,state,session=make()
    result=session.execute('autofill',{'field_keys':['work']})
    assert browser.fields[0]['value']=='confirmed work'
    assert browser.fields[1]['value']==''
    assert result['filled']==['work'] and not result['failed']
    assert result['scope_status']=='ok' and result['out_of_scope_failure_count']==1
    assert 'education' in state['autofill_failures']
    # A subsequent call cannot accidentally expand to the whole page.
    session.execute('autofill',{})
    assert browser.fields[1]['value']==''


def test_bad_scope_is_rejected_before_any_writes():
    browser,state,session=make()
    for keys in [[],['missing']]:
        with pytest.raises(BrowserBlocked):session.execute('autofill',{'field_keys':keys})
    assert all(f['value']=='' for f in browser.fields)


def test_only_scoped_fast_options_are_even_considered(monkeypatch):
    browser,state,session=make();seen=[]
    session.fast_choices=lambda *a:pytest.fail('No semantic model needed')
    monkeypatch.setattr('eregion.runtime.auto_apply.fast_options.source_for',lambda s,f:seen.append(f['key']))
    fill_options(session,field_keys=['work'])
    assert seen==['work']


def test_scope_failure_reports_partial_without_discarding_success():
    browser,state,session=make()
    original=browser.fill
    def fill(f,v):
        if f['key']=='education':raise RuntimeError('cannot write')
        original(f,v)
    browser.fill=fill
    result=session.execute('autofill',{'field_keys':['work','education']})
    assert result['scope_status']=='partial'
    assert result['filled']==['work'] and 'education' in result['failed']


def test_host_literal_fill_never_calls_semantic_options(monkeypatch):
    browser,state,session=make();session._host_literal_autofill=True
    monkeypatch.setattr('eregion.runtime.auto_apply.fast_options.fill_options',lambda *a,**k:pytest.fail('Host semantic call'))
    session.execute('autofill',{'field_keys':['work']})


def test_initial_host_old_failures_are_not_new_failure_or_whole_page_scope():
    from eregion.runtime.auto_apply.autofill import failure_signature
    browser,state,session=make()
    state['autofill_failures']['education']['signature']=failure_signature(browser.fields[1],'other fact')
    session._host_literal_autofill=True
    result=session.execute('autofill',{})
    assert result['filled']==['work'] and result['failed']=={}
    assert result['historical_failure_count']==1 and result['scope']==['work']
    assert 'education' in state['autofill_failures']
    assert browser.fields[1]['value']==''
    assert result['completion_level']=='literal_writes_only'


def test_initial_host_does_not_hide_a_new_write_failure():
    browser,state,session=make();session._host_literal_autofill=True
    browser.fill=lambda *a: (_ for _ in ()).throw(RuntimeError('write failed'))
    result=session.execute('autofill',{})
    assert 'work' in result['failed']
    assert result['scope_status']=='failed'


def test_unchecked_is_unreviewed_until_model_saves_a_grounded_decision():
    browser,state,session=make()
    flag=field('current',kind='checkbox',label='I currently work here',required=False)
    flag['value']=False;browser.fields.append(flag)
    state['profile']['experience']=[{'id':'intern','company':'Example','end_date':'Aug 2022'}]
    obs=session.observe()
    assert [f['key'] for f in obs['unreviewed_checkboxes']]==['current']
    result=session.execute('autofill',{'field_keys':['work']})
    assert result['scope_status']=='ok' and result['unreviewed_checkboxes'][0]['key']=='current'
    obs=session.observe()
    session.execute('resolve_field',{'observation_id':obs['observation_id'],'field_key':'current',
        'mode':'equivalent','value':'false','source_refs':['experience:intern:end_date'],
        'reason':'This internship ended in August 2022, so it is not a current role.'})
    assert browser.fields[-1]['value'] is False
    assert session.observe()['unreviewed_checkboxes']==[]
    assert state['field_decisions']['current']['value'] is False
