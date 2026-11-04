"""Default-route gap and matching-only binding. Providers are synthetic offline."""
import json

import httpx
import pytest

from eregion.api.services.fit_analysis_service import FitAnalysisService
from eregion.domain.workflow.handlers import JobAgentStepHandler
from eregion.domain.workflow.state import AgentState,Step
from evals.audit_fit_catalog import freeze_catalog,compare_catalog
from eregion.domain.job_extraction import JobExtraction
from eregion.domain.jev_fit.settings import Settings
from eregion.domain.schemas import ResumeAnalysis,JobAnalysis,SkillAssessment,SkillEvidence
from test_jev_fit_production import JevEndpoint,NoMainModel
from eregion.domain.requirement_catalog import FrozenRequirementCatalog


BODY='Engineer\nRequired: Python and SQL.\n'
RESUME='Candidate Example\nSKILLS\nPython, SQL\n'


def inputs(tmp_path):
    jev=FitAnalysisService(NoMainModel(),jev_enabled=True,cache_size=0,
        jev_config=Settings(key='offline-only',cache_path=tmp_path/'catalogue.sqlite3'),
        jev_transport=httpx.MockTransport(JevEndpoint()))._analyze_sync(resume_text=RESUME,job_description=BODY)
    return jev


def matches(job):
    return SkillAssessment(matches=[SkillEvidence(requirement_id=r.requirement_id,job_skill=r.canonical_name,
        requirement_level=r.level,match_status='missing',resume_evidence=[],confidence=1) for r in job.requirements],
        explanation='Offline catalogue identity probe, not a quality judgment.',recommendations=[])


@pytest.mark.parametrize('obsolete_mode',['local','extract'])
def test_default_main_service_uses_model_extraction_and_reuses_its_result(tmp_path,monkeypatch,obsolete_mode):
    monkeypatch.setenv('JOB_AGENT_MAIN_CATALOG',obsolete_mode)
    jev=inputs(tmp_path);asked=[]
    class OfflineMain:
        def cache_namespace(self):return 'offline-model-catalogue-v1'
        def __call__(self,schema,system,content):
            asked.append(schema.__name__)
            if schema is ResumeAnalysis:return jev.resume_analysis
            if schema is JobAnalysis:return jev.job_analysis
            job=JobAnalysis.model_validate_json(content.split('\n\nJob analysis:\n')[1])
            return matches(job)
    service=FitAnalysisService(JobAgentStepHandler(analyzer=OfflineMain()),jev_enabled=False)
    first=service._analyze_sync(resume_text=RESUME,job_description=BODY)
    warm=service._analyze_sync(resume_text=RESUME,job_description=BODY)
    # Resume and job extraction run concurrently; only matching depends on both.
    assert sorted(asked[:2]) == ['JobAnalysis', 'ResumeAnalysis']
    assert asked[2:] == ['SkillAssessment']
    assert first.catalogue_identity is None and warm.model_calls==0
    assert first.job_analysis==warm.job_analysis


def test_production_matching_step_preserves_an_explicit_frozen_jev_catalogue_four_times(tmp_path):
    jev=inputs(tmp_path);calls=[]
    reference=freeze_catalog(jev.job_analysis,BODY,provenance='Explicit supplied catalogue')
    def analyzer(schema,system,content):
        assert schema is SkillAssessment
        job=JobAnalysis.model_validate_json(content.split('\n\nJob analysis:\n')[1])
        calls.append(freeze_catalog(job,BODY,provenance='Matching payload'))
        return matches(job)
    handler=JobAgentStepHandler(analyzer=analyzer)
    for _ in range(4):
        state=AgentState(run_id='offline-fixed-catalogue',resume_text=RESUME,job_description=BODY,
            resume_analysis=jev.resume_analysis,job_analysis=jev.job_analysis)
        result=handler.execute(Step.MATCH_SKILLS,state).updates['skill_match']
        assert [m.requirement_id for m in result.matches]==[r.requirement_id for r in jev.job_analysis.requirements]
    assert len(calls)==4 and all(compare_catalog(reference,c)['exact_catalogue_agreement'] for c in calls)
    assert freeze_catalog(jev.job_analysis,BODY,provenance='Explicit supplied catalogue')==reference


