import re
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from copy import deepcopy

from eregion.domain.jev_fit.resolve import DecisionStore, UnknownDecision, UnknownResolution, resolve_unknowns_many
from test_e2e_followups import EVIDENCE, raw_with_unknowns
from test_career_flow import career
from remaining_batch_contract import resolver_coverage


def test_eight_jobs_colliding_requirement_ids_one_call_and_persistent_reuse(tmp_path):
    calls=[]
    def invoke(model,schema,system,human):
        calls.append(human)
        identifiers=re.findall(r'^- (\S+) \[',human,re.M)
        assert len(set(identifiers))==8
        return UnknownResolution(decisions=[UnknownDecision(requirement_id=rid,is_candidate_requirement=True,
            level='required',personal_condition=False,status='matched',
            evidence_quote='Ran Kubernetes clusters in production for 3 years.',reason='Exact supplied evidence.') for rid in identifiers])
    store=DecisionStore(tmp_path/'resolver.sqlite3')
    jobs={str(i):raw_with_unknowns(f'Kubernetes production requirement {i}') for i in range(8)}
    result=resolve_unknowns_many(jobs,EVIDENCE,model=None,invoke=invoke,store=store)
    assert len(calls)==1 and sum(r['main_model_calls'] for r in result.values())==1
    assert all(r['matches'][0]['requirement_id']=='j0' and r['matches'][0]['status']=='matched' for r in result.values())
    assert all(r['resolver']['batch_jobs']==8 for r in result.values())
    fresh={str(i):raw_with_unknowns(f'Kubernetes production requirement {i}') for i in range(8)}
    warm=resolve_unknowns_many(fresh,EVIDENCE,model=None,invoke=invoke,store=DecisionStore(tmp_path/'resolver.sqlite3'))
    assert len(calls)==1 and sum(r['main_model_calls'] for r in warm.values())==0
    assert all(r['resolver']['cached']==1 for r in warm.values())


def test_one_batch_failure_retains_all_unknowns_and_counts_one_paid_attempt():
    calls=[]
    def fail(*args):calls.append(args);raise TimeoutError('Synthetic provider failure')
    result=resolve_unknowns_many({'a':raw_with_unknowns('Go'),'b':raw_with_unknowns('Rust')},EVIDENCE,model=None,invoke=fail)
    assert len(calls)==1 and sum(r['main_model_calls'] for r in result.values())==1
    assert all(r['matches'][0]['status']=='unknown' and r['resolver']['status']=='failed' for r in result.values())


def test_returned_ids_cannot_resolve_another_job_or_fabricate_evidence():
    def invoke(*args):
        return UnknownResolution(decisions=[UnknownDecision(requirement_id=rid,is_candidate_requirement=True,
            level='required',personal_condition=False,status='matched',evidence_quote=quote,reason='Synthetic')
            for rid,quote in [('job0:j0','Built Python tools for analytics.'),
                ('job1:j0','Built Rust systems at an invented employer.'),('job99:j0','Built Python tools for analytics.')]])
    result=resolve_unknowns_many({'a':raw_with_unknowns('Python'),'b':raw_with_unknowns('Rust')},EVIDENCE,model=None,invoke=invoke)
    assert result['a']['matches'][0]['status']=='matched'
    assert result['b']['matches'][0]['status']=='unknown'
    assert sum(r['main_model_calls'] for r in result.values())==1


def test_identical_predicates_are_requested_once_then_fanned_to_original_job_ids(tmp_path):
    requests=[]
    def invoke(model,schema,system,human):
        ids=re.findall(r'^- (\S+) \[',human,re.M);requests.append(ids)
        assert len(ids)==1  # same evidence, same complete predicate and level
        return UnknownResolution(decisions=[UnknownDecision(requirement_id=ids[0],is_candidate_requirement=True,
            level='required',personal_condition=False,status='matched',
            evidence_quote='Ran Kubernetes clusters in production for 3 years.',reason='Exact supplied evidence.')])
    jobs={str(i):raw_with_unknowns('Kubernetes in production') for i in range(8)}
    store=DecisionStore(tmp_path/'dedup.sqlite3')
    result=resolve_unknowns_many(jobs,EVIDENCE,model=None,invoke=invoke,store=store)
    assert len(requests)==1 and sum(r['main_model_calls'] for r in result.values())==1
    assert all(r['matches'][0]['requirement_id']=='j0' and r['matches'][0]['status']=='matched' for r in result.values())
    warm=resolve_unknowns_many({str(i):raw_with_unknowns('Kubernetes in production') for i in range(8)},EVIDENCE,
        model=None,invoke=invoke,store=DecisionStore(tmp_path/'dedup.sqlite3'))
    assert len(requests)==1 and sum(r['main_model_calls'] for r in warm.values())==0


