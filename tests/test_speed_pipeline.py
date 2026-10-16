"""Regression checks for mechanical application steps and shared analysis reuse."""
import json
import os
import time
from contextlib import contextmanager
from threading import Barrier
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from eregion.api.db import create_database
from eregion.api.services.fit_analysis_service import FitAnalysisService
from eregion.domain.workflow.handlers import JobAgentStepHandler
from eregion.domain.workflow.state import AgentState, Step
from eregion.runtime.analysis_cache import AnalysisCache
from eregion.runtime.fast_decisions import FastDecisions
from eregion.domain.schemas import ResumeAnalysis, ResumeEvidence, JobAnalysis, JobRequirement, SkillAssessment, SkillEvidence

class Analyzer:
    def __init__(self): self.calls = []; self.version = 'v1'
    def cache_namespace(self): return self.version
    def __call__(self, schema, system, human):
        self.calls.append(schema)
        if schema is ResumeAnalysis:
            return ResumeAnalysis(summary='Developer', skills=['Python'], education=[],
                evidence=[ResumeEvidence(evidence_id='e', source_section='Skills', exact_text='Python')])
        if schema is JobAnalysis:
            return JobAnalysis(title='Engineer', summary='Role', responsibilities=[], requirements=[
                JobRequirement(requirement_id='r', canonical_name='python', original_text=human,
                    source_text=human, atomic_text=human, level='required')])
        if schema is SkillAssessment:
            job = JobAnalysis.model_validate_json(human.split('\n\nJob analysis:\n')[1])
            return SkillAssessment(matches=[SkillEvidence(requirement_id=r.requirement_id,
                job_skill=r.canonical_name, requirement_level=r.level, match_status='matched',
                resume_evidence=['Python'], confidence=1) for r in job.requirements], explanation='Grounded', recommendations=[])
        raise AssertionError(schema)

def test_fit_cache_survives_restart_and_is_reused_by_workflow(tmp_path, monkeypatch):
    monkeypatch.setenv('TYPESAFE_ENABLED', 'false')
    monkeypatch.setenv('JOB_AGENT_MAIN_CATALOG', 'extract')  # counts model job extraction calls
    db = create_database('sqlite:///' + (tmp_path/'analysis.db').as_posix(), create_schema_for_tests=True)
    analyzer = Analyzer()
    def handler():
        return JobAgentStepHandler(analyzer=analyzer, analysis_cache=AnalysisCache(db.session_factory))
    try:
        fit = FitAnalysisService(handler())._analyze_sync(resume_text='Skills\nPython', job_description='Requires Python')
        assert len(analyzer.calls) == 3
        warm = FitAnalysisService(handler())._analyze_sync(resume_text='Skills\nPython', job_description='Requires Python')
        assert warm.model_calls == 0 and set(warm.cache_hits) == {'resume', 'job', 'match'}
        state = AgentState(run_id='tailor', resume_text='Skills\nPython', job_description='Requires Python')
        h = handler()
        for step in (Step.ANALYZE_RESUME, Step.VALIDATE_EVIDENCE, Step.ANALYZE_JOB, Step.MATCH_SKILLS):
            state = state.model_copy(update=h.execute(step, state).updates)
        assert len(analyzer.calls) == 3
        changed = FitAnalysisService(handler())._analyze_sync(resume_text='Skills\nPython', job_description='Python required.')
        assert changed.cache_hits == ['resume'] and len(analyzer.calls) == 5
        analyzer.version = 'v2'
        FitAnalysisService(handler())._analyze_sync(resume_text='Skills\nPython', job_description='Requires Python')
        assert len(analyzer.calls) == 8
        extra = ResumeEvidence(evidence_id='confirmed-v2', source_section='Confirmed Career Evidence', exact_text='New Python project')
        state = state.model_copy(update={'resume_analysis': state.resume_analysis.model_copy(update={'evidence': [*state.resume_analysis.evidence, extra]})})
        h.execute(Step.MATCH_SKILLS, state)
        assert len(analyzer.calls) == 9
    finally: db.close()

def test_unvalidated_resume_never_enters_cache(tmp_path):
    db = create_database('sqlite:///' + (tmp_path/'invalid.db').as_posix(), create_schema_for_tests=True)
    analyzer = Analyzer(); h = JobAgentStepHandler(analyzer=analyzer, analysis_cache=AnalysisCache(db.session_factory))
    try:
        for _ in range(2):
            with pytest.raises(ValueError):
                h.execute(Step.ANALYZE_RESUME, AgentState(run_id='bad', resume_text='No language listed', job_description=''))
        assert len(analyzer.calls) == 2
    finally: db.close()

@pytest.mark.parametrize('count,expected', [(18, [18]), (60, [24, 24, 12])])
def test_matching_batches_keep_all_requirements_and_bound_large_jobs(count, expected):
    from eregion.domain.fast_match import assess_skills
    resume = ResumeAnalysis(summary='', skills=[], education=[], evidence=[])
    job = JobAnalysis(title='Role', summary='', responsibilities=[], requirements=[JobRequirement(
        requirement_id=str(i), canonical_name=f'duty{i}', original_text=f'Duty {i}', level='required', category='responsibility') for i in range(count)])
    meeting = Barrier(len(expected), timeout=3); sizes = []
    def analyze(schema, prompt, content):
        subset = JobAnalysis.model_validate_json(content.split('\n\nJob analysis:\n')[1])
        sizes.append(len(subset.requirements)); meeting.wait()
        return SkillAssessment(matches=[SkillEvidence(requirement_id=r.requirement_id,
            job_skill=r.canonical_name, requirement_level='required', match_status='missing',
            resume_evidence=[], confidence=1) for r in subset.requirements], explanation='', recommendations=[])
    result = assess_skills(resume, job, analyze, FastDecisions())
    assert sorted(sizes) == sorted(expected)
    assert [m.requirement_id for m in result.matches] == [r.requirement_id for r in job.requirements]

