"""Efficiency changes keep semantics with the model: fewer requests, same evidence contracts."""
import asyncio
import json
import threading
import time
import unittest

import httpx
import pytest

from langchain_core.messages import AIMessage
from test_career_flow import career
from test_semantic_search_qualification import job, setup, response
import test_jev_fit_production as jev_fixture
import test_shared_agent_loop as loop_fixture
from eregion.domain.jev_fit.core import job_units
from eregion.runtime.career import qualification as q
from eregion.runtime.career.linkedin_source import search_cards
from eregion.runtime.provider_usage import TaskProviderBudgetExceeded


def test_one_item_per_line_lists_keep_each_qualifier_whole():
    jd=("Key Responsibilities:\nBuild weekly SQL reports for clients and internal teams\n\n"
        "Desired Skills/Qualifications:\n\n"
        "Bachelor's Degree with 3.0 or higher GPA or equivalent work experience\n"
        "0-2 years of marketing or analytics experience\n"
        "Fast learner, comfortable using advanced SQL, experience working with data pipelines\n")
    units=[u['text'] for u in job_units(jd)]
    assert "Bachelor's Degree with 3.0 or higher GPA or equivalent work experience" in units
    assert '0-2 years of marketing or analytics experience' in units
    assert 'Key Responsibilities:' in units and 'Key' not in units and "Bachelor's" not in units
    assert 'Desired Skills/Qualifications:' in units
    # Wrapped prose ending in a connector still stays one requirement.
    assert job_units('Requirements:\nExperience with\nPython and SQL')[-1]['text']=='Experience with\nPython and SQL'


def test_multi_page_search_text_binds_repeated_promoted_cards_without_guessing():
    text=('Data Analyst\nA Co\nNew York, NY (On-site)\nPromoted\nYield Analyst\nB Co\nNew York, NY (Hybrid)\n'
          'Data Analyst\nA Co\nNew York, NY (On-site)\nPromoted\n'          # page 2 repeats the promoted card
          'Data Analyst\nC Co\nBrooklyn, NY (Hybrid)\n')
    refs=[{'kind':'job','url':f'/jobs/view/{i}/','text':t} for i,t in
          (('111111','Data Analyst'),('222222','Yield Analyst'),('333333','Data Analyst'))]
    cards=search_cards({'sections':{'search_results':text},'references':{'search_results':refs}})
    assert cards['111111']['company']=='A Co' and cards['333333']['location']=='Brooklyn, NY (Hybrid)'
    assert cards['222222']['location']=='New York, NY (Hybrid)'
    # A third distinct card for two references is ambiguous: fields stay unknown.
    text+='Data Analyst\nD Co\nChicago, IL (On-site)\n'
    cards=search_cards({'sections':{'search_results':text},'references':{'search_results':refs}})
    assert cards['111111']['location'] is None and cards['333333']['location'] is None


def test_jev_broker_keeps_concurrent_jobs_in_one_request_per_stage():
    from eregion.domain.jev_fit.settings import Settings
    from eregion.domain.jev_fit.transport import Broker, Client
    seen=[]
    def handle(request):
        payload=json.loads(request.content);seen.append(payload)
        if 'cases' in payload['state']:  # Only the cases referenced by this request.
            assert {q['instructions']['context_case'] for q in payload['questions'].values()}==set(payload['state']['cases'])
        return httpx.Response(200,json={'model':payload['model'],'usage':{'prompt_tokens':1,'completion_tokens':1},
            'answers':{k:{'type':'noul','noul':.99} for k in payload['questions']}})
    broker=Broker(Settings(key='test-only'),None,transport=httpx.MockTransport(handle))
    scope={'owner':'t','resume':'r','facts':'f'}
    q=lambda text:{'x':{'type':'noul','instructions':{'question':text}}}
    def fast():
        c=Client(broker,{**scope,'job':'a'})
        try:
            c.evaluate({'doc':'A'},q('stage one A'),'one');c.evaluate({'doc':'A'},q('optional A'),'optional')
            c.evaluate({'doc':'A'},q('stage two A'),'two')
        finally:broker.leave()
    def slow():
        c=Client(broker,{**scope,'job':'b'})
        try:
            time.sleep(.2);c.evaluate({'doc':'B'},q('stage one B'),'one')
            time.sleep(.2);c.checkpoint('optional')   # Nothing to ask at this optional stage.
            time.sleep(.2);c.evaluate({'doc':'B'},q('stage two B'),'two')
        finally:broker.leave()
    broker.expect(2)
    threads=[threading.Thread(target=fast),threading.Thread(target=slow)]
    try:
        for t in threads:t.start()
        for t in threads:t.join(10)
    finally:broker.close()
    assert len(seen)==3  # one, optional, two: never split by worker timing
    assert [len(p['questions']) for p in seen]==[2,1,2]


