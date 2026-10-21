"""Exercise the default chat UI through the real API; all external IO is synthetic."""
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright, expect

from eregion.api.main import create_app
from eregion.runtime.career.service import CareerService
from test_career_flow import career, ToolModel, execute
from langchain_core.messages import AIMessage
from test_task_loop import ProviderError

pytestmark=pytest.mark.skipif(os.getenv('JOB_AGENT_BROWSER_TESTS')!='1',reason='Opt-in offline Chrome integration.')


def test_general_browsing_without_resume_has_preview_in_existing_chat(career,tmp_path):
    from eregion.runtime.browser.manager import BrowsingManager
    from test_general_browser import LocalWebBrowser
    c=career
    c.repository.change(c.state['task_id'],status='cancelled',pause_requested=True)
    c.applications.repository.clear_candidate()
    c.harness._browsing=BrowsingManager(factory=LocalWebBrowser)
    model=ToolModel(lambda s:('browser_open',{'url':'https://first.example/'}) if not s.get('browser')
        else ('finish_task',{'text':'The public website is open. Its heading is Public website.'}))
    service=CareerService(c.runtime,c.applications,harness=c.harness,model_factory=lambda:model)
    app=create_app(run_service=object(),session_runtime=c.runtime)
    app.state.auto_apply=c.applications;app.state.career_agent=service
    with TestClient(app,base_url='http://localhost') as client,sync_playwright() as p:
        browser=p.chromium.launch(channel='chrome',headless=True)
        page=browser.new_page(locale='en-US',viewport={'width':1440,'height':1000});errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        def route(r):
            if not r.request.url.startswith('http://localhost/'):return r.abort()
            response=client.request(r.request.method,r.request.url,content=r.request.post_data,
                headers={k:v for k,v in r.request.headers.items() if k in {'content-type','origin'}})
            r.fulfill(status=response.status_code,body=response.content,headers={'content-type':response.headers.get('content-type','')})
        page.route('**/*',route);page.goto('http://localhost/apply')
        page.locator('#chat-text').fill('Open https://first.example/ and inspect the public website.')
        page.locator('#send-message').click()
        expect(page.locator('#agent-messages')).to_contain_text('Its heading is Public website.',timeout=20000)
        state=next(s for s in c.repository.list() if s['status']=='completed')
        view=service.view(state['task_id'])
        assert view['application_run_id'] is None and view['browser']['url']=='https://first.example/'
        assert client.get(view['preview_url']).status_code==200
        page.locator('#browser-toggle').click()
        expect(page.locator('#browser-preview')).to_be_visible(timeout=10000)
        page.screenshot(path=str(tmp_path/'general-browser-preview.png'),full_page=True)
        assert not errors
        browser.close()


def test_rendered_browser_evidence_flows_into_saved_job(career):
    from eregion.runtime.browser.manager import BrowsingManager
    from test_general_browser import LocalWebBrowser
    c=career
    class JobBrowser(LocalWebBrowser):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.page.locator('body').evaluate("""e => {
                const article=document.createElement('article');
                article.innerHTML='<h1>Software engineer</h1><p>Build Python services, analyze data, collaborate with customers, and maintain reliable production systems.</p>';
                article.append(document.querySelector('#loaded'));
                e.append(article);
            }""")
    manager=BrowsingManager(factory=JobBrowser);c.harness._browsing=manager
    try:
        opened=execute(c,'browser_open',url='https://first.example/job')
        load=next(control for control in opened['controls'] if control['label']=='Load content')
        execute(c,'browser_click',observation_id=opened['observation_id'],ref=load['ref'])
        execute(c,'browser_wait')
        c.harness.discovery.job=lambda _:pytest.fail('Rendered evidence should not require another HTTP-only fetch.')
        job=execute(c,'read_job',url='https://first.example/job')
        assert job['source']=='rendered_browser' and 'Loaded successfully' in job['description']
        saved=execute(c,'import_job',url=job['url'])
        assert 'Loaded successfully' in c.runtime.workspace.current_snapshot(saved['application_id']).cleaned_job_description
        c.repository.change(c.state['task_id'],pause_requested=True)
        with pytest.raises(Exception,match='pause'):execute(c,'browser_type',observation_id=opened['observation_id'],ref=load['ref'],text='stale')
    finally:manager.close()


