"""Durable candidate data, scoped recall, and complete repeated education."""
import base64
from io import BytesIO
import os

import pytest

from eregion.runtime.auto_apply.agent import AgentTools
from eregion.runtime.auto_apply.autofill import autofill, desired_answer
from eregion.runtime.auto_apply.browser import BrowserBlocked
from eregion.runtime.auto_apply.contracts import ContinueApplication
from eregion.runtime.auto_apply.education import facts, gaps, normalize_records
from eregion.runtime.auto_apply.loop import ApplicationLoop
from eregion.runtime.auto_apply.memory import compatible
from eregion.runtime.auto_apply.repository import ApplyRepository
from eregion.runtime.auto_apply.resume_profile import ResumeFacts, extract_profile
from test_auto_apply import FakeBrowser, database, field, request, wait


RECORDS=normalize_records([
    dict(school='North University',degree='Masters',field_of_study='Systems Engineering',start_date='August 2024',end_date='May 2026'),
    dict(school='South College',degree='Bachelors',field_of_study='Computer Science',start_date='2020',end_date='June 2024')])


def test_configured_model_extraction_uses_agent_compatible_tool_transport(monkeypatch):
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import AIMessage
    from eregion.runtime.auto_apply.resume_profile import extract_facts
    model=ChatOpenAI(model='fixture',api_key='fixture-key');requests=[]
    def invoke(self,messages,**kwargs):
        requests.append(kwargs)
        return AIMessage(content='',tool_calls=[{'id':'facts','name':'ResumeFacts','args':{'facts':[],
            'education':[{'facts':[{'field':'school','value':'North University','quote':'North University'}]}]}}])
    monkeypatch.setattr(ChatOpenAI,'invoke',invoke)
    def unsupported(*args,**kwargs):raise AssertionError('Do not require provider-specific forced output.')
    monkeypatch.setattr(ChatOpenAI,'with_structured_output',unsupported)
    result=extract_facts(model,[('human','North University')])
    assert len(result.education)==1
    assert requests[0]['tools'][0]['function']['name']=='ResumeFacts'
    assert 'tool_choice' not in requests[0] and 'response_format' not in requests[0]


def test_candidate_api_preserves_pdf_on_profile_edits_and_extraction_is_read_only(database,monkeypatch):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from eregion.api.main import create_app
    import eregion.runtime.auto_apply.resume_profile as extraction
    loop=ApplicationLoop(ApplyRepository(database.session_factory),browser_factory=FakeBrowser)
    app=create_app(run_service=object(),session_runtime=SimpleNamespace(database=database));app.state.auto_apply=loop
    monkeypatch.setattr(extraction,'extract_profile',lambda _: {'profile':{'first_name':'Extracted'},'warning':None})
    with TestClient(app) as client:
        body={'profile':{**request().profile.model_dump(),'education':RECORDS},'resume_base64':request().resume_base64,'resume_name':'saved.pdf','extracted':True}
        assert client.post('/api/auto-apply/candidate',json=body).status_code==200
        assert client.post('/api/auto-apply/resume-profile',json={'resume_base64':request().resume_base64}).status_code==200
        assert client.get('/api/auto-apply/candidate').json()['profile']['first_name']=='Test'
        body['profile']['city']='Ithaca, NY'
        assert client.post('/api/auto-apply/candidate',json={'profile':body['profile']}).status_code==200
        saved=client.get('/api/auto-apply/candidate')
        assert saved.headers['cache-control']=='no-store'
        assert saved.json()['resume_base64']==request().resume_base64
        assert saved.json()['resume_name']=='saved.pdf' and saved.json()['profile']['city']=='Ithaca, NY'
        assert client.get('/api/auto-apply/candidate',headers={'Origin':'https://evil.example'}).status_code==403
        assert client.post('/api/auto-apply/candidate',json={**body,'resume_base64':'invalid'}).status_code==422
        assert client.delete('/api/auto-apply/candidate').status_code==200
        assert client.get('/api/auto-apply/candidate').json()['resume_base64'] is None