class DecisionHarness(loop_fixture.Harness):
    batch_decision_tools = {'write_item'}


class DecisionBatch(unittest.TestCase):
    setUp=loop_fixture.LoopChecks.setUp
    tearDown=loop_fixture.LoopChecks.tearDown
    start=loop_fixture.LoopChecks.start

    def test_independent_decisions_in_one_response_all_execute(self):
        self.harness=DecisionHarness()
        call,finish=loop_fixture.call,loop_fixture.finish
        model=loop_fixture.Model([AIMessage(content='',tool_calls=[call('write_item',{'value':'first'},'a'),
            call('write_item',{'value':'second'},'b')]),finish('Both decisions recorded.')])
        _,task=self.start(model)
        self.assertEqual(loop_fixture.settled(self.repo,task)['status'],'completed')
        self.assertEqual(self.harness.writes,['first','second'])


def test_qualification_groups_run_concurrently_and_bind_their_own_postings(career,monkeypatch):
    repo,tid=setup(career);active=[];peak=[0];lock=threading.Lock()
    def judge(request,postings):
        with lock:active.append(1);peak[0]=max(peak[0],len(active))
        time.sleep(.2)
        with lock:active.pop()
        return response(request,postings)
    monkeypatch.setattr(q,'judge',judge)
    jobs=[job(i,title=f'Data Analyst {i}') for i in range(12)]
    results=q.record_many(repo,tid,jobs)
    assert peak[0]>1 and all(r['qualified'] for r in results)
    for j,r in zip(jobs,results):
        assert r['url']==j['url'] and r['checks']['role']['evidence'][0]['quote']==j['title']


def test_budget_stop_in_one_group_propagates_and_saves_nothing_unverified(career,monkeypatch):
    repo,tid=setup(career)
    def judge(request,postings):
        if any(p['title'].endswith(' 7') for p in postings):raise TaskProviderBudgetExceeded('limit')
        return response(request,postings)
    monkeypatch.setattr(q,'judge',judge)
    jobs=[job(i,title=f'Data Analyst {i}') for i in range(12)]
    with pytest.raises(TaskProviderBudgetExceeded):
        q.record_many(repo,tid,jobs)
    saved=repo.get(tid).get('candidate_qualifications',{})
    assert all(row['review']['method']=='model_with_verified_quotes' for row in saved.values())
    assert not any(k.endswith('/7') for k in saved)


def test_batch_summary_keeps_every_candidate_visible_without_paging():
    from eregion.runtime.request_context import ContextArchive
    rows=[{'url':f'https://www.linkedin.com/jobs/view/{1000000+i}/','title':f'Data Analyst {i}','status':'needs_evidence',
           'qualification':{'checks':{'role':{'status':'met','quote':'Data Analyst '*40},
                                      'location':{'status':'unknown','quote':'','reason':'Only a country is stated.'}}}}
          for i in range(20)]
    summary=ContextArchive().tool_result({'jobs':[],'candidates':rows,'qualified_count':0,'filler':'x'*6000})
    assert len(summary['candidates'])==20
    assert summary['candidates'][19]['checks']=={'role':{'status':'met'},
        'location':{'status':'unknown','reason':'Only a country is stated.'}}