def test_same_id_changed_constraint_is_not_catalogue_agreement(tmp_path):
    jev=inputs(tmp_path)
    a=freeze_catalog(jev.job_analysis,BODY,provenance='offline')
    changed=jev.job_analysis.model_copy(deep=True)
    changed.requirements[0].minimum_years=5
    b=freeze_catalog(changed,BODY,provenance='offline')
    assert compare_catalog(a,b)['changed_predicates'] and not compare_catalog(a,b)['exact_catalogue_agreement']
    changed.requirements[0].source_text='invented requirement'
    with pytest.raises(ValueError,match='verbatim'):freeze_catalog(changed,BODY,provenance='offline')


def catalog(tmp_path):
    jev=inputs(tmp_path)
    return FrozenRequirementCatalog.from_jev_result(jev,BODY,
        producer_signature='offline-jev-frozen-signature-v1',provenance='MockTransport Jev result, not live')


def main_handler(calls,cache=None,*,allow_extraction=False):
    def analyzer(schema,system,content):
        calls.append(schema.__name__)
        if schema is ResumeAnalysis:return ResumeAnalysis(summary='',skills=[],evidence=[],education=[])
        if schema is JobAnalysis and allow_extraction:
            from eregion.domain.schemas import JobRequirement
            # Explicit scripted extraction for historical loop replay only.
            text=next(line.strip() for line in content.splitlines() if line.strip())
            return JobAnalysis(title=None,summary='',responsibilities=[],requirements=[JobRequirement(
                requirement_id='recorded-0',canonical_name=text,display_name=text,original_text=text,
                atomic_text=text,source_text=text,category='other',verification_mode='resume_evidence',level='required')])
        assert schema is SkillAssessment, 'Frozen main matching must not extract another catalogue'
        job=JobAnalysis.model_validate_json(content.split('\n\nJob analysis:\n')[1])
        return matches(job)
    analyzer.cache_namespace=lambda:'offline-frozen-main-v1'
    return JobAgentStepHandler(analyzer=analyzer,analysis_cache=cache)


def test_main_service_internal_catalogue_binding_four_cold_and_no_jev_call(tmp_path,monkeypatch):
    frozen=catalog(tmp_path);calls=[]
    monkeypatch.setattr(FitAnalysisService,'_jev_many_sync',lambda *a,**k:pytest.fail('Hidden Jev call'))
    reference=freeze_catalog(frozen.validate_for(BODY),BODY,provenance='reference')
    for _ in range(4):
        result=FitAnalysisService(main_handler(calls),jev_enabled=False,cache_size=0)._analyze_sync(
            resume_text=RESUME,job_description=BODY,fixed_job_catalog=frozen)
        assert compare_catalog(reference,freeze_catalog(result.job_analysis,BODY,provenance='payload'))['exact_catalogue_agreement']
        assert [m.requirement_id for m in result.skill_match.matches]==[r['requirement_id'] for r in reference['requirements']]
        assert result.model_calls==2 and result.catalogue_identity['catalogue_sha256']==frozen.catalogue_sha256
    assert calls==['ResumeAnalysis','SkillAssessment']*4


