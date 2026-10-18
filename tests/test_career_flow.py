"""Complete chat flow with real persistence/domain services and synthetic model/browser IO."""
import base64
import json
import os
import time
from io import BytesIO
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pypdf import PdfReader

from eregion.api.db import create_database, upgrade_database
from eregion.api.services.fit_analysis_service import FitAnalysisService
from eregion.domain.workflow.handlers import JobAgentStepHandler
from eregion.runtime.tasks.loop import TaskLoop, function
from eregion.runtime.career.repository import TaskRepository
from eregion.runtime.career.harness import CareerHarness
from eregion.runtime.career.service import CareerService
from eregion.runtime.career.discovery import JobDiscovery
from eregion.runtime.auto_apply.repository import ApplyRepository
from eregion.runtime.auto_apply.loop import ApplicationLoop
from eregion.runtime.auto_apply.contracts import ChatRequest
from eregion.runtime.application_pack.repository import PackRepository
from eregion.runtime.application_pack.workflow import ApplicationPackWorkflow
from eregion.runtime.context.repository import ContextSnapshotRepository
from eregion.runtime.evidence.repository import CareerEvidenceRepository
from eregion.runtime.memory.repository import MemoryRepository
from eregion.runtime.mock_interview.controller import MockInterviewController
from eregion.runtime.mock_interview.repository import MockInterviewRepository
from eregion.runtime.sessions.repository import SessionRepository
from eregion.runtime.workspace.repository import JobWorkspaceRepository
from eregion.domain.pdf_rendering import render_letter_pdf
from test_auto_apply import FakeBrowser, wait
from test_application_pack import FakePackModel
from test_job_workflow import fake_analyze as workflow_analyze
from test_mock_interview import ScriptedModel as InterviewModel


def fake_analyze(schema, system, human):
    from eregion.domain.schemas import JobAnalysis, JobRequirement
    if schema is JobAnalysis:
        # Explicit model stand-in for these synthetic workflow fixtures. This is
        # not a semantic-quality oracle or a production extraction fallback.
        text = 'Python' if 'Python' in human else 'SQL'
        assert text in human
        return JobAnalysis(title='Engineer', summary='', responsibilities=[], requirements=[JobRequirement(
            requirement_id='fixture', requirement_group_id='fixture', canonical_name=text.lower(),
            display_name=text, original_text=text, source_text=text, atomic_text=text,
            category='skill', verification_mode='resume_evidence', level='required')])
    return workflow_analyze(schema, system, human)


class ToolModel:
    def __init__(self, decide):self.decide,self.calls=decide,0
    def invoke(self,messages,tools):
        self.calls+=1
        snapshot=json.loads(next(m.content for m in reversed(messages) if isinstance(m,HumanMessage)))
        name,args=self.decide(snapshot)
        return AIMessage(content='',tool_calls=[{'name':name,'args':args,'id':str(uuid4())}])


def settled(repo,identifier,statuses=('needs_input','completed','waiting')):
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        state=repo.get(identifier)
        if state['status'] in statuses:return state
        time.sleep(.01)
    raise AssertionError(state)


class Discovery:
    def search(self,**_):return {'jobs':[self.job('https://jobs.lever.co/example/abc123')]}
    def job(self,url):return {'url':url,'title':'Python Engineer','company':'Example','location':'Remote',
        'description':'Requires Python APIs.'}


