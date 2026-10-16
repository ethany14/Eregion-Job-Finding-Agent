"""Real Chrome, entirely offline, including account and multi-page boundaries."""
import json
import os
from pathlib import Path

import pytest

from eregion.runtime.auto_apply import authentication
from eregion.runtime.auto_apply.agent import HybridApplicationAgent
from eregion.runtime.auto_apply.browser import ApplicationBrowser, BrowserBlocked
from eregion.runtime.auto_apply.contracts import AccountCredentials, ContinueApplication
from eregion.runtime.auto_apply.loop import ApplicationLoop
from eregion.runtime.auto_apply.repository import ApplyRepository
from test_auto_apply import database, request, wait
from test_auto_apply_agent import ScriptModel

pytestmark=pytest.mark.skipif(os.getenv('JOB_AGENT_BROWSER_TESTS')!='1',reason='Offline Chrome opt-in')
URL='https://example.wd5.myworkdayjobs.com/en-US/External/job/Engineer_JR123'


class WorkdayBrowser(ApplicationBrowser):
    def __init__(self,url,**kwargs):super().__init__(url,headless=True,**kwargs)
    def _guard_request(self,route):
        if route.request.is_navigation_request():
            route.fulfill(content_type='text/html',body=(Path(__file__).parent/'fixtures/auto_apply/workday.html').read_text(encoding='utf-8'))
        else:route.abort()


def navigate(label):
    return ('click_page_action',lambda s:{'observation_id':s['observation_id'],
        'control_id':next(c['id'] for c in s['page']['controls'] if c['label']==label)})


def test_job_to_login_to_multipage_review_and_single_submit(database):
    model=ScriptModel([navigate('Apply'),navigate('Apply Manually'),('autofill',{}),navigate('Save and Continue'),
        ('autofill',{}),navigate('Save and Continue'),('autofill',{}),('ready_for_review',{})])
    repo=ApplyRepository(database.session_factory)
    loop=ApplicationLoop(repo,browser_factory=WorkdayBrowser,agent_factory=lambda:HybridApplicationAgent(model_factory=lambda:model),retry_delays=())
    try:
        run_id=loop.start(request(url=URL,automation_mode='hybrid',profile={'first_name':'Test','last_name':'Candidate','email':'test@example.com','country':'US'}))['run_id']
        auth=wait(loop,run_id);assert auth['status']=='needs_input' and auth['account_request']['mode']=='login'
        assert model.calls==['click_page_action','click_page_action']
        loop.account(run_id,AccountCredentials(expected_version=auth['version'],origin=auth['account_request']['origin'],mode='login',username='test@example.com',password='Password-NOT-IN-MODEL'))
        review=wait(loop,run_id)
        assert review['status']=='awaiting_review',review.get('message')
        assert len(review['review_fields'])==5
        assert not review['fields'] # Final step contains only the summary.
        assert 'Password-NOT-IN-MODEL' not in json.dumps(review)
        browser=loop.browsers[run_id]
        counts=lambda:browser.page.evaluate('[window.accountClicks,window.nextClicks,window.submitClicks]')
        assert loop.executor.submit(counts).result()==[1,2,0]
        # A summary-only edit invalidates approval, even without input fields.
        loop.executor.submit(lambda:browser.page.locator('#summary').evaluate("e=>e.textContent='Changed'")).result()
        loop.continue_run(run_id,ContinueApplication(expected_version=review['version'],confirm_submit=True))
        changed=wait(loop,run_id);assert changed['status']=='awaiting_review'
        assert loop.executor.submit(counts).result()==[1,2,0]
        loop.continue_run(run_id,ContinueApplication(expected_version=changed['version'],confirm_submit=True))
        assert wait(loop,run_id)['status']=='submitted'
    finally:loop.close()


def test_registration_explicit_consent_and_password_exclusion():
    b=WorkdayBrowser(URL)
    try:
        for label in ['Apply','Apply Manually']:
            b.click_page_action(next(c for c in b.page_state()['controls'] if c['label']==label))
        ctx=authentication.context(b)
        authentication.switch_mode(b,'register',ctx['origin']);ctx=authentication.context(b)
        assert ctx['mode']=='register' and len(ctx['consents'])==1
        assert not any(f['kind']=='password' for f in b.observe())
        args={'expected_version':1,'origin':ctx['origin'],'mode':'register','username':'test@example.com','password':' secret with spaces '}
        with pytest.raises(BrowserBlocked):authentication.authenticate(b,AccountCredentials(**args))
        assert b.page.evaluate('window.accountClicks')==0
        authentication.authenticate(b,AccountCredentials(**args,consents={ctx['consents'][0]['key']:True}),expected=ctx)
        assert b.page.evaluate('window.accountClicks')==1
        assert b.page_state()['heading']=='My Information'
        assert 'secret with spaces' not in json.dumps(b.page_state())
    finally:b.close()


