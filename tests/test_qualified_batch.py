import asyncio
from copy import deepcopy
import pytest
from test_career_flow import career,execute
from eregion.runtime.career.qualification import assess,deferred
from eregion.runtime.core.engine import LoopStop
from eregion.runtime.provider_usage import provider_call,usage_scope,TaskProviderBudgetExceeded

GOAL='Find 15 current junior data analyst positions in New York for my saved resume.'
def posting(i,**kw):
    return {'url':f'https://jobs.lever.co/example/{i}','title':'Junior Data Analyst','location':'New York, NY',
            'company':'Synthetic','description':'Requires Python.\n0-2 years of analytical experience.',**kw}

def prepare(c,monkeypatch,jobs):
    monkeypatch.setenv('JOB_AGENT_JOB_API_FIRST','1')
    c.repository.change(c.state['task_id'],goal=GOAL,messages=[],search_constraints={'levels':['junior'],'strict':True},
                        search_candidates={j['url']:j for j in jobs})
    calls=[]
    def read(url):calls.append(url);return deepcopy(next(j for j in jobs if j['url']==url))
    monkeypatch.setattr(c.harness.discovery,'job',read)
    return calls

@pytest.mark.parametrize('change,field,status',[
    ({'location':'United States (Remote)'},'location','unknown'),
    ({'title':'Yield Analyst'},'role','unknown'),
    ({'description':'Requires 5 years of professional experience.'},'seniority','unmet'),
    ({'title':'Data Analyst','description':'Enjoy our company benefits.'},'seniority','unknown'),
])
def test_unknown_or_mismatch_never_qualifies(change,field,status):
    result=assess({'goal':GOAL},posting(1,**change))
    assert not result['qualified'] and result['checks'][field]['status']=='unknown'
    assert result['review']['method']=='not_assessed'  # No scripted semantic verdict, including for familiar phrases.

def test_batch_only_scores_qualified_and_reuses_persisted_results(career,monkeypatch):
    # A clear mismatch (unmet level) is not scored; an unconfirmed field alone would be shown.
    c=career;jobs=[posting(1),posting(2),posting(3,description='Requires 5 years of professional experience.')]
    reads=prepare(c,monkeypatch,jobs)
    service=c.harness.analysis._fit_service;original=service.analyze_many;groups=[]
    async def batch(**kwargs):groups.append([j['id'] for j in kwargs['jobs']]);return await original(**kwargs)
    monkeypatch.setattr(service,'analyze_many',batch)
    args={'urls':[j['url'] for j in jobs]}
    result=execute(c,'verify_and_score_jobs',**args)
    assert result['qualified_count']==2 and len(result['jobs'])==2
    assert [len(g) for g in groups]==[2]
    state=c.repository.get(c.state['task_id'])
    assert len(deferred(state))==1 and len(state['jobs'])==2
    repeated=execute(c,'verify_and_score_jobs',**args)
    assert repeated['no_new_evidence'] and len(groups)==1 and len(reads)==3
    # Finished postings are reported as such instead of being redone (live-set-02 designer).
    assert sorted(i['outcome'] for i in repeated['already_processed'])==['not_shown','scored','scored']
    assert repeated['already_processed'][2]['unmet']==['seniority'] and 'do not resubmit' in repeated['next']
    regrouped=execute(c,'verify_and_score_jobs',urls=[jobs[0]['url'],jobs[2]['url']])
    assert regrouped['no_new_evidence'] and len(groups)==1 and len(reads)==3
    extra=posting(4);jobs.append(extra)
    c.repository.change(c.state['task_id'],lambda s:{'search_candidates':{**s['search_candidates'],extra['url']:extra}})
    execute(c,'verify_and_score_jobs',urls=[jobs[0]['url'],extra['url']])
    assert [len(g) for g in groups]==[2,1]  # warm item removed before paid dispatch
    changed=deepcopy(state);changed['chat_revision']+=1
    assert not deferred(changed)