def test_legacy_resume_restores_once_and_clear_does_not_resurrect(database):
    repo=ApplyRepository(database.session_factory)
    state,_=repo.create(request())
    saved=repo.saved_candidate()
    assert saved['profile']['email']==state['profile']['email']
    assert saved['resume_base64']==request().resume_base64 and saved['needs_extraction']
    profile={**saved['profile'],'education':RECORDS}
    repo.save_candidate(profile,name='Candidate.pdf',extracted=True)
    restarted=ApplyRepository(database.session_factory)
    assert restarted.saved_candidate()['profile']['education']==RECORDS
    assert not restarted.saved_candidate()['needs_extraction']
    assert restarted.refresh_education(state)['profile']['education']==RECORDS
    restarted.clear_candidate()
    assert ApplyRepository(database.session_factory).saved_candidate()['resume_base64'] is None
    assert not repo.saved_candidate()['profile']['education']


def test_education_upgrade_requires_same_owner_and_resume(database):
    repo=ApplyRepository(database.session_factory);state,_=repo.create(request())
    repo.save_candidate({**state['profile'],'education':RECORDS},resume=b'%PDF-another')
    assert not repo.refresh_education(state)['profile']['education']
    repo.save_candidate({**state['profile'],'email':'other@example.com','education':RECORDS},resume=repo.resume(state['run_id']))
    assert not repo.refresh_education(state)['profile']['education']


def test_explicit_contact_correction_updates_saved_details_without_losing_education(database):
    repo=ApplyRepository(database.session_factory);state,_=repo.create(request())
    repo.save_candidate({**state['profile'],'city':'Old city','education':RECORDS},resume=repo.resume(state['run_id']),extracted=True)
    state=repo.save(state,fields=[field('city',label='Location (City)*')])
    repo.remember(state,{'city':'Ithaca, NY'})
    assert repo.saved_candidate()['profile']['city']=='Ithaca, NY'
    assert repo.saved_candidate()['profile']['education']==RECORDS
    assert not repo.saved_candidate()['needs_extraction']
    repo.remember(state,{'city':'Historical city'},historical=True)
    assert repo.saved_candidate()['profile']['city']=='Ithaca, NY'


def test_resume_extracts_every_education_with_evidence_and_date_precision():
    from reportlab.pdfgen.canvas import Canvas
    stream=BytesIO();canvas=Canvas(stream)
    lines=['Alex Demo','alex@example.com']+[str(v) for r in RECORDS for k,v in r.items() if k!='id' and v]
    for i,line in enumerate(lines):canvas.drawString(30,790-i*22,line)
    canvas.save()
    class Model:
        def with_structured_output(self,schema):return self
        def invoke(self,messages):
            return ResumeFacts(education=[{'facts':[{'field':k,'value':v,'quote':v} for k,v in r.items() if k!='id' and v]} for r in RECORDS]+[
                {'facts':[{'field':'school','value':'Invented School','quote':'Invented School'}]}])
    result=extract_profile(base64.b64encode(stream.getvalue()).decode(),model_factory=Model)
    assert result['profile']['education']==RECORDS
    sources=facts(result['profile']);bachelor=RECORDS[1]['id']
    assert sources[f'education:{bachelor}:start_year']=='2020'
    assert f'education:{bachelor}:start_month' not in sources
    assert sources[f'education:{bachelor}:end_month']=='6'


def test_multiple_answers_are_saved_and_resumed_as_one_batch(database):
    repo=ApplyRepository(database.session_factory);browser=FakeBrowser('')
    browser.fields=[field('English'),field('Source'),field('Consent','checkbox','I agree to recruitment data processing',False)]
    loop=ApplicationLoop(repo,browser_factory=lambda _:browser)
    try:
        run=loop.start(request())['run_id'];state=wait(loop,run)
        result=loop.continue_run(run,ContinueApplication(expected_version=state['version'],answers={'English':'Yes','Source':'LinkedIn','Consent':False}))
        assert result['answers']=={'English':'Yes','Source':'LinkedIn','Consent':False}
        assert result['messages'][-1]['binding_method']=='explicit_batch'
        assert len([m for m in result['messages'] if m['role']=='user'])==1
        assert wait(loop,run)['status']=='awaiting_review'
        assert browser.submissions==0
        assert len(repo.memories(state['profile']))==3
    finally:loop.close()