def test_exact_tool_name_still_requires_a_model_decision():
    from eregion.domain.fast_match import assess_skills
    r=JobRequirement(requirement_id='r',canonical_name='tableau',original_text='Tableau',level='required')
    resume=ResumeAnalysis(summary='',skills=[],education=[],evidence=[
        ResumeEvidence(evidence_id='e',source_section='Skills',exact_text='Used Tableau.')])
    job=JobAnalysis(title='Role',summary='',responsibilities=[],requirements=[r]);calls=[]
    def analyze(*args):
        calls.append(args)
        return SkillAssessment(matches=[SkillEvidence(requirement_id='r',job_skill='tableau',
            requirement_level='required',match_status='unknown',resume_evidence=[],confidence=0)],explanation='',recommendations=[])
    result=assess_skills(resume,job,analyze,FastDecisions())
    assert len(calls)==1 and result.matches[0].match_status=='unknown'

def test_sentence_references_round_trip_exactly():
    from eregion.domain.job_extraction import numbered_sentences, ReferencedJobExtraction
    text = 'Engineer.\nMust know Python; SQL is preferred.\nBuild APIs.'
    sentences = numbered_sentences(text)
    extraction = ReferencedJobExtraction(title='Engineer', summary='', statements=[{'sentence_id':'s1', 'requirements':[
        {'canonical_name':'python','display_name':'Python','atomic_text':'Must know Python','category':'skill','verification_mode':'resume_evidence','level':'required'}]},
        {'sentence_id':'s2','requirements':[{'canonical_name':'sql','display_name':'SQL','atomic_text':'SQL is preferred.',
            'category':'skill','verification_mode':'resume_evidence','level':'preferred'}]}], responsibility_sentence_ids=['s3'])
    result = extraction.expand(sentences)
    assert sentences == {'s0':'Engineer.','s1':'Must know Python;','s2':'SQL is preferred.','s3':'Build APIs.'}
    assert [r.source_text for r in result.requirements] == ['Must know Python;', 'SQL is preferred.']
    assert [r.level for r in result.requirements] == ['required','preferred']
    assert all(value in text for value in sentences.values())
    assert result.responsibilities == ['Build APIs.']
    with pytest.raises(ValueError): extraction.expand({})