def test_login_state_reused_by_same_owner_only(tmp_path):
    if os.name!='nt':pytest.skip('DPAPI')
    from eregion.runtime.auto_apply.browser_sessions import BrowserSessionStore
    store=BrowserSessionStore(tmp_path/'state')
    b=WorkdayBrowser(URL,session_store=store,owner_key='owner1')
    try:
        b.context.add_cookies([{'name':'auth','value':'offline-session','url':URL}])
    finally:b.close()
    for owner,heading in [('owner1','My Information'),('owner2','Example Engineer')]:
        b=WorkdayBrowser(URL,session_store=store,owner_key=owner)
        try:assert b.page_state()['heading']==heading
        finally:b.close()


def test_workday_visible_submit_surface_and_header_are_disambiguated():
    b=WorkdayBrowser(URL)
    try:
        b.page.set_content('''<button>Sign In</button><form onsubmit="return false">
          <label>Email<input data-automation-id="email" autocomplete="email" type="text"></label>
          <label>Password<input type="password"></label><div>
          <div role="button" aria-label="Submit" data-automation-id="click_filter" onclick="window.clicked=(window.clicked||0)+1; window.email=this.closest('form').querySelector('input').value; this.closest('form').remove()">Sign In</div>
          <button type="submit" aria-hidden="true">Sign In</button></div></form>
          <input data-automation-id="beecatcher" name="website">''')
        assert len(b.observe())==1 # Only email; password and honeypot excluded.
        account=authentication.context(b)
        def no_model():
            raise AssertionError('This conventional login must not call a model')
        authentication.authenticate(b,AccountCredentials(expected_version=1,
            origin=account['origin'],mode='login',username='test@example.com',password='fixture-password'),
            model_factory=no_model)
        assert b.page.evaluate('window.clicked')==1
        assert b.page.evaluate('window.email')=='test@example.com'
        assert authentication.context(b) is None
        b.page.set_content('''<main><h1>Review</h1><div>
          <div role="button" aria-label="Submit" data-automation-id="click_filter" onclick="window.submits=(window.submits||0)+1">Submit</div>
          <button aria-hidden="true" type="submit">Submit</button></div></main>''')
        assert b.page_state()['can_submit']
        b.submit();assert b.page.evaluate('window.submits')==1
    finally:b.close()


def test_secure_account_ui_keeps_password_out_of_chat_and_clears_input():
    from playwright.sync_api import sync_playwright
    from urllib.parse import urlsplit
    root=Path(__file__).parents[1]/'frontend/web_app';requests=[]
    state={'run_id':'account-ui','version':3,'url':URL,'status':'needs_input','message':'Sign in securely.',
        'fields':[],'messages':[],'account_request':{'origin':'https://example.wd5.myworkdayjobs.com','mode':'login','consents':[]}}
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='chrome',headless=True);page=browser.new_page()
        def route(r):
            path=urlsplit(r.request.url).path
            if path=='/api/auto-apply':r.fulfill(json={'attempts':[state]})
            elif path=='/api/auto-apply/account-ui/account':
                requests.append((path,r.request.post_data_json));r.fulfill(json={'status':'queued'})
            elif path.startswith('/web-assets/'):
                file=root/path.removeprefix('/web-assets/');r.fulfill(path=str(file))
            elif path=='/apply':r.fulfill(path=str(root/'apply.html'))
            else:r.abort()
        page.route('**/*',route);page.goto('https://northstar.example/apply?mode=application')
        page.locator('#attempts button').click();page.get_by_role('button',name='Sign in securely',exact=True).click()
        page.locator('#account-username').fill('test@example.com');page.locator('#account-password').fill('UI-SECRET-ONLY')
        # Polling must not erase a partially completed secure form.
        page.wait_for_timeout(2200);assert page.locator('#account-password').input_value()=='UI-SECRET-ONLY'
        page.get_by_role('button',name='Sign in & continue',exact=True).click()
        page.wait_for_timeout(200)
        assert len(requests)==1 and requests[0][1]['password']=='UI-SECRET-ONLY'
        assert page.locator('#account-password').input_value()==''
        assert 'UI-SECRET-ONLY' not in page.locator('#agent-messages').inner_text()
        page.get_by_role('button',name='Sign in securely',exact=True).click()
        assert page.locator('#account-password').input_value()==''
        browser.close()