@pytest.fixture
def career(tmp_path, monkeypatch):
    # Historical browser-route fixture; API/MCP tests explicitly opt into the new default route.
    monkeypatch.setenv('JOB_AGENT_JOB_API_FIRST', '0')
    # This fixture scripts discovery, scoring and writing; screening must be offline too.
    monkeypatch.setenv('TYPESAFE_ENABLED', 'false')
    from eregion.runtime.career import qualification
    from fake_search_qualification import judge
    monkeypatch.setattr(qualification,'judge',judge)
    url=f"sqlite:///{(tmp_path/'career.db').as_posix()}"
    upgrade_database(url)
    db=create_database(url)
    workspace=JobWorkspaceRepository(db.session_factory)
    evidence=CareerEvidenceRepository(db.session_factory)
    packs=PackRepository(db.session_factory)
    workflow=ApplicationPackWorkflow(packs=packs,workspace=workspace,evidence=evidence,
        memories=MemoryRepository(db.session_factory),model=FakePackModel())
    interviews=MockInterviewController(interviews=MockInterviewRepository(db.session_factory),
        sessions=SessionRepository(db.session_factory),workspace=workspace,packs=packs,evidence=evidence,
        context_snapshots=ContextSnapshotRepository(db.session_factory),model=InterviewModel())
    runtime=SimpleNamespace(database=db,workspace=workspace,evidence=evidence,packs=packs,
        pack_workflow=workflow,mock_interviewer=interviews)
    repository=TaskRepository(db.session_factory)
    browsers=[]
    def browser(url):
        instance=FakeBrowser(url);browsers.append(instance);return instance
    applications=ApplicationLoop(ApplyRepository(db.session_factory),browser_factory=browser)
    # Avoid a real application model while retaining its actual autofill/review/submit loop.
    real_start=applications.start
    applications.start=lambda request,**kwargs:real_start(request.model_copy(update={'automation_mode':'autofill'}),**kwargs)
    original=render_letter_pdf('Synthetic Candidate\nsynthetic@example.com\nEXPERIENCE\nBuilt Python APIs.')
    applications.repository.save_candidate({'first_name':'Synthetic','last_name':'Candidate','email':'synthetic@example.com'},
        resume=original,name='original.pdf',extracted=True)
    harness=CareerHarness(runtime,applications,repository,discovery=Discovery(),
        fit_service=FitAnalysisService(JobAgentStepHandler(analyzer=fake_analyze)))
    state,_=repository.create('Find Python jobs, score, tailor, apply, and practice interviews.','synthetic-flow')
    yield SimpleNamespace(runtime=runtime,repository=repository,harness=harness,state=state,
        applications=applications,browsers=browsers,original=original)
    applications.close();db.close()


def execute(c,name,**args):return c.harness.execute(c.state['task_id'],name,args,call_id=str(uuid4()))


def test_scoring_reuses_extractions_across_jobs_and_conversations(career,monkeypatch):
    from eregion.domain.schemas import ResumeAnalysis, JobAnalysis, SkillAssessment
    # Counts model job extraction per posting and verifies warm reuse.
    monkeypatch.setenv('JOB_AGENT_MAIN_CATALOG','extract')

    c=career
    calls=[]
    namespace=['model-a']
    def analyze(schema,*args):
        calls.append(schema)
        return fake_analyze(schema,*args)
    c.harness.analysis._fit_service=FitAnalysisService(
        JobAgentStepHandler(analyzer=analyze),cache_namespace=lambda:namespace[0])
    first=execute(c,'import_job',url='https://jobs.lever.co/example/abc123')
    score=execute(c,'score_job',application_id=first['application_id'])
    assert score['match_score'] is not None
    assert calls.count(ResumeAnalysis)==calls.count(JobAnalysis)==calls.count(SkillAssessment)==1

    # Another posting has a different JD, while the saved resume is unchanged.
    second=c.runtime.workspace.save_workspace(cleaned_job_description='Requires Python APIs. SQL preferred.',
        source_url='https://jobs.lever.co/example/second',title='Python Engineer')
    c.repository.link(c.state['task_id'],'jobs',second.application.application_id)
    execute(c,'score_job',application_id=second.application.application_id)
    assert calls.count(ResumeAnalysis)==1
    assert calls.count(JobAnalysis)==calls.count(SkillAssessment)==2

    task,_=c.repository.create('Score the saved job again.','another-scoring-conversation',
        context={'application_id':first['application_id']})
    repeated=c.harness.execute(task['task_id'],'score_job',
        {'application_id':first['application_id']},call_id=str(uuid4()))
    assert repeated['match_score']==score['match_score']
    assert repeated['match_report']==score['match_report']
    assert len(calls)==5  # The new conversation reuses the completed analysis.

    # The conversation shortcut must not bypass changed model configuration.
    namespace[0]='model-b'
    execute(c,'score_job',application_id=first['application_id'])
    assert len(calls)==8
    execute(c,'score_job',application_id=first['application_id'])
    assert len(calls)==8


