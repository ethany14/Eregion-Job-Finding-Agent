import time
from types import SimpleNamespace
import pytest
from eregion.runtime.auto_apply.agent import AgentTools
from eregion.runtime.auto_apply.browser import BrowserBlocked
from eregion.runtime.auto_apply.runner import ApplicationHost,LoopStop
from test_auto_apply import field

def setup_check():
    f=field('q',kind='select',label='Will you require sponsorship?')
    f['options']=[{'label':x,'value':x} for x in ['Yes','No']]
    state={'profile':{},'answers':{},'answer_sources':{},'chat_revision':0,
      'url':'https://example.test/jobs/123','opening_page_evidence':{'text':'Job in San Jose, California, United States'},
      'messages':[{'id':'u1','role':'user','text':"I do not need sponsorship."}]}
    s=AgentTools(None,state,autofill=None,active=lambda:True,bind_answers=lambda *a:None)
    s.fields=[f];s.observation_id='o'
    args={'observation_id':'o','answers':[{'field_key':'q','message_id':'u1','quote':'I do not need sponsorship.','value':'Yes'}]}
    return s,args

def test_failed_verdict_reused_only_for_exact_same_sources_and_context():
    s,args=setup_check();calls=[]
    def check(rows,*,context):calls.append(context);return [False]
    s.verify_answers=check
    for _ in range(2):
        with pytest.raises(BrowserBlocked):s._record_answers(args)
    assert len(calls)==1 and s.state['answers']=={}
    assert calls[0]['opening_page']['text'].startswith('Job in San Jose')
    assert calls[0]['user_messages'][0]['text']=='I do not need sponsorship.'
    s.state['opening_page_evidence']={'text':'Changed posting: Toronto, Canada'}
    with pytest.raises(BrowserBlocked):s._record_answers(args)
    assert len(calls)==2
    s.state['messages'].append({'id':'u2','role':'user','text':'Correction: I need sponsorship now.'})
    with pytest.raises(BrowserBlocked):s._record_answers(args)
    assert len(calls)==3

def test_failed_provider_call_is_not_cached_or_saved():
    s,args=setup_check()
    def fail(*a,**k):raise TimeoutError('provider')
    s.verify_answers=fail
    with pytest.raises(TimeoutError):s._record_answers(args)
    assert not s.state.get('answer_verification_cache') and s.state['answers']=={}

def test_nested_verifier_yields_before_reservation_if_slice_is_too_short():
    session=SimpleNamespace(state={},browser=object(),autofill_called=False,failed_fields=set())
    model=SimpleNamespace(timeout=90,verify_answers=lambda *a,**k:pytest.fail('No request'))
    host=ApplicationHost(session,model,active=lambda:True,reserve=lambda _:pytest.fail('No reservation'),
       report=lambda *_:None,decisions=SimpleNamespace(enabled=False))
    try:
        host.deadline=time.monotonic()+30
        with pytest.raises(LoopStop) as err:host.verify_answers([],context={})
        assert err.value.result['reason']=='yielded'
    finally:host.requests.close()

def test_opening_context_is_captured_from_observed_posting_and_survives_form():
    from test_apply_experience_context import session
    browser, tools = session()
    page = {'stage':'job_page','url':'https://example.test/job/123',
            'heading':'Example role','text':'Loading','controls':[]}
    browser.page_state = lambda: dict(page)
    tools.observe()
    assert not tools.state.get('opening_page_evidence')
    page.update(text='Observed posting in California, United States',
                controls=[{'kind':'open_application','label':'Apply'}])
    tools.observe()
    saved = dict(tools.state['opening_page_evidence'])
    assert saved == {'url':page['url'],'heading':page['heading'],'text':page['text']}
    page.update(stage='form',heading='Questions',text='Can you work in this country?',controls=[])
    tools.observe()
    assert tools.state['opening_page_evidence'] == saved