def test_recommendation_rechecks_current_request(career,monkeypatch):
    c=career;jobs=[posting(1)];prepare(c,monkeypatch,jobs)
    result=execute(c,'verify_and_score_jobs',urls=[jobs[0]['url']])
    identifier=result['jobs'][0]['application_id']
    args=dict(application_id=identifier,tier='priority',reason='Good Python fit',
              evidence=['Junior Data Analyst'],gaps=[],to_confirm=[],next_steps=['Apply.'],caveats='')
    # A changed location is not confirmed for the new request: never priority.
    c.repository.change(c.state['task_id'],goal=GOAL.replace('New York','Boston'))
    with pytest.raises(ValueError,match='not confirmed inside the requested location'):execute(c,'recommend_job',**args)
    # A changed level the opening clearly does not meet keeps it out entirely.
    c.repository.change(c.state['task_id'],goal=GOAL.replace('junior','senior'))
    result=execute(c,'recommend_job',**args)
    assert result['error_code']=='qualification_needed'
    assert not c.repository.get(c.state['task_id']).get('recommendations')


def test_verified_redirect_keeps_the_batch_item_bound_to_its_source(career,monkeypatch):
    c=career;source=posting('redirect');prepare(c,monkeypatch,[source])
    resolved=posting('canonical')
    monkeypatch.setattr(c.harness.discovery,'job',lambda _:deepcopy(resolved))
    result=execute(c,'verify_and_score_jobs',urls=[source['url']])
    assert result['qualified_count']==1 and len(result['jobs'])==1
    assert result['jobs'][0]['url']==resolved['url']

def test_batch_preflights_all_urls_before_io(career,monkeypatch):
    jobs=[posting(1)];reads=prepare(career,monkeypatch,jobs)
    with pytest.raises(ValueError,match='observed'):
        execute(career,'verify_and_score_jobs',urls=[jobs[0]['url'],posting(99)['url']])
    assert reads==[]


def test_larger_acquisition_keeps_scorer_groups_bounded_and_resume_reuses(career,monkeypatch):
    c=career;jobs=[posting(i) for i in range(20)]
    reads=prepare(c,monkeypatch,jobs)
    original=c.harness.analysis.analyze_many;groups=[]
    async def batch(ids,**kwargs):
        groups.append(len(ids));return await original(ids,**kwargs)
    monkeypatch.setattr(c.harness.analysis,'analyze_many',batch)
    value=execute(c,'verify_and_score_jobs',urls=[j['url'] for j in jobs])
    assert value['qualified_count']==20 and len(value['jobs'])==20 and groups==[16,4]
    assert len(reads)==20
    from eregion.runtime.request_context import ContextArchive
    summary=ContextArchive().tool_result(value)
    assert isinstance(summary['jobs'],list) and len(summary['jobs'])==20
    assert all(isinstance(j['decision_brief'],dict) for j in summary['jobs'])

def test_cancel_mid_read_stops_before_import_or_next_source(career,monkeypatch):
    c=career;jobs=[posting(1),posting(2)];prepare(c,monkeypatch,jobs)
    calls=[]
    def read(url):
        calls.append(url);c.repository.change(c.state['task_id'],status='cancelled',pause_requested=True);return jobs[0]
    monkeypatch.setattr(c.harness.discovery,'job',read)
    with pytest.raises(LoopStop):execute(c,'verify_and_score_jobs',urls=[j['url'] for j in jobs])
    assert len(calls)==1 and c.repository.get(c.state['task_id'])['jobs']==[]

def test_nested_provider_guard_blocks_dispatch_before_reservation():
    def stop():raise LoopStop('steered')
    with usage_scope(provider_guard=stop):
        with pytest.raises(LoopStop):
            with provider_call('fake'):pytest.fail('provider body must not run')