def test_same_text_different_level_requires_distinct_primary_decisions():
    jobs={'required':raw_with_unknowns('Kubernetes in production'),
        'preferred':raw_with_unknowns('Kubernetes in production')}
    jobs['preferred']['matches'][0].update(level='preferred',weight=1)
    jobs['preferred']['extraction'][0]['classification']='preferred'
    def invoke(model,schema,system,human):
        rows=re.findall(r'^- (\S+) \[(\w+)\]:',human,re.M)
        assert rows==[('job0:j0','required'),('job1:j0','preferred')]
        return UnknownResolution(decisions=[UnknownDecision(requirement_id=rid,is_candidate_requirement=True,
            level=level,personal_condition=False,status='missing',reason='No supporting evidence.') for rid,level in rows])
    result=resolve_unknowns_many(jobs,EVIDENCE,model=None,invoke=invoke)
    assert [r['matches'][0]['level'] for r in result.values()]==['required','preferred']
    assert all(r['resolver']['batch_unique_requirements']==2 for r in result.values())


@pytest.mark.parametrize('reply,expected',[
    ('exact','matched'),('no_quote','unknown'),('invented_quote','unknown'),('no_primary','unknown'),
    ('alias_only','unknown'),
])
def test_offline_eight_job_live_contract_checks_primary_aliases_and_evidence(tmp_path,reply,expected):
    requests=[]
    def invoke(model,schema,system,human):
        ids=re.findall(r'^- (\S+) \[',human,re.M)
        assert ids==['job0:j0']
        quote={'exact':'Ran Kubernetes clusters in production for 3 years.',
            'invented_quote':'Ran Kubernetes clusters for 20 years.'}.get(reply)
        decisions=[] if reply=='no_primary' else [UnknownDecision(
            requirement_id='job1:j0' if reply=='alias_only' else ids[0],is_candidate_requirement=True,
            level='required',personal_condition=False,status='matched',evidence_quote=quote,reason='Synthetic response.')]
        requests.append({'primary_wire_ids':ids,'decisions':[d.model_dump() for d in decisions]})
        return UnknownResolution(decisions=decisions)
    jobs={str(i):raw_with_unknowns('Kubernetes in production') for i in range(8)}
    original=deepcopy(jobs)
    result=resolve_unknowns_many(jobs,EVIDENCE,model=None,invoke=invoke,store=DecisionStore(tmp_path/'decisions.sqlite3'))
    assert all(r['matches'][0]['status']==expected for r in result.values())
    assert sum(r['main_model_calls'] for r in result.values())==1
    audit=resolver_coverage(original,result,requests,EVIDENCE)
    assert all(v for k,v in audit['checks'].items() if k!='primary_replies_complete')
    assert audit['checks']['primary_replies_complete']==(reply not in {'no_primary','alias_only'})
    if reply=='exact':
        assert all(audit['checks'].values())
        # Reopening the persistent store, rather than reusing a Python result.
        warm=resolve_unknowns_many({str(i):raw_with_unknowns('Kubernetes in production') for i in range(8)},
            EVIDENCE,model=None,invoke=invoke,store=DecisionStore(tmp_path/'decisions.sqlite3'))
        assert len(requests)==1 and all(r['main_model_calls']==0 for r in warm.values())
        assert all(r['matches'][0]['status']=='matched' for r in warm.values())
        # Coverage must fail if an original requirement or output disappears.
        damaged=deepcopy(result);damaged['7']['matches']=[]
        assert not resolver_coverage(original,damaged,requests,EVIDENCE)['checks']['every_original_target_has_legal_result']
    (tmp_path/'eight-job-offline-coverage.json').write_text(json.dumps(audit,indent=2),encoding='utf-8')