def test_workday_prompt_reads_late_lists_categories_and_releases_the_covered_radio():
    # Adobe pilot (2026-10-15): the list rendered late and read as empty, rows read twice
    # ("Job Board not checked"), and the open prompt covered the radio, whose click timed out.
    class PromptBrowser(WorkdayBrowser):
        def _guard_request(self,route):
            if route.request.is_navigation_request():
                route.fulfill(content_type='text/html',body=(Path(__file__).parent/'fixtures/auto_apply/workday_prompt.html').read_text(encoding='utf-8'))
            else:route.abort()
    browser=PromptBrowser(URL)
    try:
        fields=browser.observe()
        source=next(f for f in fields if 'Hear About' in f['label'])
        radio=next(f for f in fields if f['kind']=='radio')
        top=browser.inspect_options(source['key'])
        assert [(o['label'],o['expandable']) for o in top]==[('Adobe Source',True),('Job Board',True)]
        browser.fill(radio,'No')  # the prompt is still open over it
        assert next(f for f in browser.observe() if f['key']==radio['key'])['value']=='false'
        browser.inspect_options(source['key'])
        assert [o['label'] for o in browser.explore_option(source['key'],'Adobe Source')]==['Adobe.com','Adobe Recruiting Team']
        browser.choose_option(source['key'],'Adobe.com')
        assert next(f for f in browser.observe() if f['key']==source['key'])['value']=='Adobe.com'
    finally:browser.close()


def test_workday_date_segments_are_typed_and_empty_placeholders_are_not_values():
    # Adobe pilot (2026-10-15): "MM" read as a value and a set month turned into 08/2008.
    class DateBrowser(WorkdayBrowser):
        def _guard_request(self,route):
            if route.request.is_navigation_request():
                route.fulfill(content_type='text/html',body=(Path(__file__).parent/'fixtures/auto_apply/workday_date.html').read_text(encoding='utf-8'))
            else:route.abort()
    browser=DateBrowser(URL)
    try:
        month,year=browser.observe()
        assert month['value']=='' and year['value']==''
        browser.fill(month,'08');browser.fill(year,'2026')
        assert [f['value'] for f in browser.observe()]==['08','2026']
    finally:browser.close()


def test_filled_workday_picker_can_be_changed_without_inspecting_first():
    # Adobe pilot (2026-10-15): the UW-Madison degree read "Masters" and every choose_option
    # for "Bachelors" failed, because the menu was never opened for it.
    class DegreeBrowser(WorkdayBrowser):
        def _guard_request(self,route):
            if route.request.is_navigation_request():
                route.fulfill(content_type='text/html',body=(Path(__file__).parent/'fixtures/auto_apply/workday_degree.html').read_text(encoding='utf-8'))
            else:route.abort()
    browser=DegreeBrowser(URL)
    try:
        degree=browser.observe()[0]
        assert degree['value']=='Masters'
        browser.choose_option(degree['key'],'Bachelors')
        assert browser.observe()[0]['value']=='Bachelors'
    finally:browser.close()


def test_workday_rerender_keeps_field_keys_and_education_bindings():
    # W1 (Adobe): education-10--fieldOfStudy became education-148--fieldOfStudy after a
    # re-render, which dropped the record binding and the failure counts.
    from eregion.runtime.auto_apply.education import binding
    class RerenderBrowser(WorkdayBrowser):
        def _guard_request(self,route):
            if route.request.is_navigation_request():
                route.fulfill(content_type='text/html',body=(Path(__file__).parent/'fixtures/auto_apply/workday_rerender.html').read_text(encoding='utf-8'))
            else:route.abort()
    browser=RerenderBrowser(URL)
    try:
        before=browser.observe()
        keys=[f['key'] for f in before]
        assert len(set(keys))==4 and keys[0].endswith(':1') and keys[2].endswith(':2')  # two entries kept apart
        school2=before[2]
        state={'education_bindings':{school2['key']:{'education_id':'uw','component':'school','source_ref':'education:uw:school',
            'field':{k:school2.get(k) for k in ('key','dom_id','label','group')}}}}
        browser.fill(school2,'University of Wisconsin-Madison')
        browser.page.click('#rerender')
        after=browser.observe()
        assert [f['key'] for f in after]==keys and after[2]['dom_id']!=school2['dom_id']
        assert after[2]['value']=='University of Wisconsin-Madison' and binding(after[2],state)['education_id']=='uw'
        assert binding(after[0],state) is None  # the first entry is not the bound record
    finally:browser.close()


def test_workday_search_returns_the_results_not_the_previous_list():
    # W1 (Adobe): searching Field of Study returned the unfiltered list still on screen.
    class SearchBrowser(WorkdayBrowser):
        def _guard_request(self,route):
            if route.request.is_navigation_request():
                route.fulfill(content_type='text/html',body=(Path(__file__).parent/'fixtures/auto_apply/workday_search.html').read_text(encoding='utf-8'))
            else:route.abort()
    browser=SearchBrowser(URL)
    try:
        study=browser.observe()[0]
        browser.inspect_options(study['key'])
        results=browser.inspect_options(study['key'],query='Systems')
        assert [o['label'] for o in results]==['Computer Systems Analysis','Management Information Systems']
        browser.choose_option(study['key'],'Management Information Systems')
        assert browser.observe()[0]['value']=='Management Information Systems'
    finally:browser.close()
