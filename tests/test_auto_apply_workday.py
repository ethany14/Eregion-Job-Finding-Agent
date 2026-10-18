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


@pytest.mark.parametrize('path',['fill','fill_plain','type_field'])
def test_workday_date_segments_are_typed_and_empty_placeholders_are_not_values(path):
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
        # Autofill uses fill_plain and type_from_source uses type_field; all must type (W2).
        write={'fill':browser.fill,'fill_plain':browser.fill_plain,'type_field':lambda f,v:browser.type_field(f['key'],v)}[path]
        write(month,'08');write(year,'2026')
        assert [f['value'] for f in browser.observe()]==['08','2026']
    finally:browser.close()


@pytest.mark.parametrize('component,expected',[('start_month',['08','']),('start_year',['','2026']),('start_date',['08',''])])
def test_type_from_source_fills_a_workday_date_segment_from_a_cited_experience_component(monkeypatch,component,expected):
    # Adobe W6 re-test (2026-10-16): experience:<id>:start_month ("8") was refused for the MM
    # segment as lacking precision, and the error told the model to ask the user.
    from eregion.runtime.auto_apply.agent import AgentTools
    from eregion.runtime.auto_apply.experience import normalize_records
    monkeypatch.setenv('TYPESAFE_ENABLED','false')
    class DateBrowser(WorkdayBrowser):
        def _guard_request(self,route):
            if route.request.is_navigation_request():
                route.fulfill(content_type='text/html',body=(Path(__file__).parent/'fixtures/auto_apply/workday_date.html').read_text(encoding='utf-8'))
            else:route.abort()
    job=normalize_records([{'title':'Research','company':'Cornell University','start_date':'Aug 2026','current':'Present'}])[0]
    browser=DateBrowser(URL)
    try:
        session=AgentTools(browser,{'profile':{'experience':[job]},'answers':{}},autofill=lambda:{},active=lambda:True)
        obs=session.observe()
        target=next(f for f in session.fields if ('Year' if component=='start_year' else 'Month') in f['key'])
        result=session.execute('type_from_source',{'observation_id':obs['observation_id'],'field_key':target['key'],
            'source_ref':f"experience:{job['id']}:{component}"})
        assert 'error' not in result
        assert [f['value'] for f in browser.observe()]==expected
    finally:browser.close()


def test_a_month_component_does_not_fill_a_full_date_control():
    from eregion.runtime.auto_apply.education import format_date
    assert format_date({'key':'text:start:1','label':'Start date (MM/YYYY)'},'8','month') is None
    assert format_date({'key':'text:x--dateSectionMonth-input:1','label':'From*'},'13','month') is None


def test_filled_workday_date_segments_are_corrected_without_spilling_into_the_year():
    # Adobe re-test (2026-10-16): the site prefilled 2/2026; typing "08" over "2" spilled
    # digits into the year (it read "8202"), and the month's display "8" failed a "08" check.
    from eregion.runtime.auto_apply.autofill import matches
    class FilledBrowser(WorkdayBrowser):
        def _guard_request(self,route):
            if route.request.is_navigation_request():
                route.fulfill(content_type='text/html',body=(Path(__file__).parent/'fixtures/auto_apply/workday_date_filled.html').read_text(encoding='utf-8'))
            else:route.abort()
    browser=FilledBrowser(URL)
    try:
        month,year=browser.observe()
        browser.type_field(month['key'],'08')
        month,year=browser.observe()
        assert (month['value'],year['value'])==('8','2026')
        assert matches(month,'08') and matches(year,'2026')
        browser.type_field(month['key'],'08')   # already holds it: no keystrokes
        browser.type_field(year['key'],'2026')
        assert [f['value'] for f in browser.observe()]==['8','2026']
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