def test_memory_available_across_jobs_with_model_selected_paraphrase(database):
    repo=ApplyRepository(database.session_factory);old,_=repo.create(request())
    old=repo.save(old,fields=[field('old-source',label='Where did you find this opening?')])
    repo.remember(old,{'old-source':'LinkedIn'})
    new,_=repo.create(request(url='https://jobs.lever.co/second/new-job',idempotency_key='another-job'))
    context=repo.agent_context(new['run_id']);browser=FakeBrowser('')
    browser.fields=[field('new-source',label='How did you hear about us?')]
    session=AgentTools(browser,context,autofill=lambda:{},active=lambda:True)
    snapshot=session.observe();memory=snapshot['remembered_answers'][0]
    session.execute('use_remembered_answer',{'observation_id':snapshot['observation_id'],'field_key':'new-source',
        'memory_id':memory['memory_id'],'reason':'Both questions ask for the job discovery channel.'})
    assert desired_answer(browser.fields[0],context)=='LinkedIn'
    context['answers']['new-source']='University';context['answer_sources']['new-source']={'type':'user'}
    with pytest.raises(BrowserBlocked,match='explicit answer'):
        session.execute('use_remembered_answer',{'observation_id':session.observe()['observation_id'],'field_key':'new-source','memory_id':memory['memory_id'],'reason':'same'})
    assert not repo.memories({**new['profile'],'email':'different@example.com'})


@pytest.mark.parametrize('source,target',[
    ('Do you require sponsorship in the US?','Will you require sponsorship in the UK?'),
    ('Do you require sponsorship now?','Do you require sponsorship in the future?'),
    ('I consent to processing data for this application','I consent to marketing emails'),
    ('Have you previously worked for us?','Have you previously worked for us?')])
def test_memories_do_not_change_scope(source,target):
    memory={'source':'user_answer','question':source,'url':'https://jobs.lever.co/first/a'}
    assert not compatible(memory,field('q','select',target),{'profile':{},'url':'https://jobs.lever.co/second/b'})


EDUCATION_FORM='''<form id="application"><h1>My Experience</h1><section><h2>Education</h2>
<div id="rows"></div><button type="button" onclick="addEducation()">Add Education</button></section>
<button type="button">Next</button></form><script>
let number=0;function addEducation(){number++;const row=document.createElement('fieldset');row.innerHTML=`<legend>Education ${number}</legend>`;
for(const [part,label] of [['school','School'],['degree','Degree'],['major','Field of Study'],['start','Start year'],['endmonth','Graduation month'],['endyear','Graduation year']]){
const id=`${part}-${number}`;row.innerHTML+=`<label for="${id}">${label}</label><input id="${id}" ${part==='school'||part==='degree'?'required':''}>`;}
document.querySelector('#rows').append(row);}addEducation();</script>'''