def test_general_visual_tools_require_task_opt_in(career):
    c=career
    names=lambda:{t['function']['name'] for t in c.harness.available_tools(c.repository.get(c.state['task_id']))}
    assert 'browser_screenshot' not in names()
    with pytest.raises(ValueError,match='Visual browser backup'):execute(c,'browser_screenshot')
    c.repository.change(c.state['task_id'],visual_fallback=True)
    # Visual tools are advertised only after a browser is actually open.
    c.repository.change(c.state['task_id'],browser={'url':'https://example.com/'})
    c.harness._browsing=SimpleNamespace(inspect=lambda _:True,close=lambda:None)
    assert {'browser_screenshot','browser_click_at'}<=names()


def prepare(c):
    found=execute(c,'search_jobs',query='Python')['jobs'][0]
    execute(c,'read_job',url=found['url'])
    job=execute(c,'import_job',url=found['url'])
    scored=execute(c,'score_job',application_id=job['application_id'])
    assert scored['match_score'] is not None
    pack=execute(c,'generate_materials',application_id=job['application_id'],artifacts=['tailored_resume','cover_letter'])
    return job,pack


def approve(c,pack):
    for item in c.runtime.packs.items(pack['pack_id']):
        assert item.status.value=='awaiting_review',item
        c.runtime.packs.review(pack['pack_id'],item.pack_item_id,expected_version=item.version,
            approve=True,idempotency_key=f'approve-{item.pack_item_id}')


def test_decision_context_has_material_status_and_full_documents_remain_inspectable(career):
    c=career
    job,pack=prepare(c)
    snapshot=c.harness.observe(c.repository.get(c.state['task_id']))
    summary=snapshot['materials'][0]
    assert summary['content_available_via']=='inspect_materials'
    assert all('text' not in item for item in summary['items'])
    assert all(item['verification']['passed'] for item in summary['material_overview'])
    details=execute(c,'inspect_materials',pack_id=pack['pack_id'])
    assert all(item['text'] for item in details['items'])
    assert [(i['pack_item_id'],i['status'],i['version']) for i in summary['items']]==[
        (i['pack_item_id'],i['status'],i['version']) for i in details['items']]
    assert len(json.dumps(summary))<len(json.dumps(details))


def test_saved_job_start_validates_context_before_scheduling_and_fences_retries(career):
    from eregion.runtime.workspace.errors import ApplicationNotFoundError
    c=career
    job=execute(c,'import_job',url='https://jobs.lever.co/example/abc123')
    other=execute(c,'import_job',url='https://jobs.lever.co/example/other')
    c.repository.change(c.state['task_id'],status='cancelled',pause_requested=True)
    model=ToolModel(lambda _:('ask_user',{'text':'What would you like to do with this job?','wait_for_reply':True}))
    service=CareerService(c.runtime,c.applications,harness=c.harness,model_factory=lambda:model)
    try:
        before=len(c.repository.list())
        with pytest.raises(ApplicationNotFoundError):service.start('Review this job.','missing-job-start',application_id='missing')
        assert len(c.repository.list())==before and model.calls==0
        started=service.start('Review this job.','saved-job-start',application_id=job['application_id'])
        state=settled(c.repository,started['run_id'])
        assert state['jobs']==[job['application_id']]
        assert not state['application_runs'] and model.calls==1
        retry=service.start('Review this job.','saved-job-start',application_id=job['application_id'])
        assert retry['run_id']==started['run_id'] and model.calls==1
        with pytest.raises(ValueError,match='another goal or saved job'):
            service.start('Review this job.','saved-job-start',application_id=other['application_id'])
    finally:service.close()