def test_covered_workday_radio_is_chosen_through_its_label():
    # W6 (Adobe): "employed by Adobe" radio check() timed out under a covering element.
    class RadioBrowser(WorkdayBrowser):
        def _guard_request(self,route):
            if route.request.is_navigation_request():
                route.fulfill(content_type='text/html',body=(Path(__file__).parent/'fixtures/auto_apply/workday_radio.html').read_text(encoding='utf-8'))
            else:route.abort()
    browser=RadioBrowser(URL)
    try:
        radio=browser.observe()[0]
        browser.fill(radio,'No')
        assert browser.observe()[0]['value']=='false'
    finally:browser.close()


@pytest.mark.parametrize('decided,checked',[('true',True),('yes',True),('Yes',True),('false',False),('no',False)])
def test_resolve_field_ticks_a_workday_checkbox_and_the_end_date_is_no_longer_required(monkeypatch,decided,checked):
    # W6 re-test: "I currently work here" stayed unticked ("true" reached a boolean-only writer),
    # so the To date stayed required and Next was refused.
    from eregion.runtime.auto_apply.agent import AgentTools
    from eregion.runtime.auto_apply.experience import normalize_records
    monkeypatch.setenv('TYPESAFE_ENABLED','false')
    class JobBrowser(WorkdayBrowser):
        def _guard_request(self,route):
            if route.request.is_navigation_request():
                route.fulfill(content_type='text/html',body=(Path(__file__).parent/'fixtures/auto_apply/workday_current_job.html').read_text(encoding='utf-8'))
            else:route.abort()
    job=normalize_records([{'title':'Research','company':'Cornell University','start_date':'Aug 2026','current':'Present'}])[0]
    browser=JobBrowser(URL)
    try:
        session=AgentTools(browser,{'profile':{'experience':[job]},'answers':{}},autofill=lambda:{},active=lambda:True)
        obs=session.observe()
        box=next(f for f in session.fields if f['kind']=='checkbox')
        assert any(f['required'] and 'To' in f['label'] for f in session.fields)
        result=session.execute('resolve_field',{'observation_id':obs['observation_id'],'field_key':box['key'],'value':decided,
            'mode':'equivalent','source_refs':[f"experience:{job['id']}:current"],'reason':'Resume says Present.'})
        fields=browser.observe()
        assert next(f for f in fields if f['key']==box['key'])['value'] is checked and result['verified']
        assert any(f['required'] and 'To' in f['label'] for f in fields) is (not checked)
    finally:browser.close()


@pytest.mark.parametrize('path', ['fill', 'fill_plain', 'type_field'])
def test_year_repadding_and_composite_end_key_do_not_clear_the_month(path):
    # A reduced mechanical failure mode, not a claimed capture of Workday internals:
    # End switches the widget's active segment; partial deletion re-pads the year.
    html = """<label for="w--dateSectionMonth-input">Month</label>
    <input id="w--dateSectionMonth-input" value="8">
    <label for="w--dateSectionYear-input">Year</label>
    <input id="w--dateSectionYear-input" value="2008"><script>
    const m=document.querySelector('[id*=Month]'),y=document.querySelector('[id*=Year]');
    y.addEventListener('keydown',e=>{
      if(e.key==='End'){e.preventDefault();m.focus();}
      if(e.key==='Backspace'){
        e.preventDefault();
        if(y.selectionStart===0 && y.selectionEnd===y.value.length)y.value='';
        else y.value=y.value.slice(0,-1).padStart(4,'0');
      }
    });</script>"""
    class DateBrowser(WorkdayBrowser):
        def _guard_request(self,route):
            if route.request.is_navigation_request():route.fulfill(content_type='text/html',body=html)
            else:route.abort()
    browser=DateBrowser(URL)
    try:
        month,year=browser.observe()
        if path=='type_field':browser.type_field(year['key'],'2026')
        else:getattr(browser,path)(year,'2026')
        assert [f['value'] for f in browser.observe()]==['8','2026']
    finally:browser.close()