def test_production_score_jobs_colliding_ids_one_resolver_call_and_restart_cache(career,tmp_path,monkeypatch):
    from eregion.api.db import create_database
    from eregion.api.services.fit_analysis_service import FitAnalysisService
    from eregion.runtime.auto_apply.loop import ApplicationLoop
    from eregion.runtime.auto_apply.repository import ApplyRepository
    from eregion.runtime.career.harness import CareerHarness
    from eregion.runtime.career.repository import TaskRepository
    from eregion.runtime.evidence.repository import CareerEvidenceRepository
    from eregion.runtime.workspace.repository import JobWorkspaceRepository
    from eregion.domain.jev_fit.settings import Settings
    from test_auto_apply import FakeBrowser
    from test_jev_fit_production import JevEndpoint, NoMainModel

    c=career
    endpoint=JevEndpoint();endpoint.unknown=True
    calls=[]
    def invoke(model,schema,system,human):
        rows=re.findall(r'^- (\S+) \[required\]: (.+)$',human,re.M)
        calls.append(rows)
        assert len(rows)==2 and len({rid.split(':')[1] for rid,_ in rows})==1
        return UnknownResolution(decisions=[UnknownDecision(requirement_id=rid,
            is_candidate_requirement=True,level='required',personal_condition=False,
            status='matched' if text=='Python' else 'missing',
            evidence_quote='Built Python APIs.' if text=='Python' else None,
            reason='Exact Python evidence; no supplied Rust evidence.') for rid,text in rows])
    monkeypatch.setattr('eregion.domain.model.invoke_structured',invoke)
    monkeypatch.setattr('eregion.domain.model.shared_model',lambda **_:object())
    config=replace(Settings(key='test-only',cache_path=tmp_path/'production-decisions.sqlite3'),resolve_unknowns=True)
    captures=[]
    def fit():
        service=FitAnalysisService(NoMainModel(),jev_enabled=True,jev_config=config,
            jev_transport=httpx.MockTransport(endpoint))
        actual=service.analyze_many
        async def capture(**kwargs):
            results=await actual(**kwargs)
            captures.append({key:{'main_model_calls':value.main_model_calls,
                'fast_model_calls':value.fast_model_calls,'score':value.skill_match.overall_score,
                'routing':value.decision_routing} for key,value in results.items()})
            return results
        service.analyze_many=capture
        return service
    c.harness.analysis._fit_service=fit()
    ids=[]
    for index,skill in enumerate(('Python','Rust')):
        saved=c.runtime.workspace.save_workspace(cleaned_job_description=f'{skill} Engineer\nRequired: {skill}\n',
            source_url=f'https://careers.example.com/jobs/batch-{index}',title=f'{skill} Engineer')
        identifier=saved.application.application_id
        ids.append(identifier);c.repository.link(c.state['task_id'],'jobs',identifier)
    cold=c.harness.execute(c.state['task_id'],'score_jobs',{'application_ids':ids},call_id=str(uuid4()))
    assert len(calls)==1 and [j['match_score'] for j in cold['jobs']]==[100,0]
    assert sum(v['main_model_calls'] for v in captures[0].values())==1
    reports=[c.harness.job_summary(identifier,detail=True)['match_report'] for identifier in ids]
    assert reports[0]['matches'][0]['requirement_id']==reports[1]['matches'][0]['requirement_id']
    assert [report['matches'][0]['match_status'] for report in reports]==['matched','missing']
    paid=len(endpoint.requests)
    assert paid>0

    # Reopen the actual persisted database and candidate PDF with new runtime,
    # repositories, harness and fit service. No process-local fit cache survives.
    db=create_database(f"sqlite:///{(tmp_path/'career.db').as_posix()}")
    applications=ApplicationLoop(ApplyRepository(db.session_factory),browser_factory=FakeBrowser)
    runtime=SimpleNamespace(**{**vars(c.runtime),'database':db,
        'workspace':JobWorkspaceRepository(db.session_factory),'evidence':CareerEvidenceRepository(db.session_factory)})
    repository=TaskRepository(db.session_factory)
    harness=CareerHarness(runtime,applications,repository,fit_service=fit())
    task,_=repository.create('Compare both saved jobs again after restart.','production-batch-restart')
    for identifier in ids:repository.link(task['task_id'],'jobs',identifier)
    try:
        warm=harness.execute(task['task_id'],'score_jobs',{'application_ids':ids},call_id=str(uuid4()))
        assert [j['match_score'] for j in warm['jobs']]==[100,0]
        assert len(calls)==1 and len(endpoint.requests)==paid
        # Persisted scores are reused; the fit service may not be called at all after restart.
        warm_capture=captures[1] if len(captures)>1 else {}
        assert all(v['main_model_calls']==v['fast_model_calls']==0 for v in warm_capture.values())
        assert all(next(r for r in v['routing'] if r['purpose']=='jev_fit_resolver')['cached']==1
            for v in warm_capture.values())
        (tmp_path/'production-score-jobs-evidence.json').write_text(json.dumps({
            'entrance':'CareerHarness.execute(score_jobs)','provider_io':'deterministic synthetic, no real API fee',
            'cold':cold,'warm':warm,'resolver_wire_ids':calls,'resolver_calls':len(calls),
            'jev_requests':paid,'results':captures,'cold_match_reports':reports,
            'restart':'new database connection, repositories, ApplicationLoop, CareerHarness and FitAnalysisService'},
            indent=2),encoding='utf-8')
    finally:harness.close();applications.close();db.close()