def test_audit_retries_only_a_structurally_unusable_reply(monkeypatch):
    from evals import search_semantic_audit as audit
    from eregion.domain import model
    replies=[]
    def invoke(_model,schema,prompt,text):
        postings=json.loads(text)['postings']
        from test_search_semantic_audit import audited
        value=audited('Find junior data analyst jobs in New York.',postings)
        if not replies:value.decisions[0].posting_id='posting-0'   # malformed identity once
        replies.append(value);return value
    monkeypatch.setattr(model,'invoke_structured',invoke)
    monkeypatch.setattr(model,'shared_model',lambda **_:object())
    jobs=[job(i) for i in range(2)]
    out=audit.review('Find junior data analyst jobs in New York.',jobs)
    assert len(replies)==2 and set(out)=={j['url'] for j in jobs}
    replies.clear()
    def always_bad(_model,schema,prompt,text):
        from test_search_semantic_audit import audited
        value=audited('x',json.loads(text)['postings']);value.decisions[0].posting_id='bad';replies.append(1);return value
    monkeypatch.setattr(model,'invoke_structured',always_bad)
    with pytest.raises(ValueError):audit.review('Find junior data analyst jobs in New York.',jobs)
    assert len(replies)==2   # bounded: never a third attempt


def test_truncated_reply_gets_a_specific_bounded_repair():
    from langchain_core.messages import AIMessage
    from eregion.runtime.model_output import output_problem, REPAIR_INSTRUCTION, TRUNCATED_INSTRUCTION
    cut=AIMessage(content='',tool_calls=[{'name':'recommend_job','args':{'application_id':'a'},'id':'1'}],
                  response_metadata={'finish_reason':'length'})
    assert output_problem(cut)=='incomplete_response'
    seen=[]
    class Fixed(loop_fixture.Model):
        def invoke(self,messages,tools):
            seen.append(messages);return super().invoke(messages,tools)
    case=DecisionBatch('test_independent_decisions_in_one_response_all_execute')
    case.setUp()
    try:
        case.harness=DecisionHarness()
        model=Fixed([cut,loop_fixture.finish('Done.')])
        _,task=case.start(model)
        assert loop_fixture.settled(case.repo,task)['status']=='completed'
        repair=' '.join(str(getattr(m,'content','')) for m in seen[1])
        assert TRUNCATED_INSTRUCTION.strip() in repair and case.harness.writes==[]
    finally:case.tearDown()


def test_one_reply_of_repeated_decisions_counts_once_toward_the_stall_limit():
    class Repeating(loop_fixture.Harness):
        batch_decision_tools={'write_item'}
        def execute(self,task_id,name,args,**kwargs):
            self.writes.append(args['value']);return {'saved':args['value'],'no_new_evidence':True}
    case=DecisionBatch('test_independent_decisions_in_one_response_all_execute');case.setUp()
    try:
        case.harness=Repeating()
        call=loop_fixture.call
        batch=AIMessage(content='',tool_calls=[call('write_item',{'value':str(i)},str(i)) for i in range(3)])
        model=loop_fixture.Model([batch,loop_fixture.finish('Saved results reported.')])
        _,task=case.start(model)
        state=loop_fixture.settled(case.repo,task)
        assert state['status']=='completed' and case.harness.writes==['0','1','2']
        assert state['progress_guard']['count']==1
    finally:case.tearDown()


def test_last_attempts_are_reserved_for_the_main_model_reply(tmp_path):
    from eregion.api.db import create_database
    from eregion.runtime.provider_usage import UsageLedger, ReplyBudgetReserved, TaskProviderBudgetExceeded
    db=create_database('sqlite:///'+(tmp_path/'u.sqlite3').as_posix(),create_schema_for_tests=True)
    try:
        ledger=UsageLedger(db.session_factory);scope={'task_id':'t'};limits={'provider_attempts':10}
        def start(i,purpose):
            ledger.start(f'c{i}',scope,{'purpose':purpose,'input_tokens':1,'output_tokens':1},limits=limits)
        for i in range(7):start(i,'jev:fit:support_yes_no')
        with pytest.raises(ReplyBudgetReserved):start(7,'search_qualification:SearchQualification')
        start(7,'agent');start(8,'agent');start(9,'agent')   # the reply keeps its reserve
        with pytest.raises(TaskProviderBudgetExceeded):start(10,'agent')   # the task cap is unchanged
    finally:db.close()