def test_extension_analysis_reuses_chat_resume_without_old_settings_page(career):
    from hashlib import sha256
    from eregion.runtime.resumes.repository import ResumeDocumentRepository
    from eregion.api.resume_routes import _analysis_resume

    c=career
    c.runtime.resumes=ResumeDocumentRepository(c.runtime.database.session_factory)
    c.runtime.owner_resolver=SimpleNamespace(resolve=lambda:SimpleNamespace(owner_id='synthetic-owner'))
    first=_analysis_resume(c.runtime,None)
    assert first.content_sha256==sha256(c.original).hexdigest()
    assert first.extracted_text and first.is_default
    assert _analysis_resume(c.runtime,None).resume_id==first.resume_id
    updated=render_letter_pdf('Updated Synthetic Candidate\nBuilt Python and SQL APIs.')
    c.applications.repository.save_candidate({'first_name':'Synthetic'},resume=updated,name='updated.pdf',extracted=True)
    second=_analysis_resume(c.runtime,None)
    assert second.resume_id!=first.resume_id and 'SQL' in second.extracted_text
    assert _analysis_resume(c.runtime,first.resume_id).resume_id==first.resume_id
    assert c.runtime.resumes.default('synthetic-owner').resume_id==second.resume_id


def test_account_wait_stays_visible_and_parent_only_receives_safe_summary(career):
    c=career
    run=execute(c,'open_application_url',url='https://jobs.lever.co/example/abc123')
    child=wait(c.applications,run['run_id'])
    account={'origin':'https://jobs.lever.co','mode':'login','stage':'password',
        'instructions':'PRIVATE ACCOUNT PAGE','consents':[]}
    c.applications.repository.save(child,status='needs_input',account_request=account,
        message='Use the secure account form.',fields=[{'key':'account-user','value':'PRIVATE IDENTIFIER'}])
    c.repository.change(c.state['task_id'],status='needs_input',input_request={
        'id':'parent-question','text':'An old parent question','fields':[],'field_keys':[]})
    service=CareerService.__new__(CareerService)
    service.runtime,service.applications,service.repository,service.harness=c.runtime,c.applications,c.repository,c.harness
    view=service.view(c.state['task_id'])
    assert view['account_request']==account and view['input_request'] is None
    assert view['message']=='Use the secure account form.'
    snapshot=c.harness.application(run['run_id'])
    assert 'PRIVATE' not in json.dumps(snapshot)
    token=c.harness.wait_token({'id':run['run_id']})
    c.applications.repository.save(c.applications.repository.get(run['run_id']),
        account_request={**account,'mode':'register'})
    assert c.harness.wait_token({'id':run['run_id']})!=token