@pytest.mark.skipif(os.getenv('JOB_AGENT_BROWSER_TESTS')!='1',reason='Opt-in real browser fixture')
def test_two_education_rows_include_optional_details_and_do_not_refill():
    from test_auto_apply_browser import FixtureBrowser
    browser=FixtureBrowser(request().url)
    state={'profile':{**request().profile.model_dump(),'education':RECORDS},'answers':{},'automation_mode':'hybrid'}
    try:
        browser.page.set_content(EDUCATION_FORM)
        session=AgentTools(browser,state,autofill=lambda:autofill(browser,state,lambda:b''),active=lambda:True)
        components={'school':'school','degree':'degree','major':'field_of_study','start':'start_year','endmonth':'end_month','endyear':'end_year'}
        for i,record in enumerate(RECORDS,1):
            if i>1:
                snapshot=session.observe();control=next(c for c in snapshot['page']['controls'] if c['kind']=='add_entry')
                session.execute('click_page_action',{'observation_id':snapshot['observation_id'],'control_id':control['id']})
            snapshot=session.observe()
            row=[f for f in browser.observe() if f['dom_id'].endswith('-'+str(i))]
            assert all(desired_answer(f,state) is None for f in row)
            session.execute('bind_education',{'observation_id':snapshot['observation_id'],'education_id':record['id'],
                'fields':[{'field_key':f['key'],'component':components[f['dom_id'].split('-')[0]]} for f in row]})
            result=session.execute('autofill',{})
            assert not result.get('failed')
            if i==1:
                snapshot=session.observe();control=next(c for c in snapshot['page']['controls'] if c['kind']=='next')
                with pytest.raises(BrowserBlocked,match='South College'):
                    session.execute('click_page_action',{'observation_id':snapshot['observation_id'],'control_id':control['id']})
        fields=browser.observe()
        assert not gaps(state,fields,browser.page_state())
        assert browser.page.locator('#major-2').input_value()=='Computer Science'
        assert browser.page.locator('#endyear-1').input_value()=='2026'
        assert browser.page.locator('#endmonth-2').input_value()=='6'
        assert autofill(browser,state,lambda:b'')['filled']==[]
        assert browser.page.locator('#rows > fieldset').count()==2
        browser.page.locator('#major-2').fill('')
        assert any(g.startswith('Field of Study') for g in gaps(state,browser.observe(),browser.page_state()))
        degree=next(f for f in fields if f['dom_id']=='degree-2');state['answers'][degree['key']]='B.Sc.'
        assert desired_answer(degree,state)=='B.Sc.'
    finally:browser.close()


@pytest.mark.skipif(os.getenv('JOB_AGENT_BROWSER_TESTS')!='1',reason='Opt-in real browser fixture')
def test_chat_ui_restores_resume_edits_education_and_batches_answers(tmp_path):
    import json
    from pathlib import Path
    from urllib.parse import urlsplit
    from test_auto_apply_browser import FixtureBrowser
    browser=FixtureBrowser(request().url);assets=Path(__file__).parents[1]/'frontend/web_app'
    saved={'profile':{**request().profile.model_dump(),'education':RECORDS},'resume_base64':request().resume_base64,'resume_name':'Saved.pdf','needs_extraction':False}
    questions=[field('english','select','Fluent in English?'),field('consent','checkbox','Recruitment data processing')]
    questions[0]['options']=[{'label':'Yes','value':'1'},{'label':'No','value':'0'}]
    attempt={'run_id':'fixture-run','url':request().url,'status':'needs_input','version':4,'fields':questions,
        'messages':[{'id':'question','role':'assistant','text':'Two questions','question_keys':['english','consent']}]}
    submissions=[];errors=[];browser.page.on('pageerror',lambda error:errors.append(str(error)))
    def route(r):
        path=urlsplit(r.request.url).path
        if path=='/apply':return r.fulfill(content_type='text/html',body=(assets/'apply.html').read_text(encoding='utf-8'))
        if path.startswith('/web-assets/'):
            target=assets/Path(path).relative_to('/web-assets')
            if target.exists():return r.fulfill(content_type='text/javascript' if target.suffix=='.js' else 'text/css',body=target.read_text(encoding='utf-8'))
        if path=='/api/auto-apply':return r.fulfill(json={'attempts':[attempt]})
        if path=='/api/auto-apply/candidate':
            if r.request.method=='POST':saved['profile']=r.request.post_data_json['profile']
            return r.fulfill(json=saved)
        if path=='/api/auto-apply/fixture-run/continue':
            submissions.append(r.request.post_data_json);attempt['status']='awaiting_review';attempt['version']+=1
            return r.fulfill(json=attempt)
        return r.fulfill(status=204)
    try:
        browser.page.route('**/*',route);browser.page.goto('https://jobs.lever.co/apply?mode=application')
        browser.page.get_by_text('Saved.pdf',exact=False).wait_for()
        browser.page.get_by_role('button',name='Your details',exact=True).click()
        assert browser.page.locator('.education-record').count()==2
        browser.page.locator('.education-record').nth(1).locator('[data-component=field_of_study]').fill('Computer Engineering')
        browser.page.get_by_role('button',name='Save details',exact=True).click()
        browser.page.locator('#profile-dialog').wait_for(state='hidden')
        browser.page.reload();browser.page.get_by_text('Saved.pdf',exact=False).wait_for()
        assert saved['profile']['education'][1]['field_of_study']=='Computer Engineering'
        browser.page.locator('#attempts button').first.click()
        browser.page.locator('.question-choice').nth(0).get_by_role('button',name='Yes',exact=True).click()
        assert submissions==[]
        assert browser.page.get_by_role('button',name='Submit answers & continue').is_disabled()
        browser.page.locator('.question-choice').nth(1).get_by_role('button',name='No',exact=True).click()
        # A normal poll must not clear an unsent selection.
        browser.page.wait_for_timeout(2200)
        assert browser.page.locator('.question-choice [aria-pressed=true]').count()==2
        browser.page.screenshot(path=str(tmp_path/'batch-answers.png'),full_page=True)
        browser.page.get_by_role('button',name='Submit answers & continue').click()
        browser.page.get_by_role('button',name='Submit application',exact=True).wait_for()
        assert len(submissions)==1 and submissions[0]['answers']=={'english':'Yes','consent':False}
        assert submissions[0]['expected_version']==4
        saved['profile']['city']='Ithaca, NY'
        browser.page.get_by_role('button',name='New application',exact=False).click()
        browser.page.get_by_role('button',name='Your details',exact=True).click()
        assert browser.page.locator('#profile-fields input[name=city]').input_value()=='Ithaca, NY'
        assert errors==[]
    finally:browser.close()