def test_budget_stop_keeps_imports_and_resume_uses_same_jobs(career,monkeypatch):
    c=career;jobs=[posting(1),posting(2)];reads=prepare(c,monkeypatch,jobs)
    original=c.harness.analysis.analyze_many
    async def stop(*args,**kw):raise TaskProviderBudgetExceeded('test')
    monkeypatch.setattr(c.harness.analysis,'analyze_many',stop)
    with pytest.raises(TaskProviderBudgetExceeded):execute(c,'verify_and_score_jobs',urls=[j['url'] for j in jobs])
    before=c.repository.get(c.state['task_id'])
    assert len(before['jobs'])==2 and not before.get('analyses')
    monkeypatch.setattr(c.harness.analysis,'analyze_many',original)
    execute(c,'verify_and_score_jobs',urls=[j['url'] for j in jobs])
    after=c.repository.get(c.state['task_id'])
    assert after['jobs']==before['jobs'] and len(reads)==2 and len(after['analyses'])==2
    assert all(not v.get('failure') for v in after['batch_verifications'].values())

def test_granular_scoring_cannot_bypass_qualification_guard(career,monkeypatch):
    c=career;jobs=[posting(1,description='Requires 5 years of professional experience.')];prepare(c,monkeypatch,jobs)
    execute(c,'read_job',url=jobs[0]['url']);saved=execute(c,'import_job',url=jobs[0]['url'])
    assert execute(c,'score_job',application_id=saved['application_id'])['error_code']=='qualification_needed'
    assert execute(c,'score_jobs',application_ids=[saved['application_id']])['error_code']=='qualification_needed'


def test_deferred_survives_body_eviction_but_not_new_evidence():
    from eregion.runtime.career.alerts import job_key
    job=posting(1,location='US Remote')
    state={'goal':GOAL,'chat_revision':1,'retrieved_jobs':{}}
    state['candidate_qualifications']={job_key(job['url']):assess(state,job)}
    assert deferred(state)=={job_key(job['url'])}
    state['retrieved_jobs']={job['url']:posting(1)}
    assert not deferred(state)


@pytest.mark.parametrize('apostrophe',["'",'\u2019'])
def test_possessive_experience_range_is_same_requirement(apostrophe):
    from fake_search_qualification import opening_level
    from scripts.remaining_search_acceptance import constraints
    assert opening_level(f"1 - 2 years{apostrophe} Data Analysis experience")=='junior'
    assert constraints('analyst',posting(1,title='Data Analyst',
                       description=f"1 - 2 years{apostrophe} Data Analysis experience"))['seniority']
    assert opening_level(f"1 - 2 years{apostrophe} experience\nRequires 5 years of experience")=='senior'
    assert opening_level(f"4 - 6 years{apostrophe} experience") is None


def test_batch_context_root_does_not_page_through_known_verdicts():
    from eregion.runtime.request_context import ContextArchive
    result={'jobs':[],'candidates':[{'url':posting(i)['url'],'title':'Data Analyst',
             'status':'needs_evidence','qualification':assess({'goal':GOAL},posting(i,location='US Remote'))}
             for i in range(8)],'qualified_count':0,'next':'Search different candidates.'}
    archive=ContextArchive();summary=archive.tool_result(result)
    ref=summary['context_ref']
    for offset in (0,3000,6000):
        read=archive.read(ref,offset)
        assert read['no_new_evidence'] and read['content']=='' and len(read['candidates'])==8
    source=archive.read(ref,path=['candidates','0','qualification','checks','location'])
    assert 'Semantic assessment required.' in source['content']
    assert archive.read(ref,historical=True)['content']


def test_failed_source_does_not_manufacture_batch_progress(career,monkeypatch):
    jobs=[posting(1)];prepare(career,monkeypatch,jobs)
    def fail(url):raise ValueError('Unavailable posting')
    monkeypatch.setattr(career.harness.discovery,'job',fail)
    result=execute(career,'verify_and_score_jobs',urls=[jobs[0]['url']])
    assert result['no_new_evidence'] and result['candidates'][0]['status']=='source_error'