def test_discovery_score_documents_application_and_interview_share_one_task(career):
    c=career
    job,pack=prepare(c)
    with pytest.raises(ValueError,match='review'):
        execute(c,'start_application',application_id=job['application_id'],pack_id=pack['pack_id'])
    approve(c,pack)
    run=execute(c,'start_application',application_id=job['application_id'],pack_id=pack['pack_id'])
    review=wait(c.applications,run['run_id'])
    assert review['status']=='awaiting_review'
    assert c.browsers[0].submissions==0
    assert c.applications.repository.resume(run['run_id'])!=c.original
    assert base64.b64decode(c.applications.repository.saved_candidate()['resume_base64'])==c.original
    assert 'Built Python APIs.' in review['cover_letter']
    service=CareerService.__new__(CareerService)
    service.runtime,service.applications,service.repository,service.harness=c.runtime,c.applications,c.repository,c.harness
    service.loop=SimpleNamespace(chat=lambda *a,**k:None)
    view=service.view(c.state['task_id'])
    assert view['career'] and view['jobs'][0]['match_score'] is not None
    assert len(view['materials'][0]['items'])==2
    with pytest.raises(ValueError,match='changed'):
        service.application_action(c.state['task_id'],'continue',{'application_run_id':'wrong','expected_version':review['version'],'confirm_submit':True})
    service.application_action(c.state['task_id'],'continue',{'application_run_id':run['run_id'],
        'expected_version':review['version'],'confirm_submit':True})
    assert wait(c.applications,run['run_id'])['status']=='submitted'
    assert service.view(c.state['task_id'])['jobs'][0]['status']=='applied'
    assert c.browsers[0].submissions==1
    interview=execute(c,'start_interview',application_id=job['application_id'],question_count=3)
    identifier=interview['interview']['mock_interview_id']
    question=interview['question']['question_id']
    c.repository.append(c.state['task_id'],role='user',id='answer-user',text='Built Python APIs.')
    feedback=execute(c,'answer_interview',interview_id=identifier,question_id=question,message_id='answer-user')
    # Career answers now return after durable dispatch/next-question generation.
    # Observe the actual saved job/evaluation, rather than assume synchronous feedback.
    deadline=time.monotonic()+12
    while time.monotonic()<deadline:
        jobs=c.runtime.mock_interviewer.interviews.feedback_jobs(identifier)
        if any(j['status']=='failed' for j in jobs):raise AssertionError(jobs)
        if jobs and all(j['status']=='done' for j in jobs):
            feedback=c.harness.interview(identifier)
            if feedback.get('latest_feedback'):break
        time.sleep(.02)
    assert jobs and all(j['status']=='done' for j in jobs),jobs
    assert feedback['latest_feedback']['exact_answer_quotes']==['Built Python APIs.']
    replay=execute(c,'answer_interview',interview_id=identifier,question_id=question,message_id='answer-user')
    assert replay['interview']['version']==feedback['interview']['version']
    assert len([t for t in c.runtime.mock_interviewer.interviews.turns(identifier) if t['answer']])==1
    c.repository.append(c.state['task_id'],role='user',id='end-user',text='End the interview.')
    result=execute(c,'control_interview',interview_id=identifier,action='end',message_id='end-user')
    assert result['report']['question_answer_summaries'][0]['answer']=='Built Python APIs.'
    restored=TaskRepository(c.runtime.database.session_factory).get(c.state['task_id'])
    assert restored['jobs']==[job['application_id']] and restored['interviews']==[identifier]


def test_changed_materials_replace_existing_attempt_and_clear_review(career):
    c=career;job,pack=prepare(c);approve(c,pack)
    original=execute(c,'start_application',application_id=job['application_id'],resume_source='original')
    first=wait(c.applications,original['run_id'])
    assert first['review_digest']
    changed=execute(c,'start_application',application_id=job['application_id'],pack_id=pack['pack_id'])
    assert changed['run_id']==original['run_id']
    latest=wait(c.applications,changed['run_id'])
    assert latest['status']=='awaiting_review'
    assert len(c.browsers)==2 and c.browsers[0].closed
    assert c.applications.repository.resume(changed['run_id'])!=c.original
    assert all(b.submissions==0 for b in c.browsers)


def test_unknown_submission_cannot_be_relabelled_as_new_materials(career):
    c=career;job,pack=prepare(c);approve(c,pack)
    run=execute(c,'start_application',application_id=job['application_id'],resume_source='original')
    old=wait(c.applications,run['run_id'])
    c.applications.repository.save(old,status='submission_unknown')
    execute(c,'start_application',application_id=job['application_id'],pack_id=pack['pack_id'])
    state=c.repository.get(c.state['task_id'])
    assert state['application_links'][run['run_id']]['materials']=={}
    assert c.applications.repository.resume(run['run_id'])==c.original
    c.harness.sync_submissions(state)
    assert c.runtime.workspace.get_application(job['application_id']).status.value!='applied'


def test_model_can_start_with_interview_without_enforcing_full_pipeline(career):
    c=career;job,_=prepare(c)
    def decide(s):
        if not s['interviews']:return 'start_interview',{'application_id':job['application_id'],'question_count':3}
        return 'ask_user',{'text':s['interviews'][0]['question']['question_text'],'wait_for_reply':True}
    model=ToolModel(decide)
    loop=TaskLoop(c.repository,c.harness,model_factory=lambda:model)
    try:
        state=settled(c.repository,c.state['task_id'])
        assert state['input_request']['text']=='What did you personally do?'
        assert state['application_runs']==[] and model.calls==2
    finally:loop.close()