def test_date_clear_detects_sibling_change_before_typing_replacement():
    html = """<label for="w--dateSectionMonth-input">Month</label>
    <input id="w--dateSectionMonth-input" value="8">
    <label for="w--dateSectionYear-input">Year</label>
    <input id="w--dateSectionYear-input" value="2008"><script>
    const m=document.querySelector('[id*=Month]'),y=document.querySelector('[id*=Year]');
    window.digits=0;
    y.addEventListener('keydown',e=>{
      if(e.key==='Backspace'){e.preventDefault();m.value='';}
      if(/^\d$/.test(e.key))window.digits++;
    });</script>"""
    class DateBrowser(WorkdayBrowser):
        def _guard_request(self,route):
            if route.request.is_navigation_request():route.fulfill(content_type='text/html',body=html)
            else:route.abort()
    browser=DateBrowser(URL)
    try:
        year=browser.observe()[1]
        with pytest.raises(BrowserBlocked,match='sibling segment'):
            browser.type_field(year['key'],'2026')
        assert browser.page.evaluate('window.digits')==0
        assert browser.observe()[1]['value']=='2008'
    finally:browser.close()


@pytest.mark.parametrize('path', ['fill', 'fill_plain', 'type_field'])
@pytest.mark.parametrize('initial_year', ['', '2008'])
def test_roving_date_segment_uses_widget_navigation_not_dom_focus(path,initial_year):
    # Adobe-18 evidence: Year tabindex=-1, Month is tab stop; typing into
    # DOM-focused Year changed Month. This fixture models that observed contract.
    html = """<div><label for="r--dateSectionMonth-input">Month</label>
    <input id="r--dateSectionMonth-input" role="spinbutton" value="8">
    <label for="r--dateSectionYear-input">Year</label>
    <input id="r--dateSectionYear-input" role="spinbutton" tabindex="-1" value="YEAR_VALUE"></div>
    <script>
    const parts=[...document.querySelectorAll('input')];let active=0;
    function activate(i){active=i;parts.forEach((p,j)=>p.tabIndex=j===i?0:-1);parts[i].focus();}
    document.querySelector('div').addEventListener('keydown',e=>{
      if(e.key==='ArrowRight'){e.preventDefault();activate(Math.min(1,active+1));return;}
      if(e.key==='ArrowLeft'){e.preventDefault();activate(Math.max(0,active-1));return;}
      const target=parts[active];
      if(e.key==='Backspace'){e.preventDefault();target.value='';return;}
      if(/^\d$/.test(e.key)){e.preventDefault();target.value+=e.key;}
    });</script>""".replace('YEAR_VALUE',initial_year)
    class DateBrowser(WorkdayBrowser):
        def _guard_request(self,route):
            if route.request.is_navigation_request():route.fulfill(content_type='text/html',body=html)
            else:route.abort()
    browser=DateBrowser(URL)
    try:
        month,year=browser.observe()
        if path=='type_field':browser.type_field(year['key'],'2026')
        else:getattr(browser,path)(year,'2026')
        assert [f['value'] for f in browser.observe()]==['8','2026']
        browser.type_field(month['key'],'07')
        assert [f['value'] for f in browser.observe()]==['07','2026']
    finally:browser.close()


def test_host_reads_closed_select_one_options_and_leaves_no_menu_open():
    # Adobe-22: the model opened each closed "Select One" question in its own round. The host now
    # reads them once on page entry; nothing is chosen and every menu is closed afterwards.
    from eregion.runtime.auto_apply.agent import AgentTools
    from eregion.runtime.auto_apply.runner import ApplicationHost
    class SelectBrowser(WorkdayBrowser):
        def _guard_request(self,route):
            if route.request.is_navigation_request():
                route.fulfill(content_type='text/html',body=(Path(__file__).parent/'fixtures/auto_apply/workday_select_one.html').read_text(encoding='utf-8'))
            else:route.abort()
    browser=SelectBrowser(URL)
    try:
        tools=AgentTools(browser,{'profile':{},'answers':{}},autofill=lambda:{},active=lambda:True)
        tools.observe()
        questions=[f for f in tools.fields if f['kind']=='combobox']
        assert len(questions)==2 and all(not f['options'] and not f['value'] for f in questions)
        host=ApplicationHost(tools,None,active=lambda:True,reserve=lambda kind:True,report=lambda *a:None)
        host._read_closed_options(tools)
        assert all([o['label'] for o in tools.observed_options[f['key']]][-2:]==['Yes','No'] for f in questions)
        assert browser.page.locator('[role="listbox"]').count()==0
        assert browser.page.evaluate('window.chosen')==0
        shown={f['key']:[o['label'] for o in f['options']] for f in tools.observe()['fields'] if f['kind']=='combobox'}
        assert all('Yes' in labels and 'No' in labels for labels in shown.values())
    finally:browser.close()