def test_bound_catalogue_persistent_match_cache_and_producer_version_isolation(tmp_path):
    from dataclasses import replace
    from eregion.api.db import create_database
    from eregion.runtime.analysis_cache import AnalysisCache
    frozen=catalog(tmp_path);calls=[]
    db=create_database('sqlite:///'+(tmp_path/'main-cache.db').as_posix(),create_schema_for_tests=True)
    try:
        def service():return FitAnalysisService(main_handler(calls,AnalysisCache(db.session_factory)),jev_enabled=False)
        first=service()._analyze_sync(resume_text=RESUME,job_description=BODY,fixed_job_catalog=frozen)
        warm=service()._analyze_sync(resume_text=RESUME,job_description=BODY,fixed_job_catalog=frozen)
        assert first.model_calls==2 and warm.model_calls==0 and warm.provider_attempts==[]
        changed=replace(frozen,producer_signature='offline-jev-frozen-signature-v2')
        isolated=service()._analyze_sync(resume_text=RESUME,job_description=BODY,fixed_job_catalog=changed)
        assert isolated.model_calls==1 and calls==['ResumeAnalysis','SkillAssessment','SkillAssessment']
        assert isolated.catalogue_identity['producer_signature']!=first.catalogue_identity['producer_signature']
    finally:db.close()


def test_untrusted_changed_body_payload_ids_or_producer_rejected_before_model(tmp_path):
    from dataclasses import replace
    frozen=catalog(tmp_path);calls=[]
    service=FitAnalysisService(main_handler(calls),jev_enabled=False)
    for field,value in [('canonical_name','rust'),('display_name','Rust'),('minimum_years',5),
                        ('requirement_id','invented'),('source_text','Invented requirement.')]:
        payload=json.loads(frozen.payload_json);payload['requirements'][0][field]=value
        altered=replace(frozen,payload_json=json.dumps(payload))
        with pytest.raises(ValueError):
            service._analyze_sync(resume_text=RESUME,job_description=BODY,fixed_job_catalog=altered)
    with pytest.raises(ValueError):service._analyze_sync(resume_text=RESUME,job_description=BODY+'Changed',fixed_job_catalog=frozen)
    with pytest.raises(ValueError):service._analyze_sync(resume_text=RESUME,job_description=BODY,fixed_job_catalog=json.loads(frozen.payload_json))
    with pytest.raises(ValueError,match='producer'):frozen.validate_for(BODY,producer_signature='wrong')
    with pytest.raises(ValueError,match='identity'):frozen.validate_for(BODY,expected_catalogue_sha256='wrong')
    assert calls==[]


def test_jev_body_span_and_assessment_identity_are_required(tmp_path):
    jev=inputs(tmp_path)
    for change in ('body','span','id','level'):
        bad=jev.model_copy(deep=True)
        if change=='body':bad.skill_match.decision_details['jd_sha256']='wrong'
        elif change=='span':bad.skill_match.decision_details['extraction'][0]['start']=999
        elif change=='id':bad.skill_match.matches[0].requirement_id='wrong'
        else:bad.skill_match.matches[0].requirement_level='preferred'
        with pytest.raises(ValueError):FrozenRequirementCatalog.from_jev_result(bad,BODY,producer_signature='test',provenance='offline')


def test_async_single_and_batch_entry_preserve_supplied_catalogue(tmp_path):
    import asyncio
    frozen=catalog(tmp_path);calls=[]
    service=FitAnalysisService(main_handler(calls),jev_enabled=False,cache_size=0)
    async def run():
        single=await service.analyze(resume_text=RESUME,job_description=BODY,fixed_job_catalog=frozen)
        batch=await service.analyze_many(resume_text=RESUME,jobs=[{
            'id':f'job{i}','job_description':BODY,'fixed_job_catalog':frozen} for i in range(2)])
        return [single,*batch.values()]
    results=asyncio.run(run())
    assert all(r.job_analysis==frozen.validate_for(BODY) and r.model_calls==2 for r in results)
    assert calls.count('SkillAssessment')==3 and 'JobAnalysis' not in calls


def test_catalogue_replay_rejects_stale_version_and_result_mutation_does_not_change_input(tmp_path):
    from dataclasses import replace
    frozen=catalog(tmp_path)
    with pytest.raises(ValueError,match='version'):replace(frozen,catalogue_version='old').validate_for(BODY)
    replay=frozen.validate_for(BODY)
    replay.requirements[0].canonical_name='mutated'
    assert frozen.validate_for(BODY).requirements[0].canonical_name!='mutated'