def test_direct_application_does_not_require_or_invent_job_analysis(career):
    c=career
    c.harness.discovery.job=lambda _:pytest.fail('Direct browser entry must not require description extraction.')
    run=execute(c,'open_application_url',url='https://example.wd5.myworkdayjobs.com/en-US/External/job/Engineer_JR123')
    review=wait(c.applications,run['run_id'])
    assert review['status']=='awaiting_review'
    state=c.repository.get(c.state['task_id'])
    assert state['jobs']==[] and state['packs']==[]
    assert state['active_application_run']==run['run_id']
    c.harness.sync_submissions(state)
    assert c.applications.repository.resume(run['run_id'])==c.original


def test_feedback_invalidates_pending_decision_and_pause_survives(career):
    c=career;entered,release=Event(),Event()
    def decide(s):
        if not entered.is_set():
            entered.set();assert release.wait(5)
            return 'say',{'text':'STALE RESPONSE'}
        return 'ask_user',{'text':'I read your new preference. What city?','wait_for_reply':True}
    loop=TaskLoop(c.repository,c.harness,model_factory=lambda:ToolModel(decide))
    try:
        assert entered.wait(5)
        loop.chat(c.state['task_id'],'Actually focus on New York.','new-feedback')
        release.set()
        state=settled(c.repository,c.state['task_id'])
        assert all(m['text']!='STALE RESPONSE' for m in state['messages'])
        loop.chat(c.state['task_id'],'pause','pause-msg')
        loop.chat(c.state['task_id'],'Here is an additional detail.','detail-msg')
        assert c.repository.get(c.state['task_id'])['pause_requested']
    finally:release.set();loop.close()


def test_search_public_board_shapes_and_private_redirect_are_checked():
    requested=[]
    def serve(request):
        requested.append(str(request.url))
        if request.url.host=='boards-api.greenhouse.io':return httpx.Response(200,json={'jobs':[
            {'absolute_url':'https://job-boards.greenhouse.io/example/jobs/123','title':'Python Engineer','location':{'name':'Remote'}}]})
        if request.url.host=='api.lever.co':return httpx.Response(200,json=[
            {'hostedUrl':'https://jobs.lever.co/example/abc','text':'Python Engineer','categories':{'location':'Remote'}}])
        return httpx.Response(302,headers={'location':'https://127.0.0.1/private'})
    discovery=JobDiscovery(transport=httpx.MockTransport(serve),resolver=lambda *a,**k:[(None,None,None,None,('8.8.8.8',443))])
    results=discovery.search('Python',boards=[{'platform':'greenhouse','company':'example'},{'platform':'lever','company':'example'}])
    assert len(results['jobs'])==2 and results['errors']==[]
    with pytest.raises(ValueError):discovery.read('https://example.com/redirect')
    assert all('127.0.0.1' not in url for url in requested)


def test_cover_letter_pdf_preserves_text():
    pdf=render_letter_pdf('Dear Hiring Team,\n\nBuilt Python APIs.\n\nSynthetic Candidate')
    text='\n'.join(p.extract_text() for p in PdfReader(BytesIO(pdf)).pages)
    assert 'Built Python APIs.' in text and 'Synthetic Candidate' in text


def test_selected_documents_replace_server_saved_attachments_once(career):
    from eregion.runtime.auto_apply.autofill import autofill,pending_documents
    from test_auto_apply import field
    c=career;browser=FakeBrowser('');uploads=[]
    browser.fields=[field('resume','file',label='CV'),field('support','file',label='Supporting document')]
    for f in browser.fields:f['value']='old-file.pdf'
    original_fill=browser.fill
    def fill(f,value):uploads.append((f['upload_filename'],value));original_fill(f,value)
    browser.fill=fill
    state={'answers':{},'profile':{},'cover_letter':'New approved cover letter.',
        'document_refresh':{'resume':True,'cover_letter':True},'document_bindings':{'support':'cover_letter'}}
    assert pending_documents(state,browser.fields)==['resume','cover_letter']
    assert len(autofill(browser,state,lambda:c.original)['filled'])==2
    assert pending_documents(state,browser.fields)==[]
    autofill(browser,state,lambda:c.original)
    assert len(uploads)==2
    assert uploads[0]==('resume.pdf',c.original)
    assert uploads[1][0]=='cover-letter.pdf'
    assert 'New approved cover letter.' in PdfReader(BytesIO(uploads[1][1])).pages[0].extract_text()