def test_choosing_a_searched_school_reopens_the_picker_with_the_search_text():
    # Intel apply test (2026-10-17): search_options found "University of Wisconsin-Madison", but
    # choose_option reopened the closed picker without text, saw "No Items." and refused.
    class SchoolBrowser(WorkdayBrowser):
        def _guard_request(self,route):
            if route.request.is_navigation_request():
                route.fulfill(content_type='text/html',body=(Path(__file__).parent/'fixtures/auto_apply/workday_search_school.html').read_text(encoding='utf-8'))
            else:route.abort()
    browser=SchoolBrowser(URL)
    try:
        school=next(f for f in browser.observe() if 'school' in f['key'])
        found=[o['label'] for o in browser.inspect_options(school['key'],query='Wisconsin')]
        assert 'University of Wisconsin-Madison' in found
        browser.page.keyboard.press('Tab')                       # focus leaves; the menu closes and clears
        browser.page.wait_for_timeout(100)
        assert browser.page.locator('[role="listbox"]').count()==0
        browser.choose_option(school['key'],'University of Wisconsin-Madison')
        assert browser.page.evaluate('window.selected')=='University of Wisconsin-Madison'
        with pytest.raises(BrowserBlocked):
            browser.choose_option(school['key'],'University of Nowhere')   # still exact-match only
    finally:browser.close()


def test_confirm_statement_checkbox_carries_the_statement_and_the_check_sees_it():
    # Adobe-27: the consent box is labelled only "Check this box to confirm the statement above";
    # the answer check could not connect the user's consent to it and refused three times.
    from eregion.runtime.auto_apply.agent import AgentTools
    class TermsBrowser(WorkdayBrowser):
        def _guard_request(self,route):
            if route.request.is_navigation_request():
                route.fulfill(content_type='text/html',body=(Path(__file__).parent/'fixtures/auto_apply/workday_terms_consent.html').read_text(encoding='utf-8'))
            else:route.abort()
    browser=TermsBrowser(URL)
    try:
        fields={f['name']:f for f in browser.observe()}
        terms=fields['acceptTermsAndAgreements']
        assert terms['label'].startswith('Check this box to confirm the statement above')
        assert "Adobe's Recruitment and Applicant Privacy Policy" in terms['help']
        assert fields['newsletter']['help']=='Optional job alert emails.'      # aria-describedby still wins
        state={'profile':{},'answers':{},'chat_revision':1,'messages':[{'id':'m1','role':'user',
            'text':'yes, tick the consent for the Adobe terms checkbox'}]}
        tools=AgentTools(browser,state,autofill=lambda:{},active=lambda:True);tools.observe()
        seen=[]
        tools.verify_answers=lambda items,context=None: seen.append((items,context)) or [True]*len(items)
        tools.execute('record_answers',{'observation_id':tools.observation_id,'answers':[{'field_key':terms['key'],
            'message_id':'m1','quote':'yes, tick the consent','value':'Yes'}]})
        question=seen[0][0][0]['question']
        assert 'confirm the statement above' in question and 'Applicant Privacy Policy' in question
        assert any('Applicant Privacy Policy' in q.get('help','') for q in seen[0][1]['questions'])
    finally:browser.close()