def test_summary_counts_survive_bounded_trace():
    from eregion.runtime.telemetry import timing_update, application_perf_summary
    state = {'created_at':'2026-10-09T00:00:00+00:00'}
    for i in range(100):
        state['page_history'] = [{'heading':str(i//50)}]
        state.update(timing_update(state, 'model', 2, context_chars=1000, input_tokens=200, output_tokens=20))
    summary = application_perf_summary(state)
    assert summary['model_calls'] == 100 and summary['model_seconds'] == 200
    assert summary['average_context_chars'] == 1000 and len(summary['model_calls_per_page']) == 2
    assert len(state['timings']) == 80

def test_thirty_field_snapshot_filters_memory_and_completed_fields(monkeypatch):
    from eregion.runtime.auto_apply.agent import AgentTools, HIDDEN_TOOLS
    from test_auto_apply import FakeBrowser, field
    browser = FakeBrowser(''); browser.fields = [field(f'f{i}') for i in range(30)]
    for f in browser.fields:
        f.update(value='done', help='long help '*100, options=[{'label':str(i), 'value':str(i)} for i in range(100)])
    state={'profile':{},'answers':{f['key']:'done' for f in browser.fields},
        'remembered_answers':[{'memory_id':str(i), 'source':'user_answer', 'question':'Unrelated', 'value':'x'} for i in range(50)]}
    session=AgentTools(browser,state,autofill=lambda:{},active=lambda:True)
    snapshot=session.observe()
    assert len(json.dumps(snapshot))<25000
    assert not snapshot['remembered_answers'] and all(f['done'] for f in snapshot['fields'])
    assert 'observe' in HIDDEN_TOOLS
    old=session.observation_id
    assert session.execute('observe',{}) == {'refreshed':True,'next':'Use the observation in this turn.'}
    assert session.observation_id == old

FORM = '''<main><h1 id="heading">Application contact</h1><form onsubmit="event.preventDefault();window.submits++">
<section id="p1"><label>First name<input id="first" required></label><button type="button" onclick="p1.hidden=true;p2.hidden=false;heading.textContent='Application name'">Next</button></section>
<section id="p2" hidden><label>Last name<input id="last" required></label><button type="button" onclick="p2.hidden=true;p3.hidden=false;heading.textContent='Application review'">Continue</button></section>
<section id="p3" hidden><label>Email<input type="email" id="email" required></label><button type="submit">Submit application</button></section></form></main><script>window.submits=0</script>'''

@pytest.mark.skipif(os.getenv('JOB_AGENT_BROWSER_TESTS') != '1', reason='Opt-in real Chrome')
@pytest.mark.parametrize('url', ['https://job-boards.greenhouse.io/example/jobs/123',
    'https://jobs.lever.co/example/speed-test', 'https://example.wd5.myworkdayjobs.com/en-US/Careers/job/Speed-Test_R123'])
def test_known_three_page_application_needs_zero_model_calls(url, monkeypatch):
    from eregion.runtime.auto_apply.browser import ApplicationBrowser
    from eregion.runtime.auto_apply.agent import HybridApplicationAgent
    from eregion.runtime.auto_apply.autofill import autofill
    monkeypatch.setenv('TYPESAFE_ENABLED','false'); monkeypatch.setenv('JOB_AGENT_HIDDEN_BROWSER','headless')
    class Browser(ApplicationBrowser):
        def _guard_request(self, route): route.fulfill(content_type='text/html', body=FORM)
    class Model:
        def invoke(self,*args): raise AssertionError('Mechanical flow called a model')
    browser=Browser(url,headless=True)
    state={'profile':{'first_name':'Test','last_name':'Candidate','email':'test@example.invalid'},'answers':{},'url':url}
    calls=[]
    try:
        result=HybridApplicationAgent(model_factory=Model).run(browser,state,
            autofill=lambda:autofill(browser,state,lambda:b''), active=lambda:True,
            reserve=lambda kind:calls.append(kind) or True, report=lambda *args:None,
            checkpoint=lambda **updates:state.update(updates))
        assert result['reason']=='ready',result
        assert 'model' not in calls and browser.page.evaluate('window.submits')==0
        assert len(state['page_history'])==2
    finally: browser.close()

def test_duplicate_batch_rejected_before_any_mutation(monkeypatch):
    from test_auto_apply import FakeBrowser, field
    from eregion.runtime.auto_apply.agent import AgentTools
    from eregion.runtime.auto_apply.runner import ApplicationHost
    from eregion.runtime.core.engine import Turn, ToolArgumentsError
    monkeypatch.setenv('TYPESAFE_ENABLED','false')
    browser=FakeBrowser('');browser.fields=[field('name')]
    session=AgentTools(browser,{'profile':{'first_name':'Test','last_name':'Candidate'},'answers':{}},autofill=lambda:{},active=lambda:True)
    session.observe()
    host=ApplicationHost(session,object(),active=lambda:True,reserve=lambda _:True,report=lambda *args:None)
    calls=[{'name':'type_from_source','args':{'field_key':'name'}}]*2
    with pytest.raises(ToolArgumentsError):host.validate_batch(calls,Turn('',{},[]))
    assert not browser.fields[0]['value']

@pytest.mark.skipif(os.getenv('JOB_AGENT_BROWSER_TESTS') != '1', reason='Opt-in real Chrome')
def test_worker_chrome_is_shared_but_contexts_are_isolated(monkeypatch):
    from eregion.runtime.auto_apply.browser import ApplicationBrowser
    from eregion.runtime.auto_apply.worker import BrowserWorker
    monkeypatch.setenv('JOB_AGENT_HIDDEN_BROWSER','headless')
    class Browser(ApplicationBrowser):
        def _guard_request(self,route):route.fulfill(content_type='text/html',body=FORM)
    def work():
        a=Browser('https://jobs.lever.co/example/one',headless=True)
        b=Browser('https://jobs.lever.co/example/two',headless=True)
        process=a.browser
        try:
            assert a.browser is b.browser and a.context is not b.context
            a.context.add_cookies([{'name':'isolated','value':'one','domain':'jobs.lever.co','path':'/'}])
            assert not b.context.cookies()
            a.close();assert b.is_open()
            field=next(f for f in b.observe() if f['kind']=='text')
            start=time.perf_counter();b.type_field(field['key'],'x'*2000)
            assert time.perf_counter()-start<1
        finally:a.close();b.close()
        return process
    worker=BrowserWorker(lambda:None)
    try:worker.submit(work).result(timeout=35)
    finally:worker.shutdown()

def test_one_turn_can_fill_four_independent_fields(monkeypatch):
    from test_auto_apply import FakeBrowser, field
    from eregion.runtime.auto_apply.agent import HybridApplicationAgent
    monkeypatch.setenv('TYPESAFE_ENABLED','false')
    browser=FakeBrowser('');browser.fields=[field(f'answer{i}') for i in range(4)]
    browser.page_state=lambda: {'can_submit':True,'controls':[],'url':'https://example.invalid/apply','heading':'Application'}
    def type_field(key,value):next(f for f in browser.fields if f['key']==key).update(value=value)
    browser.type_field=type_field
    state={'profile':{},'answers':{f['key']:'value' for f in browser.fields},'chat_revision':0}
    class Model:
        calls=0
        def invoke(self,messages,tools):
            self.calls+=1
            snapshot=json.loads(messages[-1].content)
            return AIMessage(content='',tool_calls=[{'id':str(i),'name':'type_from_source','args':{
                'observation_id':snapshot['observation_id'],'field_key':f'answer{i}','source_ref':f'answer:answer{i}'}} for i in range(4)])
    model=Model()
    result=HybridApplicationAgent(model_factory=lambda:model).run(browser,state,autofill=lambda:{},
        active=lambda:True,reserve=lambda _:True,report=lambda *args:None)
    assert result['reason']=='ready' and model.calls==1
    assert all(f['value']=='value' for f in browser.fields)

def test_unknown_required_field_gets_one_model_question(monkeypatch):
    from test_auto_apply import FakeBrowser, field
    from eregion.runtime.auto_apply.agent import HybridApplicationAgent
    monkeypatch.setenv('TYPESAFE_ENABLED','false')
    browser=FakeBrowser('');browser.fields=[field('editor',label='Preferred editor')]
    calls=[]
    class Model:
        def invoke(self,messages,tools):
            calls.append(1)
            return AIMessage(content='',tool_calls=[{'id':'ask','name':'request_input','args':{'field_keys':['editor']}}])
    result=HybridApplicationAgent(model_factory=Model).run(browser,{'profile':{},'answers':{}},
        autofill=lambda:{},active=lambda:True,reserve=lambda _:True,report=lambda *args:None)
    assert result['reason']=='needs_input' and calls==[1] and not browser.submissions

def test_observation_schema_stays_stable_and_does_not_poll_repository(monkeypatch):
    from test_auto_apply import FakeBrowser, field
    from eregion.runtime.auto_apply.agent import AgentTools
    from eregion.runtime.auto_apply.runner import ApplicationHost
    monkeypatch.setenv('TYPESAFE_ENABLED','false')
    browser=FakeBrowser('');browser.fields=[field('editor',label='Preferred editor')]
    state={'profile':{},'answers':{},'chat_revision':3};reads=[]
    def refresh():reads.append(1);return state
    refresh.revision=lambda:3
    session=AgentTools(browser,state,autofill=lambda:{},active=lambda:True,refresh=refresh)
    host=ApplicationHost(session,object(),active=lambda:True,reserve=lambda _:True,report=lambda *args:None,refresh=refresh)
    first=host.observe();second=host.observe()
    assert json.dumps(first.tools)==json.dumps(second.tools)
    count=len(reads)
    for _ in range(100):assert host.revision()==3
    assert len(reads)==count

def test_factual_only_revision_removes_claim_without_a_model():
    from eregion.domain.schemas import ResumeSourceEntry, ResumeEntry, ResumeSection, SupportedClaim, TailoredResume, VerificationResult, UnsupportedClaim
    from eregion.domain.resume_selection import remove_unsupported
    source=ResumeSourceEntry(source_entry_id='s',entry_type='experience',heading='Developer',evidence_ids=['e'])
    resume=ResumeAnalysis(summary='',skills=[],education=[],source_entries=[source],evidence=[
        ResumeEvidence(evidence_id='e',source_section='Experience',source_entry_id='s',exact_text='Built Python APIs.')])
    draft=TailoredResume(sections=[ResumeSection(section_type='experience',title='Experience',entries=[
        ResumeEntry(entry_id='s',heading='Developer',bullets=[SupportedClaim(text='Led a team of 100.',evidence_ids=['e'],source_entry_id='s')])])])
    state=AgentState(run_id='repair',resume_text='Experience\nDeveloper\nBuilt Python APIs.',job_description='Python',
        resume_analysis=resume,tailored_resume=draft,verification=VerificationResult(passed=False,
        unsupported_claims=[UnsupportedClaim(claim='Led a team of 100.',reason='Unsupported claim')],revision_feedback=['Remove unsupported claim.']))
    revised=remove_unsupported(state)
    assert revised is not None
    assert [c.text for c in revised.claims()]==['Built Python APIs.']

def test_rejected_draft_never_starts_extra_model_verification():
    from eregion.runtime.application_pack.verification import verify_material_resume
    from eregion.runtime.application_pack.types import ArtifactVerification, ClaimIssue
    reject=ArtifactVerification(passed=False,issues=[ClaimIssue(block_id='x',unsupported_text='x',reason='unsupported',cited_evidence_ids=[],revision_instruction='remove')])
    def failure(state):raise AssertionError('A rejected draft must not incur a second model request')
    assert verify_material_resume(None, SimpleNamespace(verify_resume=failure), lambda:reject) is reject

def test_cached_fast_options_are_not_counted_as_provider_calls():
    from eregion.runtime.telemetry import timing_update, application_perf_summary
    state = {}
    state.update(timing_update(state, 'model', 1, input_tokens=100, context_chars=200))
    state.update(timing_update(state, 'fast_options', .001, requests=0, input_tokens=0))
    state.update(timing_update(state, 'fast_options', .1, requests=1, input_tokens=50))
    summary = application_perf_summary(state)
    assert summary['model_calls'] == 2
    assert sum(summary['model_calls_per_page'].values()) == 2
    assert summary['average_input_tokens'] == 75
    assert summary['average_context_chars'] == 200

def test_optional_browser_model_tiers_keep_recovery_on_main(monkeypatch):
    from eregion.runtime.auto_apply.agent import ConfiguredToolModel
    used = []
    class Model:
        request_timeout = 10
        def __init__(self, tier): self.tier = tier
        def bind_tools(self, tools): return self
        def invoke(self, messages, **kwargs): used.append(self.tier); return AIMessage(content='Ready')
    monkeypatch.setattr('eregion.domain.model.shared_model', lambda **kwargs: Model(kwargs['purpose']))
    model = ConfiguredToolModel()
    for snapshot in ({'fields':[]}, {'fields':[{'kind':'textarea'}]}, {'runtime_feedback':{'repeated_action':True}}, {'recovery':'previous action failed'}):
        model.invoke_turn([], [], snapshot)
    assert used == ['fast', 'agent', 'agent', 'agent']

def test_batch_rejects_new_conditional_fields_before_any_action(monkeypatch):
    from eregion.runtime.auto_apply.agent import AgentTools
    from eregion.runtime.auto_apply.runner import ApplicationHost
    from eregion.runtime.core.engine import ToolArgumentsError
    from test_auto_apply import FakeBrowser, field
    monkeypatch.setenv('TYPESAFE_ENABLED','false')
    browser = FakeBrowser(''); browser.fields = [field('first')]
    state = {'profile':{},'answers':{}}
    session = AgentTools(browser,state,autofill=lambda:{},active=lambda:True)
    host = ApplicationHost(session,object(),active=lambda:True,reserve=lambda _:True,report=lambda *args:None)
    turn = host.observe()
    with pytest.raises(ToolArgumentsError, match='newly revealed'):
        host.validate_batch([{'name':'type_from_source','args':{'field_key':'first'}},
            {'name':'type_from_source','args':{'field_key':'revealed-after-first'}}],turn)
    assert not browser.fields[0]['value']

def test_document_replacement_invalidates_memory_revision_and_close_unsubscribes(tmp_path):
    from eregion.runtime.auto_apply.repository import ApplyRepository
    from eregion.runtime.auto_apply.loop import ApplicationLoop
    from test_auto_apply import request, FakeBrowser
    db = create_database('sqlite:///' + (tmp_path/'revision.db').as_posix(),create_schema_for_tests=True)
    repo = ApplyRepository(db.session_factory)
    saved, _ = repo.create(request())
    saved = repo.save(saved,status='needs_input',pause_requested=True)
    loop = ApplicationLoop(repo,browser_factory=FakeBrowser)
    try:
        saved = repo.get(saved['run_id'])
        refresh = loop._refresh_hook(saved['run_id'])
        assert refresh.revision() == 0
        repo.replace_documents(saved,b'%PDF synthetic replacement',None)
        assert refresh.revision() == 1 and not loop._can_act(saved['run_id'])
        loop._track_control(saved)  # A delayed older notification cannot undo steering.
        assert refresh.revision() == 1
    finally:
        loop.close(); db.close()
    assert not repo.state_listeners


def test_malformed_batch_items_are_argument_errors_not_crashes(monkeypatch):
    # Veeva pilot (2026-10-15): bind_education with a string item crashed the run.
    from test_auto_apply import FakeBrowser, field
    from eregion.runtime.auto_apply.agent import AgentTools
    from eregion.runtime.auto_apply.runner import ApplicationHost
    from eregion.runtime.core.engine import Turn, ToolArgumentsError
    monkeypatch.setenv('TYPESAFE_ENABLED','false')
    browser=FakeBrowser('');browser.fields=[field('school')]
    session=AgentTools(browser,{'profile':{},'answers':{}},autofill=lambda:{},active=lambda:True)
    session.observe()
    host=ApplicationHost(session,object(),active=lambda:True,reserve=lambda _:True,report=lambda *args:None)
    with pytest.raises(ToolArgumentsError):
        host.validate_batch([{'name':'bind_education','args':{'fields':['school']}}],Turn('',{},[]))


@pytest.mark.parametrize('required', [False, True])
def test_field_attempt_limit_leaves_optional_fields_and_returns_required_ones(monkeypatch, required):
    from test_auto_apply import FakeBrowser, field
    from eregion.runtime.auto_apply.agent import AgentTools
    from eregion.runtime.auto_apply.runner import ApplicationHost, FIELD_ATTEMPT_LIMIT
    from eregion.runtime.core.engine import Turn, LoopStop
    monkeypatch.setenv('TYPESAFE_ENABLED','false')
    browser=FakeBrowser('');browser.fields=[field('school',kind='combobox',required=required)]
    session=AgentTools(browser,{'profile':{},'answers':{}},autofill=lambda:{},active=lambda:True)
    session.observe()
    host=ApplicationHost(session,object(),active=lambda:True,reserve=lambda _:True,report=lambda *args:None)
    attempted=[]
    monkeypatch.setattr(session,'execute',lambda name,args:attempted.append(args['field_key']) or {'options':[]})
    turn=Turn('',{},[])
    for i in range(FIELD_ATTEMPT_LIMIT):
        call={'id':str(i),'name':'search_options','args':{'field_key':'school','query':f'Cornell {i}'}}
        host.after_action(call,host.execute(call,turn),turn)
    call={'id':'last','name':'search_options','args':{'field_key':'school','query':'Cornell again'}}
    if required:
        with pytest.raises(LoopStop):host.execute(call,turn)
    else:
        assert host.execute(call,turn)['abandoned']=='school'
    assert len(attempted)==FIELD_ATTEMPT_LIMIT
    # A plain new message ("continue") keeps the count; a new fact for the field resets it.
    school=session.fields[0]
    assert host.attempts(school)==FIELD_ATTEMPT_LIMIT
    session.state['answers']['school']='Cornell University'
    assert host.attempts(school)==0


def test_education_mapping_ignores_gpa_and_other_follow_up_questions():
    from eregion.runtime.auto_apply.education import gaps
    state={'profile':{'education':[{'id':'e1','school':'Cornell University','degree':'M.Eng.','field_of_study':'Systems Engineering'}]},
           'answers':{},'education_required':True}
    def f(key,label):return {'key':key,'label':label,'kind':'text','value':'x','required':True,'group':'','section':''}
    fields=[f('gpa','Please specify the overall average grade (GPA) for your last/current degree:✱'),
            f('other','If you selected Other, please indicate your field of study:'),
            f('study','Please select your field of study:✱')]
    assert [g for g in gaps(state,fields,None,final=True) if g.startswith('Map')]==['Map education field: Please select your field of study:✱']


def test_resume_never_fills_an_upload_for_another_document():
    # Veeva pilot (2026-10-15): the resume was uploaded as "most recent transcript".
    from eregion.runtime.auto_apply.autofill import document_kind
    def upload(label, required=True):return {'key':label,'kind':'file','label':label,'required':required}
    state={'document_bindings':{'Please upload your most recent transcript:✱':'resume'}}
    assert document_kind(upload('Please upload your most recent transcript:✱'),state) is None
    assert document_kind(upload('Photo ID'),{}) is None
    assert document_kind(upload('Resume/CV'),{})=='resume'
    assert document_kind(upload('Resume or portfolio'),{})=='resume'
    assert document_kind(upload('Upload file'),{})=='resume'


def test_autofill_skips_optional_fields_set_aside_by_the_attempt_limit():
    # Adobe pilot: Field of Study hit the limit, but autofill re-reported it each round.
    from test_auto_apply import FakeBrowser, field
    from eregion.runtime.auto_apply.autofill import autofill
    browser=FakeBrowser('');browser.fields=[field('study',required=False)]
    state={'profile':{},'answers':{'study':'Systems Engineering'},'abandoned_fields':['study']}
    result=autofill(browser,state,lambda:b'')
    assert 'study' not in result.get('failed',{}) and browser.fields[0]['value']==''
    state['abandoned_fields']=[]
    assert autofill(browser,state,lambda:b'')['filled']==['study']


def test_workday_date_segments_format_month_and_year_from_a_stated_date():
    # Adobe pilot: "Aug 2026" -> the "From* MM" segment was refused as inventing digits.
    from eregion.runtime.auto_apply.education import format_date
    def segment(part):return {'key':f'text:workExperience-8--startDate-dateSection{part}-input:1','label':'From*','kind':'text'}
    assert format_date(segment('Month'),'Aug 2026')=='08'
    assert format_date(segment('Year'),'Aug 2026')=='2026'
    assert format_date({'key':'text:jobTitle:1','label':'Job Title*','kind':'text'},'Aug 2026')=='Aug 2026'


def test_workday_single_segment_dates_need_only_their_own_part():
    # Adobe pilot: answers saved per segment ("Aug", "2026") were refused as imprecise.
    from eregion.runtime.auto_apply.education import format_date
    def segment(part):return {'key':f'text:education-5--firstYearAttended-dateSection{part}-input:1','label':'From','kind':'text'}
    assert format_date(segment('Month'),'Aug')=='08'
    assert format_date(segment('Year'),'2026')=='2026'
    assert format_date(segment('Year'),'Expected May 2027')=='2027'
    assert format_date(segment('Month'),'2026') is None


def test_only_the_models_leave_empty_decision_releases_an_optional_education_field():
    # W6 review: the attempt count must not decide to skip a field; the model's explicit
    # leave_empty decision does, and it is saved right away.
    from eregion.runtime.auto_apply.education import gaps
    from eregion.runtime.auto_apply.decisions import signature
    record={'id':'e1','school':'Cornell University','field_of_study':'Systems Engineering'}
    field={'key':'study','label':'Field of Study','kind':'text','value':'','required':False,'group':'','section':''}
    binding={'education_id':'e1','component':'field_of_study','source_ref':'education:e1:field_of_study','field':{k:field.get(k) for k in ('key','dom_id','label','group')}}
    state={'profile':{'education':[record]},'answers':{},'education_bindings':{'study':binding},'abandoned_fields':['study']}
    assert gaps(state,[field],None)==['Field of Study']  # set aside by the count: still a gap
    state['field_decisions']={'study':{'field':signature(field),'value':None,'mode':'leave_empty','sources':{},'original_answer':None,'reason':'No such option'}}
    assert gaps(state,[field],None)==[]


def test_education_binding_beats_a_decision_from_the_profiles_latest_degree():
    # Adobe pilot: the second school's Degree (bound to the B.A. record) kept refilling as
    # "Masters" from a decision sourced from profile:degree (M.Eng.).
    from eregion.runtime.auto_apply.autofill import desired_answer
    field={'key':'combobox:degree:2','dom_id':'education-148--degree','label':'Degree*','group':'From','kind':'combobox','value':'','options':[]}
    signature={k:field.get(k) for k in ('key','dom_id','label','group')}
    state={'profile':{'degree':'M.Eng.','education':[{'id':'uw','school':'University of Wisconsin-Madison','degree':'B.A.'}]},'answers':{},
           'education_bindings':{field['key']:{'education_id':'uw','component':'degree','source_ref':'education:uw:degree','field':signature}},
           'field_decisions':{field['key']:{'field':{k:field[k] for k in ('key','label','kind','group')},'value':'Masters','mode':'equivalent',
                                            'sources':{'profile:degree':'M.Eng.'},'original_answer':None,'reason':'x'}}}
    assert desired_answer(field,state)=='B.A.'
    state['field_decisions'][field['key']]['sources']={'education:uw:degree':'B.A.'}
    state['field_decisions'][field['key']]['value']='Bachelors'
    assert desired_answer(field,state)=='Bachelors'


def test_one_page_gets_a_bounded_number_of_model_calls_per_user_message(monkeypatch):
    # Adobe pilot: 523 model calls, mostly on one Workday page, because the step budget
    # reset on every message. A page now stops at PAGE_MODEL_CALL_LIMIT and asks the user.
    from test_auto_apply import FakeBrowser, field
    from eregion.runtime.auto_apply.agent import AgentTools
    from eregion.runtime.auto_apply.runner import ApplicationHost, PAGE_MODEL_CALL_LIMIT
    from eregion.runtime.core.engine import Turn, LoopStop
    monkeypatch.setenv('TYPESAFE_ENABLED','false')
    browser=FakeBrowser('');browser.fields=[field('note',required=False)]
    session=AgentTools(browser,{'profile':{},'answers':{}},autofill=lambda:{},active=lambda:True)
    session.observe();session.page_context={'url':'https://example.test/apply','heading':'My Experience','controls':[]}
    class Model:
        timeout=5
        def invoke(self,messages,tools):return 'ok'
    host=ApplicationHost(session,Model(),active=lambda:True,reserve=lambda _:True,report=lambda *args:None)
    host.deadline=10**12
    turn=Turn('',{},[])
    for _ in range(PAGE_MODEL_CALL_LIMIT):host.invoke([],[],turn)
    with pytest.raises(LoopStop) as stop:host.invoke([],[],turn)
    assert stop.value.result['reason']=='page_budget'
    session.page_context={**session.page_context,'heading':'Application Questions'}
    host.invoke([],[],turn)  # a new page starts a new budget


def test_autofill_does_not_repeat_an_unchanged_failure_but_retries_after_a_change():
    # W1 (Adobe): host autofill re-attempted the same unchanged Field of Study 164 times.
    from test_auto_apply import FakeBrowser, field
    from eregion.runtime.auto_apply.autofill import autofill
    browser=FakeBrowser('');browser.fields=[{**field('major',required=False),'kind':'select',
        'options':[{'label':'Accounting','value':'a'},{'label':'Biology','value':'b'}]}]
    tries=[]
    def fill(current,value):
        tries.append(value)
        from eregion.runtime.auto_apply.browser import BrowserBlocked
        raise BrowserBlocked('no such option',code='option_mapping_required')
    browser.fill=fill
    state={'profile':{},'answers':{'major':'Systems Engineering'},'automation_mode':'hybrid'}
    first=autofill(browser,state,lambda:b'')
    assert tries==['Systems Engineering'] and first['failed']=={'major':'option_mapping_required'}
    again=autofill(browser,state,lambda:b'')
    assert tries==['Systems Engineering'] and again['failure_details']['major']['error_code']=='unchanged_failure'
    browser.fields[0]['options'].append({'label':'Systems Engineering','value':'s'})  # new candidates
    autofill(browser,state,lambda:b'')
    assert len(tries)==2
    state['answers']['major']='Biology'  # a new fact
    autofill(browser,state,lambda:b'')
    assert tries[-1]=='Biology'


def test_no_model_request_starts_when_the_slice_cannot_fit_it(monkeypatch):
    # W3: requests started near the end of the 180 s slice were cut off as model timeouts.
    import time
    from test_auto_apply import FakeBrowser, field
    from eregion.runtime.auto_apply.agent import AgentTools
    from eregion.runtime.auto_apply.runner import ApplicationHost
    from eregion.runtime.core.engine import Turn, LoopStop
    monkeypatch.setenv('TYPESAFE_ENABLED','false')
    browser=FakeBrowser('');browser.fields=[field('note',required=False)]
    state={'profile':{},'answers':{},'timings':[{'stage':'model','duration_seconds':d} for d in (20,24,30)]}
    session=AgentTools(browser,state,autofill=lambda:{},active=lambda:True)
    session.observe()
    calls=[]
    class Model:
        timeout=60
        def invoke(self,messages,tools):calls.append(1);return 'ok'
    host=ApplicationHost(session,Model(),active=lambda:True,reserve=lambda _:True,report=lambda *args:None)
    assert host.expected_model_seconds()==36  # 1.5 x median of recent successful calls
    host.deadline=time.monotonic()+20
    with pytest.raises(LoopStop) as stop:host.invoke([],[],Turn('',{},[]))
    assert stop.value.result['reason']=='yielded' and not calls
    host.deadline=time.monotonic()+120
    host.invoke([],[],Turn('',{},[]))
    assert calls==[1]


def test_city_only_fields_are_left_to_the_model_which_may_use_a_verbatim_part():
    # §13: autofill typed the profile's "Ithaca, NY" into City-only fields.
    from test_auto_apply import FakeBrowser, field
    from eregion.runtime.auto_apply.autofill import autofill
    from eregion.runtime.auto_apply.decisions import preserves_format
    browser=FakeBrowser('');browser.fields=[field('City*')]
    state={'profile':{'city':'Ithaca, NY'},'answers':{},'automation_mode':'hybrid'}
    autofill(browser,state,lambda:b'')
    assert browser.fields[0]['value']==''  # the model decides the representation
    assert preserves_format('Ithaca',{'profile:city':'Ithaca, NY'})
    assert not preserves_format('Boston',{'profile:city':'Ithaca, NY'})
    assert not preserves_format('Ith',{'profile:city':'Ithaca, NY'})


def test_a_category_can_be_explored_after_its_leaves_were_inspected(monkeypatch):
    # W6 (Adobe): after "Adobe Source" opened, observed options held only its leaves and
    # explore_option refused the category three times ("Explore an option actually observed").
    from test_auto_apply import FakeBrowser, field
    from eregion.runtime.auto_apply.agent import AgentTools
    monkeypatch.setenv('TYPESAFE_ENABLED','false')
    browser=FakeBrowser('');browser.fields=[{**field('source'),'kind':'combobox','choice_control':True,
        'options':[{'label':'Adobe Source','value':'a','expandable':True},{'label':'Job Board','value':'j','expandable':True}]}]
    explored=[]
    browser.explore_option=lambda key,label:explored.append(label) or [{'label':'Adobe.com','value':'c'}]
    session=AgentTools(browser,{'profile':{},'answers':{'source':'Adobe.com'}},autofill=lambda:{},active=lambda:True)
    obs=session.observe()
    session.observed_options['source']=[{'label':'Adobe.com','value':'c'},{'label':'Adobe MAX','value':'m'}]  # leaves only
    result=session.execute('explore_option',{'observation_id':obs['observation_id'],'field_key':'source','option_label':'Adobe Source',
        'source_ref':'answer:source','reason':'Adobe.com is under Adobe Source'})
    assert explored==['Adobe Source'] and result['options'][0]['label']=='Adobe.com'


def test_autofill_that_only_skipped_unchanged_failures_is_not_a_failed_action(monkeypatch):
    # W6 (Adobe): skipped-as-unchanged fills counted as failures and tripped the repeat
    # guard before the model could correct the answer.
    from test_auto_apply import FakeBrowser, field
    from eregion.runtime.auto_apply.agent import AgentTools
    from eregion.runtime.auto_apply.runner import ApplicationHost
    from eregion.runtime.core.engine import Turn
    monkeypatch.setenv('TYPESAFE_ENABLED','false')
    browser=FakeBrowser('');browser.fields=[field('note',required=False)]
    session=AgentTools(browser,{'profile':{},'answers':{}},autofill=lambda:{},active=lambda:True)
    session.observe()
    host=ApplicationHost(session,object(),active=lambda:True,reserve=lambda _:True,report=lambda *args:None)
    outcome={'filled':[],'failed':{'note':'value_not_retained'},
             'failure_details':{'note':{'error':'Not retried','error_code':'unchanged_failure','next':'x','visual_recovery':False}}}
    host.after_action({'id':'1','name':'host:autofill','args':{}},outcome,Turn('',{},[]))
    assert host.history[-1]['failed'] is False


def test_resolve_field_writes_a_checkbox_from_a_yes_no_decision(monkeypatch):
    # W6 (Adobe): "I currently work here" stayed unticked because resolve_field passed the
    # string "true" to a writer that takes only a boolean; Next then required an end date.
    from test_auto_apply import FakeBrowser, field
    from eregion.runtime.auto_apply.agent import AgentTools
    from eregion.runtime.auto_apply.experience import normalize_records
    monkeypatch.setenv('TYPESAFE_ENABLED','false')
    job=normalize_records([{'title':'Research','company':'Cornell University','start_date':'Aug 2026','current':'Present'}])[0]
    browser=FakeBrowser('');browser.fields=[{**field('current',kind='checkbox',required=False),'label':'I currently work here','value':False}]
    session=AgentTools(browser,{'profile':{'experience':[job]},'answers':{}},autofill=lambda:{},active=lambda:True)
    obs=session.observe()
    result=session.execute('resolve_field',{'observation_id':obs['observation_id'],'field_key':'current','value':'true','mode':'equivalent',
        'source_refs':[f"experience:{job['id']}:current"],'reason':'The resume says Present for this job.'})
    assert browser.fields[0]['value'] is True and result['verified']


def test_bind_education_accepts_the_reference_form_of_a_record_id(monkeypatch):
    # W6 (Adobe): "education:<id>" was refused twice before the bare id was used.
    from test_auto_apply import FakeBrowser, field
    from eregion.runtime.auto_apply.agent import AgentTools
    from eregion.runtime.auto_apply.education import normalize_records
    monkeypatch.setenv('TYPESAFE_ENABLED','false')
    record=normalize_records([{'school':'University of Wisconsin-Madison','degree':'B.A.'}])[0]
    browser=FakeBrowser('');browser.fields=[field('school',label='School or University*')]
    session=AgentTools(browser,{'profile':{'education':[record]},'answers':{}},autofill=lambda:{},active=lambda:True)
    obs=session.observe()
    result=session.execute('bind_education',{'observation_id':obs['observation_id'],'education_id':'education:'+record['id'],
        'fields':[{'component':'school','field_key':'school'}]})
    assert result['education_id']==record['id'] and result['bound']==['school']


def test_refusals_execute_nothing_end_as_refused_and_leave_resolve_field_available(monkeypatch):
    # W6 review: a guard refusal must not execute the action, add attempts, or lead to an
    # automatic recovery that repeats the same refusals.
    from test_auto_apply import FakeBrowser, field
    from eregion.runtime.auto_apply.agent import AgentTools
    from eregion.runtime.auto_apply.runner import ApplicationHost, FIELD_ATTEMPT_LIMIT
    from eregion.runtime.core.engine import Turn, LoopStop
    monkeypatch.setenv('TYPESAFE_ENABLED','false')
    browser=FakeBrowser('');browser.fields=[{**field('major',required=False),'kind':'combobox','choice_control':True,
        'options':[{'label':'Industrial Engineering','value':'i'}]}]
    session=AgentTools(browser,{'profile':{},'answers':{'major':'Systems Engineering'}},autofill=lambda:{},active=lambda:True)
    session.observe()
    host=ApplicationHost(session,object(),active=lambda:True,reserve=lambda _:True,report=lambda *args:None)
    executed=[]
    monkeypatch.setattr(session,'execute',lambda name,args:executed.append(name) or {'options':[]})
    turn=Turn('',{},[])
    for i in range(FIELD_ATTEMPT_LIMIT):
        call={'id':str(i),'name':'search_options','args':{'field_key':'major','query':f'q{i}'}}
        host.after_action(call,host.execute(call,turn),turn)
    assert host.attempts(session.fields[0])==FIELD_ATTEMPT_LIMIT
    stop=None
    for i in range(6):
        call={'id':f'r{i}','name':'search_options','args':{'field_key':'major','query':'same'}}
        try:host.after_action(call,host.execute(call,turn),turn)
        except LoopStop as exc:stop=exc;break
    assert executed==['search_options']*FIELD_ATTEMPT_LIMIT  # refusals ran nothing
    assert host.attempts(session.fields[0])==FIELD_ATTEMPT_LIMIT  # and added no attempts
    assert stop is not None and stop.result['reason']=='no_progress' and stop.result['refused'] is True
    # A different mapping decided by the model is still possible.
    call={'id':'x','name':'resolve_field','args':{'field_key':'major'}}
    host.history=[];host.execute(call,turn)
    assert executed[-1]=='resolve_field'


def test_education_reference_ids_bind_only_the_named_record(monkeypatch):
    # W6 review: normalizing "education:<id>" must never bind another record.
    from test_auto_apply import FakeBrowser, field
    from eregion.runtime.auto_apply.agent import AgentTools
    from eregion.runtime.auto_apply.browser import BrowserBlocked
    from eregion.runtime.auto_apply.education import normalize_records
    monkeypatch.setenv('TYPESAFE_ENABLED','false')
    cornell,uw=normalize_records([{'school':'Cornell University','degree':'M.Eng.'},{'school':'University of Wisconsin-Madison','degree':'B.A.'}])
    browser=FakeBrowser('');browser.fields=[field('school1',label='School or University*'),field('school2',label='School or University*')]
    session=AgentTools(browser,{'profile':{'education':[cornell,uw]},'answers':{}},autofill=lambda:{},active=lambda:True)
    obs=session.observe()
    bind=lambda record_id,key:session.execute('bind_education',{'observation_id':obs['observation_id'],'education_id':record_id,
        'fields':[{'component':'school','field_key':key}]})
    assert bind('education:'+uw['id'],'school2')['education_id']==uw['id']
    assert session.state['education_bindings']['school2']['source_ref']==f"education:{uw['id']}:school"
    for wrong in ('education:'+cornell['id']+':school','edu-unknown','education:'):
        with pytest.raises(BrowserBlocked,match='Record ids'):bind(wrong,'school1')
    assert 'school1' not in session.state['education_bindings']


def test_searching_a_picker_does_not_reset_its_attempt_count(monkeypatch):
    # Adobe re-test (a455c9f): each search changed the picker's options, which reset the count,
    # so one optional Field of Study used 8 searches and 5 inspections.
    from test_auto_apply import FakeBrowser, field
    from eregion.runtime.auto_apply.agent import AgentTools
    from eregion.runtime.auto_apply.runner import ApplicationHost
    monkeypatch.setenv('TYPESAFE_ENABLED','false')
    browser=FakeBrowser('');browser.fields=[{**field('major',required=False),'kind':'combobox','choice_control':True,'options':[{'label':'Accounting','value':'a'}]}]
    session=AgentTools(browser,{'profile':{},'answers':{'major':'Systems Engineering'}},autofill=lambda:{},active=lambda:True)
    session.observe()
    host=ApplicationHost(session,object(),active=lambda:True,reserve=lambda _:True,report=lambda *args:None)
    host.count_attempt(session.fields[0])
    session.fields[0]['options']=[{'label':'Computer Systems Analysis','value':'c'}]  # a search's results
    assert host.attempts(session.fields[0])==1
    session.state['answers']['major']='Industrial Engineering'  # a new fact still resets it
    assert host.attempts(session.fields[0])==0