def test_model_can_identify_unfamiliar_document_control_but_cannot_skip_upload(career):
    from eregion.runtime.auto_apply.agent import AgentTools
    from eregion.runtime.auto_apply.browser import BrowserBlocked
    from test_auto_apply import field
    c=career;browser=FakeBrowser('');browser.fields=[field('attachment','file',label='Your document')]
    state={'profile':{},'answers':{},'document_refresh':{'resume':True}}
    tools=AgentTools(browser,state,autofill=lambda:{},active=lambda:True)
    snapshot=tools.observe()
    with pytest.raises(BrowserBlocked,match='Upload|complete the option path'):
        tools.execute('ready_for_review',{})
    tools.execute('bind_document',{'observation_id':snapshot['observation_id'],'field_key':'attachment','document':'resume'})
    assert tools.state['document_bindings']['attachment']=='resume'


def test_exact_user_evidence_is_reusable_and_model_inventions_are_rejected(career):
    c=career;job=execute(c,'import_job',url='https://jobs.lever.co/example/abc123')
    c.repository.append(c.state['task_id'],role='user',id='fact-msg',text='I also built a Python queue worker.')
    result=execute(c,'remember_evidence',message_id='fact-msg',quote='I also built a Python queue worker.',application_id=job['application_id'])
    assert result['evidence_id'] in [e.evidence_id for e in c.runtime.evidence.list_for_application(job['application_id'])]
    assert execute(c,'remember_evidence',message_id='fact-msg',quote='I also built a Python queue worker.',application_id=job['application_id'])['evidence_id']==result['evidence_id']
    with pytest.raises(ValueError,match='exact'):
        execute(c,'remember_evidence',message_id='fact-msg',quote='I led 500 engineers.')


def test_unreadable_legacy_pdf_does_not_prevent_job_search(career):
    c=career
    c.applications.repository.save_candidate({'first_name':'Synthetic'},resume=b'%PDF-invalid',extracted=True)
    observed=c.harness.observe(c.state)
    assert observed['resume_text']=='' and observed['resume_issue']
    assert execute(c,'search_jobs',query='Python')['jobs']


@pytest.mark.skipif(os.getenv('JOB_AGENT_LIVE_CAREER_TEST')!='1',reason='Opt-in configured model; synthetic resume and local tool IO only.')
def test_configured_model_directs_real_career_tools_with_synthetic_sources(career):
    from eregion.runtime.tasks.loop import TaskModel
    from eregion.runtime.auto_apply.browser import ApplicationBrowser
    from eregion.runtime.browser.manager import BrowsingManager
    c=career
    # The model may choose general browsing instead of read_job. Both routes
    # must see the same synthetic posting; never navigate to the real fake URL.
    class SyntheticPostingBrowser(ApplicationBrowser):
        def __init__(self,url):super().__init__(url,purpose='browse',headless=True)
        def _guard_request(self,route):
            if route.request.url.rstrip('/')=='https://jobs.lever.co/example/abc123':
                route.fulfill(status=200,content_type='text/html',body=(
                    '<title>Python Engineer at Example</title><h1>Python Engineer</h1>'
                    '<p>Example Company. Remote.</p><p>Requires Python APIs. Build Python services, '
                    'collaborate with the team on API design, maintain reliable backend systems, '
                    'and write tests and documentation for production services.</p>'))
            else:route.abort()
    c.harness._browsing=BrowsingManager(factory=SyntheticPostingBrowser)
    goal='Use my saved resume to read https://jobs.lever.co/example/abc123, compute my match score, and prepare a tailored resume and cover letter. Stop when the drafts are ready for my review. Do not apply.'
    c.repository.change(c.state['task_id'],goal=goal,messages=[{'id':'synthetic-goal','role':'user','text':goal}])
    loop=TaskLoop(c.repository,c.harness,model_factory=TaskModel,budget=20)
    try:
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            state=c.repository.get(c.state['task_id'])
            if state['status'] in {'needs_input','completed'}:break
            time.sleep(.1)
        print('Synthetic model check:',state['status'],[h['tool'] for h in state['history']])
        assert state['packs'],[(h['tool'],h['result'].get('error')) for h in state['history']]
        assert len(c.runtime.packs.items(state['packs'][0]))==2
        assert not state['application_runs']
    finally:
        loop.close()
        c.harness.close()