def test_reserved_budget_becomes_a_tool_result_and_the_loop_can_finish():
    from eregion.runtime.provider_usage import ReplyBudgetReserved
    class Reserved(loop_fixture.Harness):
        def execute(self,task_id,name,args,**kwargs):raise ReplyBudgetReserved('reply_reserve')
    case=DecisionBatch('test_independent_decisions_in_one_response_all_execute');case.setUp()
    try:
        case.harness=Reserved();call=loop_fixture.call
        model=loop_fixture.Model([AIMessage(content='',tool_calls=[call('write_item',{'value':'x'},'a')]),
                                  loop_fixture.finish('Saved results and the shortfall.')])
        _,task=case.start(model)
        state=loop_fixture.settled(case.repo,task)
        assert state['status']=='completed'
        assert state['history'][0]['result']['error_code']=='provider_budget_reserved'
    finally:case.tearDown()


def test_last_provider_attempts_offer_only_decisions_then_only_the_reply(career,monkeypatch):
    from eregion.runtime.provider_usage import UsageLedger
    from test_remaining_search_runtime import host_for
    monkeypatch.setenv('JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS','10')
    c=career;host=host_for(c);ledger=UsageLedger(c.repository.sessions)
    names=lambda turn:{t['function']['name'] for t in turn.tools}
    assert 'search_jobs' in names(host.observe())
    for i in range(7):ledger.start(f'c{i}',{'task_id':c.state['task_id']},{'purpose':'agent','input_tokens':1,'output_tokens':1})
    turn=host.observe()
    assert turn.snapshot['completion_required'] and names(turn)=={'finish_task'}   # nothing left to decide
    c.repository.change(c.state['task_id'],scored_job_evidence={'app-1':{'application_id':'app-1','url':'https://example.com/1'}})
    turn=host.observe()
    assert names(turn)=={'finish_task','recommend_job','decide_job'}
    assert 'Score <saved value>' in turn.snapshot['completion_reason']
    ledger.start('c7',{'task_id':c.state['task_id']},{'purpose':'agent','input_tokens':1,'output_tokens':1})
    assert names(host.observe())=={'finish_task'}   # two attempts remain for the reply and one correction


def test_clause_after_a_score_is_not_a_descending_score_interval():
    from eregion.runtime.career.reply_evidence import reply_job_evidence
    text=('7. **Data Analyst — Veeva Crossix** — New York, NY — Score **46.12** — 45-60 min NYC commute\n'
          '   https://www.linkedin.com/jobs/view/4439835443/\n'
          '8. Data Analyst — Good Apple — Score range 40–55\n   https://www.linkedin.com/jobs/view/4432001930/')
    found=reply_job_evidence(text)
    assert found['https://www.linkedin.com/jobs/view/4439835443/']=={'scores':[46.12],'ranges':[]}
    assert found['https://www.linkedin.com/jobs/view/4432001930/']=={'scores':[],'ranges':[[40.0,55.0]]}


# Jev: about two requests per job plus 8 shared (search comparison A, 2026-10-18), not one shared request per stage only.
@pytest.mark.parametrize('jev,used,expected',[(True,5,2),(True,9,0),(False,5,2)])
def test_scoring_batch_is_sized_to_the_attempts_that_can_finish_it(career,monkeypatch,jev,used,expected):
    from eregion.runtime.provider_usage import UsageLedger, ReplyBudgetReserved
    monkeypatch.setenv('JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS','20')
    c=career;ledger=UsageLedger(c.repository.sessions)
    for i in range(used):ledger.start(f'c{i}',{'task_id':c.state['task_id']},{'purpose':'agent'})
    monkeypatch.setattr(c.harness.analysis,'uses_jev',lambda:jev)
    ids=[f'app-{i}' for i in range(10)]
    if not expected:
        with pytest.raises(ReplyBudgetReserved):c.harness.affordable_scoring(c.state['task_id'],ids)
        return
    now,later=c.harness.affordable_scoring(c.state['task_id'],ids)
    assert now==ids[:expected] and later==ids[expected:]