@pytest.mark.parametrize('entry', ['home', 'saved_job'])
def test_default_chat_searches_scores_reviews_applies_and_interviews(career,tmp_path,entry):
    c=career
    saved=execute(c,'import_job',url='https://jobs.lever.co/example/abc123') if entry=='saved_job' else None
    c.repository.change(c.state['task_id'],status='cancelled',pause_requested=True)
    observations=[]
    def decide(s):
        observations.append(s)
        if s.get('goal')=='Built Python APIs.':
            import json
            (tmp_path/'answer-snapshot.json').write_text(json.dumps(s,indent=2),encoding='utf-8')
        if not s['jobs']:
            if not any(h['tool']=='search_jobs' for h in s['history']):return 'search_jobs',{'query':'Python'}
            return 'import_job',{'url':'https://jobs.lever.co/example/abc123'}
        job=s['jobs'][0]
        if job['match_score'] is None:return 'score_job',{'application_id':job['application_id']}
        if not s['materials']:return 'generate_materials',{'application_id':job['application_id'],'artifacts':['tailored_resume','cover_letter']}
        pack=s['materials'][0]
        if any(i['status']!='approved' for i in pack['items']):return 'ask_user',{'text':'Review the prepared documents below.','wait_for_reply':True}
        if not s['applications']:return 'start_application',{'application_id':job['application_id'],'pack_id':pack['pack_id']}
        application=s['applications'][0]
        if application['status']!='submitted':return 'wait_for_result',{'kind':'application','id':application['run_id']}
        if not s['interviews']:return 'start_interview',{'application_id':job['application_id'],'question_count':3}
        practice=s['interviews'][0]
        last=next(m for m in reversed(s['messages']) if m['role']=='user')
        if last['text']=='Built Python APIs.' and not practice['latest_feedback']:
            return 'answer_interview',{'interview_id':practice['interview']['mock_interview_id'],
                'question_id':practice['question']['question_id'],'message_id':last['id']}
        return 'ask_user',{'text':practice['question']['question_text'],'wait_for_reply':True}
    model=ToolModel(decide)
    service=CareerService(c.runtime,c.applications,harness=c.harness,model_factory=lambda:model)
    app=create_app(run_service=object(),session_runtime=c.runtime)
    app.state.auto_apply=c.applications
    app.state.career_agent=service
    errors,requests=[],[]
    with TestClient(app,base_url='http://localhost') as client,sync_playwright() as playwright:
        browser=playwright.chromium.launch(channel='chrome',headless=True)
        page=browser.new_page(locale='en-US',viewport={'width':1440,'height':1000})
        page.on('pageerror',lambda error:errors.append(str(error)))
        def handle(route):
            request=route.request;requests.append((request.method,request.url,request.post_data))
            response=client.request(request.method,request.url,content=request.post_data,
                headers={k:v for k,v in request.headers.items() if k in {'content-type','origin'}},follow_redirects=False)
            route.fulfill(status=response.status_code,body=response.content,
                headers={k:v for k,v in response.headers.items() if k in {'content-type','location','cache-control'}})
        page.route('**/*',lambda route:handle(route) if route.request.url.startswith('http://localhost/') else route.abort())
        # Resolve through the actual API first: Playwright routes intercept only
        # the first request in a redirect chain, and no TCP server runs here.
        entry_url=f"/app?application={saved['application_id']}" if saved else '/'
        redirect=client.get(entry_url,follow_redirects=False)
        assert redirect.status_code==307
        page.goto('http://localhost'+redirect.headers['location'])
        assert page.url.startswith('http://localhost/apply')
        expect(page.get_by_role('link',name='Back to career workspace')).to_have_count(0)
        if not saved:
            expect(page.locator('#welcome h2')).to_have_text(re.compile(r'^Good (morning|afternoon|evening)'))
            page.locator('#chat-text').fill('Find Python roles, compare fit, tailor materials, apply and practice the interview.')
            page.locator('#send-message').click()
        expect(page.locator('.career-job strong')).to_contain_text('Match score:',timeout=20000)
        expect(page.get_by_role('button',name='Approve this version')).to_have_count(2,timeout=20000)
        expect(page.locator('#attachment')).to_contain_text('original.pdf')
        # Changing stages stays in this conversation and each approval pins a document revision.
        page.get_by_role('button',name='Approve this version').first.click()
        expect(page.get_by_role('button',name='Approve this version')).to_have_count(1,timeout=10000)
        page.get_by_role('button',name='Approve this version').click()
        submit=page.get_by_role('button',name='Submit application',exact=True)
        expect(submit).to_be_visible(timeout=20000)
        assert not any(b.submissions for b in c.browsers)
        submit.click()
        expect(page.locator('.career-interview')).to_contain_text('What did you personally do?',timeout=15000)
        expect(page.locator('.career-job')).to_contain_text('Applied')
        page.locator('#chat-text').fill('Built Python APIs.')
        page.locator('#send-message').click()
        expect(page.locator('.career-interview')).to_contain_text('Explain the outcome.',timeout=10000)
        assert any('/api/career/tasks' in url and method=='POST' for method,url,_ in requests)
        assert sum(b.submissions for b in c.browsers)==1
        if saved:
            assert observations[0]['jobs'][0]['application_id']==saved['application_id']
            assert observations[0]['goal']=='Help me review this saved job.'
            assert observations[0]['messages'][0]['context']=={'application_id':saved['application_id']}
        assert errors==[]
        page.screenshot(path=str(tmp_path/'career-chat.png'),full_page=True)
        browser.close()