def test_job_card_summary_counts_requirements_and_lists_unique_gaps():
    report={'matches':[{'match_status':'matched'},{'match_status':'matched'},{'match_status':'partial'},
        {'match_status':'missing'},{'match_status':'needs_confirmation'},'not-a-match'],
        'missing_required_requirements':[{'canonical_name':'Kubernetes'},{'canonical_name':' '},{'canonical_name':'Kubernetes'}],
        'missing_preferred_requirements':[{'canonical_name':'Go'},{'canonical_name':'Rust'},{'canonical_name':'Scala'}]}
    summary=CareerHarness.requirement_summary(report)
    assert summary=={'requirement_counts':{'matched':2,'partial':1,'missing':1,'needs_confirmation':1,'unknown':0,'confirmed_condition':0},
        'gaps':['Kubernetes','Go','Rust']}
    assert CareerHarness.requirement_summary({'overall_score':40})=={'requirement_counts':None,'gaps':[]}


def test_rereading_a_waiting_application_in_one_turn_is_marked_unchanged(career,monkeypatch):
    # Intel-31: while sign-in waited on the user, one turn polled inspect/say three times.
    c=career
    monkeypatch.setattr(c.harness,'application',lambda run_id,**kwargs:{'run_id':run_id,'status':'needs_input','version':7})
    c.repository.change(c.state['task_id'],lambda s:{'application_runs':[*s['application_runs'],'run-1']})
    assert 'unchanged_since_last_inspection' not in execute(c,'inspect_application',run_id='run-1')
    again=execute(c,'inspect_application',run_id='run-1')
    assert again['unchanged_since_last_inspection'] and 'Do not inspect again' in again['next']
    c.repository.change(c.state['task_id'],lambda s:{'chat_revision':s['chat_revision']+1})
    assert 'unchanged_since_last_inspection' not in execute(c,'inspect_application',run_id='run-1')


def test_application_owned_questions_hold_the_parent_and_form_replies_route_back(career,monkeypatch):
    c=career
    child={'status':'needs_input','input_request':{'id':'q1','text':'1) Are you 18 or older?'},'agent_reason':'ask_user'}
    monkeypatch.setattr(c.applications.repository,'get',lambda run_id:child)
    waiting={'wait':{'kind':'application','id':'run-1'}}
    assert c.harness.hold_wait(waiting)
    child['agent_reason']='application_budget';assert not c.harness.hold_wait(waiting)  # a limit needs the parent
    child.update(agent_reason='ask_user',paused_for_control=True);assert not c.harness.hold_wait(waiting)
    child.update(paused_for_control=False,input_request=None,account_request={'origin':'https://x'});assert c.harness.hold_wait(waiting)
    child['status']='running';assert not c.harness.hold_wait(waiting)
    child['status']='queued';linked={'application_runs':['run-1']}
    routed=c.harness.route_application_reply(linked,{'text':'1 Yes','context':{'delivered_to_application':'run-1'}})
    assert routed['wait']=={'kind':'application','id':'run-1'} and routed['result']['direct']
    assert c.harness.route_application_reply(linked,{'text':'1 Yes'}) is None
    assert c.harness.route_application_reply({'application_runs':[]},{'text':'1 Yes','context':{'delivered_to_application':'run-1'}}) is None