def test_resume_extracts_work_experience_with_evidence_and_cites_it_per_component(database):
    # W2/§13 (Adobe pilot): both jobs had to be typed in chat; the profile held no experience.
    from reportlab.pdfgen.canvas import Canvas
    from eregion.runtime.auto_apply.experience import facts as experience_facts
    from eregion.runtime.auto_apply.decisions import source_value
    jobs=[dict(title='Research Assistant',company='North University',location='Ithaca, NY',start_date='Aug 2026',current='Present'),
          dict(title='Data Analyst Intern',company='Example Group',location='Beijing, China',start_date='Jul 2022',end_date='Aug 2022')]
    stream=BytesIO();canvas=Canvas(stream)
    lines=['Alex Demo','alex@example.com']+[v for job in jobs for v in job.values()]
    for i,line in enumerate(lines):canvas.drawString(30,790-i*22,line)
    canvas.save()
    class Model:
        def with_structured_output(self,schema):return self
        def invoke(self,messages):
            return ResumeFacts(experience=[{'facts':[{'field':k,'value':v,'quote':v} for k,v in job.items()]} for job in jobs]+[
                {'facts':[{'field':'company','value':'Invented Corp','quote':'Invented Corp'}]}])
    result=extract_profile(base64.b64encode(stream.getvalue()).decode(),model_factory=Model)
    records=result['profile']['experience']
    assert [(r['title'],r['company'],r['current']) for r in records]==[('Research Assistant','North University','Present'),('Data Analyst Intern','Example Group','')]
    sources=experience_facts(result['profile']);intern=records[1]['id']
    assert sources[f'experience:{intern}:start_month']=='7' and sources[f'experience:{intern}:end_year']=='2022'
    # Saving keeps the records; a cited component resolves to the evidenced value.
    repo=ApplyRepository(database.session_factory)
    saved=repo.save_candidate({**request().profile.model_dump(),'experience':records})
    assert saved['experience']==records
    assert source_value({'profile':saved},f'experience:{intern}:company')=='Example Group'