def test_only_a_never_established_connection_counts_as_zero_usage():
    import httpx
    from eregion.runtime.provider_usage import _never_sent
    def chained(cause):
        try:
            try:raise cause
            except Exception as inner:raise RuntimeError('APIConnectionError') from inner
        except RuntimeError as outer:return outer
    request=httpx.Request('POST','https://example.com')
    assert _never_sent(chained(httpx.ConnectError('refused',request=request)))
    assert not _never_sent(chained(httpx.ReadError('reset',request=request)))
    assert not _never_sent(chained(httpx.RemoteProtocolError('closed',request=request)))
    assert not _never_sent(RuntimeError('unknown'))


def test_full_and_trimmed_prompts_share_one_eligibility_contract():
    from eregion.runtime.career import prompt
    from eregion.runtime.career.prompt import ELIGIBILITY_RULE, trim_prompt
    assert ELIGIBILITY_RULE in prompt.SYSTEM and ELIGIBILITY_RULE in trim_prompt(prompt.SYSTEM)
    for text in (prompt.SYSTEM, trim_prompt(prompt.SYSTEM)):
        assert 'Unknown eligibility requires caveats' not in text and 'Unknown eligibility is a caveat' not in text
        assert '{ELIGIBILITY_RULE}' not in text


def test_seniority_policy_follows_the_requested_level_not_a_fixed_junior_rule():
    from eregion.domain.search_qualification import POLICY
    from evals.search_semantic_audit import PROMPT
    for text in (POLICY, PROMPT):
        assert 'early-career' not in text and 'Junior is a career level' not in text
        assert 'requested' in text and 'years cutoff' in text


def test_failed_qualification_group_is_retried_once_then_stays_unknown(career,monkeypatch):
    repo,tid=setup(career);calls=[]
    def flaky(request,postings):
        calls.append(postings[0]['title'])
        if calls.count(postings[0]['title'])==1:raise ConnectionError('reset')
        return response(request,postings)
    monkeypatch.setattr(q,'judge',flaky)
    results=q.record_many(repo,tid,[job(i,title=f'Data Analyst {i}') for i in range(3)])
    assert all(r['qualified'] for r in results) and len(calls)==2      # one group, one retry
    def broken(request,postings):calls.append('x');raise ConnectionError('reset')
    monkeypatch.setattr(q,'judge',broken);calls.clear()
    results=q.record_many(repo,tid,[job(9,title='Data Analyst 9')])
    assert len(calls)==2 and results[0]['review']['method']=='model_unavailable'   # bounded: never a third


def test_experiment_budget_is_shared_across_tasks_and_counts_failures(tmp_path,monkeypatch):
    from eregion.runtime.provider_usage import (provider_call, usage_scope, UsageLedger, TaskProviderBudgetExceeded,
                                                experiment_summary)
    from eregion.api.db import create_database
    ledger_path=tmp_path/'experiment.sqlite3'
    monkeypatch.setenv('JOB_AGENT_EXPERIMENT_ID','exp-1');monkeypatch.setenv('JOB_AGENT_EXPERIMENT_LEDGER',str(ledger_path))
    monkeypatch.setenv('JOB_AGENT_EXPERIMENT_MAX_PROVIDER_ATTEMPTS','3');monkeypatch.setenv('JOB_AGENT_EXPERIMENT_MAX_INPUT_TOKENS','10000')
    db=create_database('sqlite:///'+(tmp_path/'task.sqlite3').as_posix(),create_schema_for_tests=True)
    try:
        ledger=UsageLedger(db.session_factory)
        def call(task,fail=False):
            with usage_scope(task_id=task,ledger=ledger):
                with provider_call('m',input_token_reservation=10) as details:
                    if fail:raise ConnectionError('reset after send')
                    details.update(input_tokens=10,output_tokens=1)
        call('task-a')
        with pytest.raises(ConnectionError):call('task-b',fail=True)   # a new task, a failed call
        call('task-c')
        with pytest.raises(TaskProviderBudgetExceeded):call('task-d')  # fourth call: cumulative cap
        total=experiment_summary(ledger_path,'exp-1')
        assert total['calls']==3 and total['unknown_usage_calls']==1   # the dropped call stays unknown
    finally:db.close()