def test_repeated_recommendation_is_reported_as_already_saved(career,monkeypatch):
    c=career;jobs=[posting(1)];prepare(c,monkeypatch,jobs)
    identifier=execute(c,'verify_and_score_jobs',urls=[jobs[0]['url']])['jobs'][0]['application_id']
    args=dict(application_id=identifier,tier='priority',reason='Python fit',evidence=['Junior Data Analyst'],
              gaps=[],to_confirm=[],next_steps=['Apply.'],caveats='')
    first=execute(c,'recommend_job',**args)
    again=execute(c,'recommend_job',**args)
    assert first['recommended'] and not first.get('already_recommended')
    assert again['already_recommended'] and again['no_new_evidence']
    state=c.repository.get(c.state['task_id'])
    assert len(state['recommendations'])==1
    snapshot=c.harness.observe(state)['recommended_jobs']
    assert snapshot==[{k:state['recommendations'][0][k] for k in ('application_id','title','company','url','match_score','tier','gaps','to_confirm','next_steps')}]
    # A changed tier is a new decision, not a repeat.
    moved=execute(c,'recommend_job',**{**args,'tier':'stretch','gaps':['SQL depth unclear']})
    assert moved['recommendation']['tier']=='stretch' and not moved.get('already_recommended')


def test_source_stop_mid_batch_keeps_already_read_postings(career,monkeypatch):
    from eregion.runtime.career.linkedin_source import LinkedInSourceStopped
    c=career;jobs=[posting(1),posting(2)];prepare(c,monkeypatch,jobs)
    execute(c,'verify_and_score_jobs',urls=[jobs[0]['url']])
    def stopped(url):raise LinkedInSourceStopped('call_budget_exhausted')
    monkeypatch.setattr(c.harness.discovery,'job',stopped)
    result=execute(c,'verify_and_score_jobs',urls=[jobs[0]['url'],jobs[1]['url']])
    statuses={i['url']:i['status'] for i in result['candidates']}
    assert statuses[jobs[1]['url']]=='source_stopped' and result['qualified_count']==0
    assert [i['url'] for i in result['already_processed']]==[jobs[0]['url']]  # finished earlier, not redone


def test_exhausted_linkedin_cards_are_not_pending_work(career):
    from eregion.runtime.career.search_recovery import recovery
    c=career
    card={'url':'https://www.linkedin.com/jobs/view/4472375348/','title':'Junior Data Analyst','source':'linkedin_mcp'}
    c.repository.change(c.state['task_id'],goal=GOAL,messages=[],search_candidates={card['url']:card},
                        linkedin_mcp_state={'calls_used':49,'call_limit':50,'stopped':None,'log':[]})
    assert recovery(c.repository.get(c.state['task_id']))['pending_candidates']
    c.repository.change(c.state['task_id'],linkedin_mcp_state={'calls_used':50,'call_limit':50,'stopped':None,'log':[]})
    assert not recovery(c.repository.get(c.state['task_id']))['pending_candidates']


def test_decision_brief_lists_every_required_unresolved_item_with_ids_and_caps(career,monkeypatch):
    from eregion.runtime.career.harness import CareerHarness
    long='Required: '+'expert knowledge of advanced statistical modelling, '*6
    matches=[{'requirement_id':f'j{i}','requirement_level':'required','match_status':s,'job_skill':f'Skill {i}' if i else long}
             for i,s in enumerate(['partial','missing','unknown']*5)]
    matches+=[{'requirement_id':'p1','requirement_level':'preferred','match_status':'missing','job_skill':'Nice to have: R'},
              {'requirement_id':'c1','requirement_level':'required','match_status':'needs_confirmation','job_skill':'US work authorization'}]
    overview=CareerHarness.match_overview({'matches':matches})
    brief=CareerHarness.decision_brief({'matches':matches},overview)
    unresolved=brief['required_unresolved']
    assert unresolved['total']==15 and unresolved['omitted']==5 and len(unresolved['items'])==10
    assert [i['status'] for i in unresolved['items']][:5]==['missing']*5   # missing first, then partial
    assert all(i['id'] and i['level']=='required' for i in unresolved['items'])
    truncated=next(i for i in unresolved['items'] if i['id']=='j0')
    assert truncated['text_truncated'] and len(truncated['requirement'])==CareerHarness.BRIEF_TEXT
    assert brief['needs_confirmation']=={'total':1,'items':[{'id':'c1','level':'required','status':'needs_confirmation',
                                                             'requirement':'US work authorization'}],'omitted':0}
    assert brief['counts']['preferred:missing']==1 and 'p1' not in str(unresolved)
    from eregion.runtime.request_context import ContextArchive
    archived=ContextArchive().tool_result({'jobs':[{'application_id':'a','decision_brief':brief}],'filler':'x'*9000})
    assert archived['jobs'][0]['decision_brief']==brief      # stays inline, never behind a reference