def test_offline_eight_job_production_uses_the_exact_live_acceptance_contract(career,tmp_path,monkeypatch):
    """Same workload, entrance, restart and assertions; provider IO is synthetic."""
    from eregion.api.services import fit_analysis_service as fit_module
    from eregion.domain.jev_fit.settings import Settings
    from eregion.runtime.provider_usage import provider_call, usage_scope
    from test_jev_fit_production import JevEndpoint, NoMainModel
    from test_remaining_live_batch import test_eight_job_production_cold_resolver_and_persistent_warm as run
    real_fit=fit_module.FitAnalysisService
    endpoint=JevEndpoint();endpoint.unknown=True
    def transport(request):
        response=endpoint(request)
        body=response.json();payload=json.loads(request.content)
        for key,question in payload['questions'].items():
            args=question['instructions']
            if args['question'].startswith('Does this span'):
                body['answers'][key]['noul']=.01 if any(t in args['span'] for t in
                    ('Synthetic Data Analyst','Product team')) else .99
        return httpx.Response(200,json=body)
    monkeypatch.setattr(fit_module,'FitAnalysisService',lambda **kwargs:real_fit(
        NoMainModel(),**kwargs,jev_transport=httpx.MockTransport(transport)))
    monkeypatch.setattr(Settings,'load',lambda **kwargs:Settings(key='test-only',**kwargs))
    monkeypatch.setattr('eregion.domain.model.shared_model',lambda **kwargs:object())
    def invoke(model,schema,system,human):
        rows=re.findall(r'^- (\S+) \[(\w+)\]:',human,re.M)
        # All original job prefixes must survive through alias metadata even
        # when only the first job owns primary bullets.
        assert rows and {rid.split(':')[0] for rid,_ in rows}=={'job0'}
        with usage_scope(purpose='jev_fit_resolver:UnknownResolution'):
            with provider_call('synthetic-resolver') as usage:
                usage.update(input_tokens=20,output_tokens=10)
                return UnknownResolution(decisions=[UnknownDecision(requirement_id=rid,
                    is_candidate_requirement=True,level=level,personal_condition=False,
                    status='missing',reason='Synthetic non-supporting verdict.') for rid,level in rows])
    monkeypatch.setattr('eregion.domain.model.invoke_structured',invoke)
    run(career,tmp_path,monkeypatch)
    path=tmp_path/'eight-production-cold-warm.json'
    record=json.loads(path.read_text(encoding='utf-8'))
    record['provider_io']='OFFLINE synthetic only; timing is not real API performance evidence'
    path.write_text(json.dumps(record,indent=2),encoding='utf-8')
    assert all(record['checks'].values())


def test_real_missing_support_misclassification_cannot_erase_any_of_eight_jobs():
    from pathlib import Path
    case=json.loads((Path(__file__).parent/'fixtures/remaining_resolver_classification.json').read_text())
    jobs={str(i):raw_with_unknowns(*case['requirements']) for i in range(8)}
    for raw in jobs.values():
        for i,(match,unit) in enumerate(zip(raw['matches'],raw['extraction']),2):
            match['requirement_id']=unit['id']=f'j{i}'
    original=deepcopy(jobs)
    requests=[]
    def invoke(model,schema,system,human):
        ids=re.findall(r'^- (\S+) \[',human,re.M)
        assert ids==['job0:j2','job0:j3']
        requests.append({'primary_wire_ids':ids,'decisions':case['decisions']})
        return UnknownResolution.model_validate({'decisions':case['decisions']})
    result=resolve_unknowns_many(jobs,EVIDENCE,model=None,invoke=invoke)
    for raw in result.values():
        assert len(raw['matches'])==2
        assert [m['text'] for m in raw['matches']]==case['requirements']
        assert all(m['status']=='unknown' and m['weight']==2 for m in raw['matches'])
        assert all(u['classification']=='required' for u in raw['extraction'])
        assert raw['resolver']['classification_review']==['j2','j3']
    assert all(resolver_coverage(original,result,requests,EVIDENCE)['checks'].values())