@pytest.mark.parametrize('error,retried',[
    (ConnectionError('reset'),True),(TimeoutError('slow'),True),
    (type('AuthenticationError',(Exception,),{'status_code':401})('bad key'),False),
    (type('RateLimitError',(Exception,),{'status_code':429})('slow down'),False),
    (TypeError('program error'),False),(KeyError('config'),False)])
def test_only_transport_or_format_failures_are_retried(career,monkeypatch,error,retried):
    repo,tid=setup(career);calls=[]
    def judge(request,postings):calls.append(1);raise error
    monkeypatch.setattr(q,'judge',judge)
    result=q.record_many(repo,tid,[job(1)])[0]
    assert len(calls)==(2 if retried else 1) and result['review']['method']=='model_unavailable'


def _chained(outer, inner):
    try:
        try:
            raise inner
        except Exception as cause:
            raise outer from cause
    except Exception as error:
        return error


def test_a_connect_error_under_the_provider_error_is_retryable_but_program_errors_are_not():
    import httpcore, httpx, openai
    from eregion.runtime.model_runtime import model_failure, recoverable_failure
    request = httpx.Request('POST', 'https://example.invalid/v1/chat/completions')
    # The live chain of acceptance tiered-v3-03: OpenAI connection error over httpcore.ConnectError.
    error = _chained(openai.APIConnectionError(request=request), httpcore.ConnectError('All connection attempts failed'))
    failure = model_failure(error)
    assert failure.code == 'model_unavailable' and failure.retryable and recoverable_failure(error)
    assert not model_failure(_chained(RuntimeError('x'), ValueError('bad argument'))).retryable
    auth = openai.AuthenticationError('denied', response=httpx.Response(401, request=request), body=None)
    assert not model_failure(auth).retryable and not recoverable_failure(auth)


def test_scoring_batch_respects_the_input_token_budget_and_cached_jobs(career,monkeypatch):
    from eregion.runtime.provider_usage import UsageLedger, ReplyBudgetReserved
    monkeypatch.setenv('JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS','200')
    monkeypatch.setenv('JOB_AGENT_TASK_MAX_INPUT_TOKENS','400000')
    c=career;ledger=UsageLedger(c.repository.sessions)
    monkeypatch.setattr(c.harness.analysis,'uses_jev',lambda:True)
    ids=[f'app-{i}' for i in range(10)]
    # 400k - 3x20k reserve = 340k -> 17 requests -> (17-8)//2 = 4 jobs, though attempts would allow 10.
    now,later=c.harness.affordable_scoring(c.state['task_id'],ids)
    assert now==ids[:4] and later==ids[4:]
    # Already-analysed jobs cost nothing and are kept first.
    now,later=c.harness.affordable_scoring(c.state['task_id'],ids,cached=['app-9','app-8'])
    assert now==['app-8','app-9',*ids[:4]] and later==ids[4:8]
    for i in range(16):ledger.start(f't{i}',{'task_id':c.state['task_id']},{'purpose':'agent','input_tokens':20000,'output_tokens':1})
    with pytest.raises(ReplyBudgetReserved):c.harness.affordable_scoring(c.state['task_id'],ids)
    assert c.harness.affordable_scoring(c.state['task_id'],ids,cached=['app-0'])==(['app-0'],ids[1:])