def test_inspect_job_reads_exact_requirements_by_id(career,monkeypatch):
    c=career;jobs=[posting(1)];prepare(c,monkeypatch,jobs)
    job=execute(c,'verify_and_score_jobs',urls=[jobs[0]['url']])['jobs'][0]
    ids=[m['requirement_id'] for m in job['match_report']['matches']][:2] if job.get('match_report') else []
    if not ids:
        ids=[i['id'] for i in job['decision_brief']['required_unresolved']['items']][:2]
    detail=execute(c,'inspect_job',application_id=job['application_id'],requirement_ids=[*ids,'missing-id'])
    assert [r['id'] for r in detail['requirements']]==ids and detail['unknown_ids']==['missing-id']
    assert 'materials' not in detail and all('requirement' in r and 'resume_evidence' in r for r in detail['requirements'])


def test_queue_spreads_candidates_across_employers():
    from eregion.runtime.career.search_recovery import spread
    cards=[{'url':f'https://x/{c}{i}','company':c} for c,n in (('Affirm',4),('Stripe',1),('',1),('affirm',1)) for i in range(n)]
    assert [c['company'] for c in spread(cards)]==['Affirm','Stripe','','Affirm','Affirm','Affirm','affirm']
    assert sorted(c['url'] for c in spread(cards))==sorted(c['url'] for c in cards)  # nothing dropped


def test_low_budget_scores_what_fits_reports_the_rest_and_never_rescores(career,monkeypatch):
    # Search comparison A/B: 15 imported jobs, 0 scored — the whole batch outran its budget.
    c=career;jobs=[posting(i) for i in range(6)];prepare(c,monkeypatch,jobs)
    monkeypatch.setattr(c.harness.analysis,'uses_jev',lambda:True)
    original=c.harness.analysis.analyze_many;groups=[]
    async def batch(ids,**kwargs):
        groups.append(list(ids))
        for _ in range(8+2*len(ids)):  # the Jev estimate, spent as real ledger attempts
            with usage_scope(purpose='jev:fit:stage'),provider_call('jev-fixture',input_token_reservation=10) as d:d.update(input_tokens=10,output_tokens=1)
        return await original(ids,**kwargs)
    monkeypatch.setattr(c.harness.analysis,'analyze_many',batch)
    monkeypatch.setenv('JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS','17')   # room 14 -> (14-8)//2 = 3 jobs
    from eregion.runtime.provider_usage import UsageLedger
    ledger=UsageLedger(c.repository.sessions)
    with usage_scope(ledger=ledger,task_id=c.state['task_id']):
        first=execute(c,'verify_and_score_jobs',urls=[j['url'] for j in jobs])
    assert len(first['jobs'])==3 and len(first['unscored_for_budget'])==3 and 'Not recommendations' in first['budget_note']
    scored={j['application_id'] for j in first['jobs']}
    monkeypatch.setenv('JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS','200')
    with usage_scope(ledger=ledger,task_id=c.state['task_id']):
        second=execute(c,'verify_and_score_jobs',urls=[j['url'] for j in jobs])
    assert sorted(i['outcome'] for i in second.get('already_processed',[]))==['scored']*3
    assert len(groups)==2 and not set(groups[1])&scored  # only the deferred three were sent to the scorer


def test_inspect_job_requires_only_the_application_id():
    from eregion.runtime.career.harness import TOOLS
    schema=next(t for t in TOOLS if t['function']['name']=='inspect_job')['function']['parameters']
    # requirement_ids narrows the read; a whole-job read must not be refused as malformed.
    assert schema['required']==['application_id'] and 'requirement_ids' in schema['properties']