def test_chat_surfaces_model_outage_and_recovers_a_greeting(career):
    c=career
    c.repository.change(c.state['task_id'],status='cancelled',pause_requested=True)
    class Model:
        calls=0
        recover=False
        def invoke(self,*args):
            self.calls+=1
            if not self.recover:raise ProviderError(503)
            return AIMessage(content='',tool_calls=[{'id':'greeting','name':'finish_task','args':{'text':'Hi! What would you like help with?'}}])
    model=Model()
    service=CareerService(c.runtime,c.applications,harness=c.harness,model_factory=lambda:model)
    service.loop.retry_delays=(0,0)
    app=create_app(run_service=object(),session_runtime=c.runtime)
    app.state.auto_apply=c.applications;app.state.career_agent=service
    with TestClient(app,base_url='http://localhost') as client,sync_playwright() as playwright:
        browser=playwright.chromium.launch(channel='chrome',headless=True)
        page=browser.new_page(locale='en-US')
        def handle(route):
            request=route.request
            response=client.request(request.method,request.url,content=request.post_data,
                headers={k:v for k,v in request.headers.items() if k in {'content-type','origin'}})
            route.fulfill(status=response.status_code,body=response.content,
                headers={'content-type':response.headers.get('content-type','application/json')})
        page.route('**/*',lambda route:handle(route) if route.request.url.startswith('http://localhost/') else route.abort())
        page.goto('http://localhost/apply')
        page.locator('#chat-text').fill('hi');page.locator('#send-message').click()
        expect(page.locator('.model-error')).to_contain_text('overloaded (HTTP 503)',timeout=10000)
        expect(page.locator('#agent-status')).to_have_text('Needs attention')
        expect(page.get_by_role('button',name='Open browser',exact=True)).to_have_count(0)
        assert model.calls==3 and not c.browsers
        model.recover=True
        page.get_by_role('button',name='Try again',exact=True).click()
        expect(page.locator('#agent-messages')).to_contain_text('Hi! What would you like help with?',timeout=10000)
        expect(page.locator('.model-error')).to_have_count(0)
        assert model.calls==4 and not c.browsers
        browser.close()
