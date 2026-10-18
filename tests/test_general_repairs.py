"""Cross-input regressions for failures found during real application QA."""
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from eregion.domain.schemas import ResumeAnalysis, ResumeEvidence, ResumeSourceEntry, TailoredResume
from eregion.domain.domain import normalize_resume_analysis
from eregion.domain.resume_sources import complete_source_evidence
from eregion.domain.compact_match import match_payload
from eregion.domain.quality import ensure_foundational_resume_sections
from eregion.runtime.auto_apply.agent import AgentTools
from eregion.runtime.auto_apply.runner import ApplicationHost
from eregion.runtime.core.engine import LoopStop
from eregion.runtime.progress import browser_evidence
from eregion.runtime.application_pack.selection import current_materials, latest_materials
from test_career_flow import career, prepare, execute, approve
from test_mock_interview import setup
from test_auto_apply import database


def education(text, school, degree, end):
    return ResumeAnalysis(summary='', skills=[], education=[], evidence=[
        ResumeEvidence(evidence_id='course', source_section='Education', exact_text='Coursework: Statistics.', source_entry_id='school')],
        source_entries=[ResumeSourceEntry(source_entry_id='school', entry_type='education',
            heading=degree, organization=school, end_date=end, evidence_ids=['course'])])


@pytest.mark.parametrize('degree,end', [
    ('BSc Computer Science', 'May 2024'),
    ('MSc Engineering', 'May 2028 (expected)'),
    ('Computer Science coursework (degree not completed)', '2023'),
    ('Diploma in Design', '2022'),
])
def test_degree_headers_are_verbatim_owned_and_idempotent(degree, end):
    text=f'Other employer\n2022\nEDUCATION\nExample University\n{degree}\n{end}\nCoursework: Statistics.'
    analysis=education(text,'Example University',degree,end)
    result=normalize_resume_analysis(complete_source_evidence(analysis,text))
    assert any(degree in e.exact_text and end in e.exact_text for e in result.evidence)
    assert all(e.exact_text in text for e in result.evidence)
    assert all(e.source_entry_id==result.source_entries[0].source_entry_id for e in result.evidence)
    assert normalize_resume_analysis(complete_source_evidence(result,text))==result
    assert end in match_payload(result,[])


def test_repeated_degree_names_keep_school_ownership():
    text='School One\nMSc Computing\n2022\nHonors.\nSchool Two\nMSc Computing\n2028 (expected)\nCoursework: Statistics.'
    analysis=education(text,'School Two','MSc Computing','2028 (expected)')
    result=complete_source_evidence(analysis,text)
    header=result.evidence[-1].exact_text
    assert 'School Two' in header and 'School One' not in header and '2028 (expected)' in header


def test_skill_block_preserves_all_affirmative_clauses_without_negative_disclaimers():
    source=ResumeSourceEntry(source_entry_id='skills',entry_type='skills',heading='Tools',evidence_ids=['e'])
    quote='Python, SQL; Tableau and Excel. No claim of professional Kubernetes experience.'
    draft=TailoredResume(sections=[])
    fixed=ensure_foundational_resume_sections(draft,[source],{'e':quote})
    rendered=' '.join(c.text for c in fixed.claims())
    assert all(s in rendered for s in ('Python','SQL','Tableau','Excel'))
    assert 'Kubernetes' not in rendered and 'No claim' not in rendered


def test_failed_boolean_does_not_discard_other_supported_answers():
    fields=[{'key':k,'label':q,'kind':'checkbox','options':[]} for k,q in
            [('english','Are you fluent in English?'),('partners','Share with commercial partners?')]]
    text='I speak fluent English. Use my data for this application only.'
    state={'profile':{},'answers':{},'messages':[{'id':'u','role':'user','text':text}]}
    session=AgentTools(None,state,autofill=None,active=lambda:True)
    session.fields,session.observation_id=fields,'o'
    session.verify_answers=lambda rows, **kwargs:[True,False]
    result=session._record_answers({'observation_id':'o','answers':[
        {'field_key':k,'message_id':'u','quote':q,'value':'Yes'} for k,q in
        [('english','I speak fluent English.'),('partners','Use my data for this application only.')]]})
    assert state['answers']=={'english':True}
    assert result['saved']==['english'] and result['failed_fields']==['partners']


def test_verifier_timeout_is_provider_failure_not_bad_answer():
    def fail(_, **kwargs):raise TimeoutError('private provider payload')
    session=SimpleNamespace(state={},browser=object(),autofill_called=False,failed_fields=set())
    host=ApplicationHost(session,SimpleNamespace(verify_answers=fail),active=lambda:True,
        reserve=lambda _:True,report=lambda *_:None,decisions=SimpleNamespace(enabled=False))
    try:
        with pytest.raises(LoopStop) as caught:host.verify_answers([{'question':'Q','quote':'user words','proposed':'Yes'}])
        assert caught.value.result['reason']=='model_timeout'
        assert caught.value.result['stage']=='answer_verification'
        assert 'private provider payload' not in str(session.state)
        timings=session.state['timings']
        assert 'answer_verification' in str(timings) and 'True' in str(timings)
    finally:host.requests.close()


def test_browser_progress_ignores_tokens_but_detects_content_changes():
    left={'url':'https://example.com/jobs','observation_id':'a','controls':[{'ref':'a1','label':'Engineer','href':'https://example.com/job'}]}
    right={**left,'observation_id':'b','controls':[{**left['controls'][0],'ref':'b1'}]}
    assert browser_evidence(left)==browser_evidence(right)
    right['controls'][0]['label']='Designer'
    assert browser_evidence(left)!=browser_evidence(right)


def test_cross_pack_current_selection_and_reuse(career):
    c=career
    job,first=prepare(c)
    approve(c,first)
    application=c.runtime.workspace.get_application(job['application_id'])
    second=c.runtime.pack_workflow.create(application.application_id,expected_version=application.version,idempotency_key='independent-pack')
    c.runtime.pack_workflow.generate_many(second.pack_id,['tailored_resume'],idempotency_key='new-resume')
    visible=current_materials(c.runtime.workspace,c.runtime.packs,c.runtime.pack_workflow,application.application_id)
    assert len([i for p,i in visible if i.artifact_type=='tailored_resume'])==2
    selected=latest_materials(c.runtime.workspace,c.runtime.packs,c.runtime.pack_workflow,application.application_id)
    assert {i.artifact_type for p,i in selected}=={'tailored_resume','cover_letter'}
    assert next(p.pack_id for p,i in selected if i.artifact_type=='cover_letter')==first['pack_id']
    assert next(p.pack_id for p,i in selected if i.artifact_type=='tailored_resume')==second.pack_id
    # The latest unreviewed resume must block use, not silently fall back to old approval.
    with pytest.raises(ValueError,match='review'):
        execute(c,'start_application',application_id=application.application_id,resume_source='auto')
    repeated=c.runtime.pack_workflow.create(application.application_id,expected_version=application.version,
        idempotency_key='different-tool-args',scope_to_inputs=True)
    assert repeated.pack_id==second.pack_id
    count=len(c.runtime.packs.list_for_application(application.application_id))
    c.runtime.pack_workflow.generate_many(second.pack_id,['tailored_resume'],idempotency_key='repeat')
    assert len(c.runtime.packs.list_for_application(application.application_id))==count
    # Stale documents are not eligible even if approved.
    c.runtime.packs.stale(first['pack_id'],'test_changed_inputs')
    selected=current_materials(c.runtime.workspace,c.runtime.packs,c.runtime.pack_workflow,application.application_id)
    assert {i.artifact_type for p,i in selected}=={'tailored_resume'}


def test_interview_retry_diagnostics_are_not_new_evidence(setup):
    controller,model,application_id,db=setup
    original=model.evaluate
    feedback=[]
    def invalid(context):
        feedback.append(context.get('validation_feedback'))
        return original(context).model_copy(update={'strengths':['You increased revenue by 998877%.']})
    model.evaluate=invalid
    session=controller.start(application_id,idempotency_key='invalid-coaching',target_question_count=3)
    result=controller.answer(session.mock_interview_id,'I built Python APIs.',expected_version=session.version,idempotency_key='answer')
    assert result.status.value=='failed'
    detail=controller.view(session.mock_interview_id)['failure']
    assert detail['answer_saved'] and detail['error_code']=='evaluation_grounding'
    assert feedback[1]['detail']=='Coaching introduced an unsupported number.'
    assert model.evaluations==2
    model.evaluate=original
    resumed=controller.resume(session.mock_interview_id,expected_version=result.version,idempotency_key='recover')
    assert resumed.status.value=='awaiting_answer'
    assert len(controller.interviews.turns(session.mock_interview_id))==2


def test_job_batches_preserve_context_and_complete_sentence_ownership():
    from eregion.domain.job_extraction import extract_job, ReferencedJobExtraction
    seen=[]
    def invoke(model,schema,prompt,text):
        data=json.loads(text);seen.append(data)
        return ReferencedJobExtraction(title='Engineer',summary='',statements=[],responsibility_sentence_ids=[])
    with patch('eregion.domain.model.invoke_structured',invoke):
        extract_job(object(),'\n'.join(f'Line {i}: qualification.' for i in range(47)))
    assert len(seen)==3  # 20-sentence groups
    assert all(len(row['source_sentences'])==47 for row in seen)
    ids=[k for row in seen for k in row['extract_sentence_ids']]
    assert len(ids)==len(set(ids))==47


def test_large_tool_result_keeps_full_controls_and_recovery_feedback():
    from eregion.runtime.request_context import ContextArchive
    from eregion.runtime.core.context import trace_value
    archive=ContextArchive()
    original={'observation_id':'live-token','text':'Long public page. '*1800,
              'controls':[{'ref':str(i),'label':f'Job {i}','href':f'https://example.com/jobs/{i}'} for i in range(90)],
              'control_count':90,'progress_feedback':'No new evidence; change approach.',
              'password':'must-not-be-stored'}
    result=archive.tool_result(original)
    assert result['observation_id']=='live-token'
    assert result['progress_feedback']==original['progress_feedback']
    assert result['context_ref'] in archive.values
    assert 'must-not-be-stored' not in str(archive.values)
    read=archive.read(result['context_ref'],path=['controls','89'])
    assert json.loads(read['content'])==original['controls'][89]
    assert trace_value(original)['progress_feedback']==original['progress_feedback']


def test_active_tool_context_survives_archive_pruning():
    from eregion.runtime.request_context import ContextArchive,prepare_request
    from eregion.runtime.core.context import ConversationWindow
    archive=ContextArchive()
    original={'details':'a'*15000}
    result=archive.tool_result(original)
    for n in range(100):archive.reference({'data':str(n)*30000})
    window=ConversationWindow()
    window.append({'goal':'Inspect evidence'},'', [{'id':'call','name':'inspect','args':{}}],[result],0)
    prepare_request('Task',[],window,{},archive)
    assert result['context_ref'] in archive.values


def test_ordered_interview_outline_does_not_count_as_invented_metrics():
    from eregion.runtime.mock_interview.policy import validate_evaluation
    from eregion.runtime.mock_interview.types import MockAnswerEvaluation,MockInterviewQuestion
    q=MockInterviewQuestion(question_id='q',plan_item_id='p',question_text='Describe your work.',
                           question_type='role_fit',competency='role_fit',reason_for_asking='Practice.')
    result=MockAnswerEvaluation(evaluation_id='e',answer_id='a',overall_score=3,relevance_score=3,
        specificity_score=3,evidence_grounding_score=3,structure_score=3,communication_score=3,
        suggested_structure='1. Context: a prototype. 2. Action: built APIs. 3. Validation: reviewed test cases.')
    validate_evaluation(result,'I built APIs and reviewed test cases for a prototype.',q,'')
    invented=result.model_copy(update={'suggested_structure':'1. Context: a prototype. 2. Outcome: improved revenue by 40%.'})
    with pytest.raises(Exception,match='unsupported number'):
        validate_evaluation(invented,'I built APIs.',q,'')


def test_same_tool_provider_failure_has_bounded_retries(database):
    from test_task_loop import Harness,ToolModel,settled
    from eregion.runtime.tasks.loop import TaskLoop
    from eregion.runtime.career.repository import TaskRepository
    harness=Harness()
    def failed(*args,**kwargs):
        harness.lookups+=1
        raise TimeoutError('private provider body')
    harness.execute=failed
    calls=[]
    def decide(snapshot):
        calls.append(snapshot)
        return ('lookup',{}) if len(calls)<=5 else ('finish_task',{'text':'The provider is unavailable; saved work is retained.'})
    repository=TaskRepository(database.session_factory)
    loop=TaskLoop(repository,harness,model_factory=lambda:ToolModel(decide))
    try:
        state=loop.start('Check a service.','bounded-provider-test')
        final=settled(repository,state['task_id'])
        assert final['status']=='completed' and harness.lookups==2
        assert any(h['result'].get('error_code')=='retry_exhausted' for h in final['history'])
        assert 'private provider body' not in json.dumps(final)
    finally:loop.close()


def test_application_handoff_preserves_long_user_message():
    from eregion.runtime.career.harness import CareerHarness
    message='x'*3950+' I do not consent to future retention.'
    assert CareerHarness.application_instruction({'messages':[{'role':'user','text':message}]})==message


def test_legacy_excerpts_and_references_are_not_presented_as_complete_evidence():
    from eregion.runtime.request_context import ContextArchive, prepare_request
    from eregion.runtime.core.context import ConversationWindow
    old={'excerpt':'{"controls": [', 'truncated':True}
    archive=ContextArchive({'ctx-old':json.dumps(old)})
    assert archive.read('ctx-old')['error_code']=='incomplete_context'
    window=ConversationWindow()
    call={'id':'c','name':'browser_read','args':{}}
    window.append({'goal':'Find jobs'},'', [call],[{'context_ref':'ctx-old'}],0)
    snapshot={'history':[{'tool':'browser_read','result':old}]}
    messages,_=prepare_request('Inspect current evidence',[],window,snapshot,archive)
    rendered=' '.join(str(m.content) for m in messages)
    assert 'incomplete_context' in rendered and 'excerpt' not in rendered.replace('audit excerpt','')
    # New values remain complete after process serialization, including the last control.
    value={'controls':[{'label':str(n),'href':'https://example.com/'+str(n)} for n in range(100)], 'text':'x'*10000}
    ref=archive.tool_result(value)['context_ref']
    restored=ContextArchive(json.loads(json.dumps(archive.values)))
    assert json.loads(restored.read(ref,path=['controls','99'])['content'])==value['controls'][99]


def test_history_and_replay_share_one_complete_reference():
    from eregion.runtime.request_context import ContextArchive, prepare_request
    from eregion.runtime.core.context import ConversationWindow
    archive=ContextArchive()
    original={'observation_id':'fresh','text':'x'*16000,'controls':[{'ref':str(i),'label':str(i)} for i in range(80)]}
    value=archive.tool_result(original)
    window=ConversationWindow()
    call={'id':'c','name':'browser_read','args':{}}
    window.append({'goal':'Read'},'', [call],[value],0)
    snapshot={'history':[{'call_id':'c','tool':'browser_read','arguments':{},'result':value}]}
    assert 'result_in_tool_message' in window.snapshot(snapshot)['history'][0]
    messages,_=prepare_request('Read evidence',[],window,snapshot,archive)
    refs=set(archive.values)
    for _ in range(10):prepare_request('Read evidence',[],window,snapshot,archive)
    assert set(archive.values)==refs
    assert value==window.groups[-1]['results'][0]['value']


def test_new_failed_generating_and_empty_attempts_do_not_hide_success():
    from datetime import datetime,UTC,timedelta
    moment=datetime.now(UTC)
    oldpack=SimpleNamespace(pack_id='old',snapshot_id='snapshot',status=SimpleNamespace(value='approved'))
    newpack=SimpleNamespace(pack_id='new',snapshot_id='snapshot',status=SimpleNamespace(value='generating'))
    old=SimpleNamespace(pack_item_id='old-item',artifact_type='tailored_resume',source_question=None,
                        artifact_id='success',status=SimpleNamespace(value='approved'),updated_at=moment)
    workspace=SimpleNamespace(get_application=lambda _:SimpleNamespace(current_snapshot_id='snapshot'))
    for status,artifact in [('failed',None),('generating',None),('awaiting_review',None),('generating','old-draft')]:
        new=SimpleNamespace(pack_item_id='new-item',artifact_type='tailored_resume',source_question=None,
                            artifact_id=artifact,status=SimpleNamespace(value=status),updated_at=moment+timedelta(seconds=1))
        packs=SimpleNamespace(list_for_application=lambda _:[newpack,oldpack],items=lambda p:[new] if p=='new' else [old])
        workflow=SimpleNamespace(refresh_staleness=lambda p:newpack if p=='new' else oldpack)
        visible=current_materials(workspace,packs,workflow,'job')
        assert {i.pack_item_id for p,i in visible}=={'old-item','new-item'}
        assert latest_materials(workspace,packs,workflow,'job')[0][1]==new
        oldpack.status.value='stale'
        assert {i.pack_item_id for p,i in current_materials(workspace,packs,workflow,'job')}=={'new-item'}
        oldpack.status.value='approved'


def test_interview_timeout_keeps_answer_and_retry_key_survives_restart(setup):
    import copy
    controller,model,application_id,db=setup
    original=model.evaluate
    attempts=[]
    def timeout(context):attempts.append(context);raise TimeoutError('private response')
    model.evaluate=timeout
    state=controller.start(application_id,idempotency_key='timeout',target_question_count=3)
    answer='I built Python APIs.'
    failed=controller.answer(state.mock_interview_id,answer,expected_version=state.version,idempotency_key='once')
    assert failed.status.value=='failed' and len(attempts)==1
    restarted=copy.copy(controller)
    again=restarted.answer(state.mock_interview_id,answer,expected_version=state.version,idempotency_key='once')
    assert len(attempts)==1 and again.version==failed.version
    failed2=restarted.resume(state.mock_interview_id,expected_version=failed.version,idempotency_key='retry-once')
    replay=restarted.resume(state.mock_interview_id,expected_version=failed.version,idempotency_key='retry-once')
    assert len(attempts)==2 and replay.version==failed2.version
    model.evaluate=original
    recovered=restarted.resume(state.mock_interview_id,expected_version=failed2.version,idempotency_key='provider-restored')
    assert recovered.status.value=='awaiting_answer'
    first=restarted.interviews.turns(state.mock_interview_id)[0]
    assert first['answer']['original_text']==answer and first['evaluation']


def test_education_headers_render_once_and_keep_qualification():
    source=ResumeSourceEntry(source_entry_id='school',entry_type='education',heading='MEng Systems',
        organization='Example School',end_date='May 2028 (expected)',evidence_ids=['header','course'])
    evidence={'header':'Example School\nMEng Systems\nMay 2028 (expected)', 'course':'Coursework: Statistics.'}
    result=ensure_foundational_resume_sections(TailoredResume(sections=[]),[source],evidence)
    entry=result.sections[0].entries[0]
    assert entry.end_date=='May 2028 (expected)' and [c.text for c in entry.bullets]==['Coursework: Statistics.']
    assert ensure_foundational_resume_sections(result,[source],evidence)==result


def test_history_envelopes_never_rearchive_context_pages():
    from eregion.runtime.request_context import ContextArchive,prepare_request
    from eregion.runtime.core.context import ConversationWindow
    archive=ContextArchive()
    ref=archive.reference({'controls':[{'label':'Engineer'}]*120})['context_ref']
    page=archive.read(ref)
    history=[{'tool':'read_context','arguments':{'ref':ref},'result':page} for _ in range(16)]
    old=archive.reference(history)['context_ref']
    assert archive.read(old)['error_code']=='history_context'
    for _ in range(16):
        messages,_=prepare_request('task',[],ConversationWindow(),{'history':history,'research_pages':[{'url':'https://example.com','links':[{'label':'Engineer','url':'https://example.com/role'}]}]},archive,max_chars=1500)
        snapshot=json.loads(messages[-1].content)
        assert snapshot['history']==[] and snapshot['research_pages'][0]['links'][0]['label']=='Engineer'
    assert len(archive.values)==2


def test_persisted_budget_is_not_reset_on_worker_restart(database):
    from test_task_loop import Harness,ToolModel,settled
    from eregion.runtime.tasks.loop import TaskLoop
    from eregion.runtime.career.repository import TaskRepository
    repository=TaskRepository(database.session_factory)
    state,_=repository.create('Continue existing work.','restart-exhausted-budget')
    repository.change(state['task_id'],model_budget=2,model_calls=2,budget_base=0,status='running')
    called=[]
    def decide(snapshot):called.append(snapshot);return ('finish_task',{'text':'should not call model'})
    loop=TaskLoop(repository,Harness(),model_factory=lambda:ToolModel(decide),budget=100)
    try:
        final=settled(repository,state['task_id'])
        assert final['status']=='needs_input' and final['model_calls']==2 and not called
        assert final['model_budget']==2 and final['budget_base']==0
    finally:loop.close()


def test_older_success_requires_current_explicit_selection_before_upload(career):
    c=career
    job,first=prepare(c);approve(c,first)
    app=c.runtime.workspace.get_application(job['application_id'])
    old_resume=next(i for i in c.runtime.packs.items(first['pack_id']) if i.artifact_type=='tailored_resume')
    newer=c.runtime.pack_workflow.create(app.application_id,expected_version=app.version,idempotency_key='new-failed-pack')
    c.runtime.pack_workflow.generate_many(newer.pack_id,['tailored_resume'],idempotency_key='newer')
    item=c.runtime.packs.items(newer.pack_id)[0]
    c.runtime.packs.fail_item(newer.pack_id,item.pack_item_id,expected_version=item.version)
    item=c.runtime.packs.item(newer.pack_id,item.pack_item_id)
    with pytest.raises(ValueError,match='review'):
        execute(c,'start_application',application_id=app.application_id)
    with pytest.raises(ValueError,match='explicitly review'):
        execute(c,'start_application',application_id=app.application_id,pack_id=first['pack_id'])
    c.repository.change(c.state['task_id'],material_selections={'tailored_resume':{
        'pack_id':first['pack_id'],'item_id':old_resume.pack_item_id,'version':old_resume.version,
        'superseding_item_id':item.pack_item_id,'superseding_version':item.version}})
    result=execute(c,'start_application',application_id=app.application_id,pack_id=first['pack_id'])
    assert result['material_refs']['tailored_resume']=={'pack_id':first['pack_id'],'item_id':old_resume.pack_item_id,'version':old_resume.version}


def test_restored_skill_lists_pass_the_shared_structural_validator():
    from eregion.domain.quality import validate_resume_structure
    source = ResumeSourceEntry(source_entry_id='skills', entry_type='skills', heading='Tools', evidence_ids=['e'])
    text = 'Python, SQL; Tableau and Excel. AWS Certified Cloud Practitioner, earned August 2025. No Kubernetes experience.'
    result = ensure_foundational_resume_sections(TailoredResume(sections=[]), [source], {'e': text})
    claims = [c.text for c in result.claims()]
    assert 'Python' in claims and 'SQL' in claims
    assert 'AWS Certified Cloud Practitioner (earned August 2025)' in claims
    assert not any('Kubernetes' in c for c in claims)
    assert not any(i.code == 'unformatted_skill_block' for i in validate_resume_structure(
        result, [source], known_evidence_ids={'e'}))


def test_pdf_pages_balance_entries_without_shrinking_or_dropping_content():
    from eregion.domain.pdf_rendering import _balanced_page_breaks
    assert _balanced_page_breaks([210, 180, 190, 110, 130], 700) == {2}
    assert _balanced_page_breaks([210, 180], 700) == set()
    assert _balanced_page_breaks([800, 180], 700) == set()


def test_automatic_selection_only_uses_the_current_explicit_per_document_choice():
    from eregion.runtime.application_pack.selection import reviewed_selections
    def item(identifier, status, version=2):
        return SimpleNamespace(artifact_type='tailored_resume',source_question=None,
            pack_item_id=identifier,status=SimpleNamespace(value=status),artifact_id='artifact',
            version=version)
    old, new = item('old','approved'),item('new','failed')
    p1,p2 = SimpleNamespace(pack_id='p1'),SimpleNamespace(pack_id='p2')
    choice={'tailored_resume':{'pack_id':'p1','item_id':'old','version':2,
        'superseding_item_id':'new','superseding_version':2}}
    assert reviewed_selections([(p2,new)],[(p1,old),(p2,new)],{})==[(p2,new)]
    assert reviewed_selections([(p2,new)],[(p1,old),(p2,new)],choice)==[(p1,old)]
    # Approval in another chat/API changes the superseding item; never switch silently.
    new.version=3;new.status.value='approved'
    with pytest.raises(ValueError,match='explicitly selected'):
        reviewed_selections([(p2,new)],[(p1,old),(p2,new)],choice)
    new.version=2
    with pytest.raises(ValueError,match='explicitly selected'):
        reviewed_selections([(p2,new)],[(p2,new)],choice)
    old.version=3
    with pytest.raises(ValueError,match='explicitly selected'):
        reviewed_selections([(p2,new)],[(p1,old),(p2,new)],choice)
    with pytest.raises(ValueError,match='no longer current'):
        reviewed_selections([],[],choice)
    old.version=2
    assert reviewed_selections([(p2,new)],[(p1,old),(p2,new)],choice)==[(p1,old)]

def test_complete_real_source_sections_survive_restoration_and_pdf():
    from pathlib import Path
    from io import BytesIO
    from pypdf import PdfReader
    from eregion.domain.domain import normalize_resume_analysis
    from eregion.domain.quality import normalize_generated_resume_metadata, clean_tailored_resume, validate_resume_structure
    from eregion.domain.pdf_rendering import render_tailored_resume_pdf
    import re
    for name, expected_name in [('avery','AVERY LIN'),('riley','RILEY NG')]:
        text=(Path(__file__).parent/'fixtures/resume_source_regressions'/f'{name}.txt').read_text(encoding='utf-8')
        partial=ResumeAnalysis(summary='',skills=[],education=[],evidence=[
            ResumeEvidence(evidence_id='partial',source_entry_id='skill',source_section='Skills',
                exact_text='data validation')],
            source_entries=[ResumeSourceEntry(source_entry_id='skill',entry_type='skills',heading='Skills',evidence_ids=['partial'])])
        completed=normalize_resume_analysis(complete_source_evidence(partial,text))
        assert normalize_resume_analysis(complete_source_evidence(completed,text))==completed
        evidence={e.evidence_id:e.exact_text for e in completed.evidence}
        assert all(e in text for e in evidence.values())
        restored=ensure_foundational_resume_sections(TailoredResume(sections=[]),completed.source_entries,evidence)
        restored=normalize_generated_resume_metadata(restored,completed.source_entries,source_resume_text=text)
        restored,_=clean_tailored_resume(restored)
        assert restored.header.name==expected_name
        assert any('linkedin.com/' in line for line in restored.header.contact_lines)
        assert any('github.com/' in line for line in restored.header.contact_lines)
        assert all(not re.fullmatch(r'\d+',x) and 'SYNTHETIC' not in x and expected_name not in x for x in restored.header.contact_lines)
        skills=[c.text for section in restored.sections if section.section_type=='skills' for entry in section.entries for c in entry.bullets]
        assert 'Python' in skills and 'SQL' in skills, skills
        assert 'Technical' not in skills and 'communication' not in skills
        assert not any('Kubernetes' in x or 'No claim' in x for x in skills)
        assert not any(i.code=='unformatted_skill_block' for i in validate_resume_structure(restored,completed.source_entries,known_evidence_ids=set(evidence)))
        rendered='\n'.join(page.extract_text() for page in PdfReader(BytesIO(render_tailored_resume_pdf(restored))).pages)
        assert re.search(r'\bPython\b',rendered) and re.search(r'\bSQL\b',rendered)
        assert 'SYNTHETIC QA' not in rendered

def test_real_snapshot_education_case_does_not_duplicate_degrees():
    from pathlib import Path
    from datetime import datetime, UTC
    from uuid import uuid4
    from eregion.runtime.application_pack.types import GenerationEvidenceSnapshot, EvidenceSnapshotItem
    from eregion.runtime.application_pack.workflow import resume_analysis_from_snapshot
    from eregion.domain.domain import normalize_resume_analysis
    original=(Path(__file__).parent/'fixtures/resume_source_regressions/avery.txt').read_text(encoding='utf-8')
    raw=ResumeAnalysis.model_validate_json((Path(__file__).parent/'fixtures/resume_source_regressions/avery-analysis.json').read_text())
    analysis=normalize_resume_analysis(complete_source_evidence(raw,original))
    evidence={e.evidence_id:e for e in analysis.evidence}
    items=[]
    for source in analysis.source_entries:
        if source.entry_type!='education':continue
        for section in ['EDUCATION','Education']:
            for key in source.evidence_ids:
                items.append(EvidenceSnapshotItem(evidence_id=str(uuid4()),evidence_version_id=str(uuid4()),
                    content_hash='a'*64,source_type='resume',selection_reason='resume_foundation',associated_requirement_ids=[],
                    claim_text=evidence[key].exact_text,exact_quote=evidence[key].exact_text,
                    category='education',source_section=section,employer_or_project=source.heading,
                    role=source.organization,start_date=source.start_date,end_date=source.end_date))
    # The real grouping function consumes snapshot.items; unrelated metadata is not needed here.
    result=resume_analysis_from_snapshot(SimpleNamespace(items=items))
    assert len(result.source_entries)==2
    final=ensure_foundational_resume_sections(TailoredResume(sections=[]),result.source_entries,
        {e.evidence_id:e.exact_text for e in result.evidence})
    assert len(next(s for s in final.sections if s.section_type=='education').entries)==2


def test_real_pdf_line_wraps_preserve_owned_facts_units_and_limitations():
    from pathlib import Path
    import textwrap
    from eregion.domain.domain import normalize_resume_analysis
    for name in ['avery','riley']:
        base=Path(__file__).parent/'fixtures/resume_source_regressions'
        text=(base/f'{name}.txt').read_text(encoding='utf-8')
        analysis=ResumeAnalysis.model_validate_json((base/f'{name}-analysis.json').read_text())
        for width in [None,45,105]:
            source=text if width is None else '\n'.join(textwrap.fill(line,width=width,
                break_long_words=False,break_on_hyphens=False) for line in text.splitlines())
            result=normalize_resume_analysis(complete_source_evidence(analysis,source))
            evidence={e.evidence_id:e.exact_text for e in result.evidence}
            assert normalize_resume_analysis(complete_source_evidence(result,source))==result
            assert all(' '.join(v.split()).casefold() in ' '.join(source.split()).casefold() for v in evidence.values())
            if name=='riley':
                role=next(s for s in result.source_entries if s.organization and 'Silver Birch' in s.organization)
                assert len(role.evidence_ids)==1
                quote=' '.join(evidence[role.evidence_ids[0]].split())
                assert '34 to 22 minutes' in quote and 'missing identifiers' in quote
                assert 'Had no responsibility for Kubernetes administration' in quote
                assert 'Coastline Information' not in quote


def test_real_skill_snapshot_selection_restoration_keeps_only_complete_section():
    from pathlib import Path
    from eregion.runtime.application_pack.types import EvidenceSnapshotItem
    from eregion.runtime.application_pack.policy import EvidenceSelectionPolicy
    from eregion.runtime.application_pack.workflow import resume_analysis_from_snapshot
    from eregion.runtime.evidence.types import EvidenceCategory, EvidenceSourceType, EvidenceStatus
    from eregion.domain.schemas import JobAnalysis
    from eregion.domain.quality import affirmative_skill_clauses, validate_resume_structure
    raw=json.loads((Path(__file__).parent/'fixtures/resume_source_regressions/riley-fragment-snapshot.json').read_text())
    items=[EvidenceSnapshotItem.model_validate(i) for i in raw['items']]
    rows=[]
    for item in items:
        data=item.model_dump()
        data.update(category=EvidenceCategory(item.category), source_type=EvidenceSourceType.RESUME,
                    source_reference='same-real-resume', created_by='pack_resume_import')
        rows.append(SimpleNamespace(evidence_id=item.evidence_id,status=EvidenceStatus.CONFIRMED,current=SimpleNamespace(**data)))
    job=JobAnalysis(title='Data Workflow Engineer',summary='Python data engineering',requirements=[],responsibilities=[])
    selected=EvidenceSelectionPolicy().select(evidence=rows,links=[],job=job,token_budget=20000)
    skills=[i for i in selected if i.category=='skill']
    assert len(skills)==1,[(i.source_section,i.claim_text) for i in skills]
    assert all(not i.evidence_version_id.startswith(('6f076e89','0002c5a2','fa4f5f67')) for i in selected)
    restored_source=resume_analysis_from_snapshot(SimpleNamespace(items=skills))
    evidence={e.evidence_id:e.exact_text for e in restored_source.evidence}
    result=ensure_foundational_resume_sections(TailoredResume(sections=[]),restored_source.source_entries,evidence)
    claims=[c.text for c in result.claims()]
    assert set(claims)==set(affirmative_skill_clauses(skills[0].claim_text))
    assert 'Python' in claims and 'SQL' in claims and 'experimental design' in claims
    assert not {'design','2025','Methods: experimental'} & set(claims)
    assert not validate_resume_structure(result,restored_source.source_entries,known_evidence_ids=set(evidence))
    # Identical text does not erase ownership or sources outside an explicit skills section.
    full=skills[0]
    base=rows[0].current
    for changes in [dict(category=EvidenceCategory.EXPERIENCE,source_section='Experience',employer_or_project='Different Employer'),
                    dict(source_reference='different-resume'),dict(source_section='Project Skills')]:
        fragment=SimpleNamespace(**{**vars(base),**changes,'evidence_id':'isolated','evidence_version_id':'isolated-v','claim_text':'Python','exact_quote':'Python'})
        chosen=EvidenceSelectionPolicy().select(evidence=[rows[0],SimpleNamespace(evidence_id='isolated',status=EvidenceStatus.CONFIRMED,current=fragment)],links=[],job=job,token_budget=20000)
        assert {i.evidence_id for i in chosen}=={full.evidence_id,'isolated'}


def test_claim_verifier_passes_cited_ownership_and_specific_feedback():
    from eregion.runtime.application_pack.verifier import ArtifactVerifier
    from eregion.runtime.application_pack.types import EvidenceSnapshotItem
    from eregion.runtime.semantics import SupportedClaims, ClaimDecision
    captured=[]
    evidence=EvidenceSnapshotItem(evidence_id='e',evidence_version_id='v',content_hash='a'*64,
        source_type='resume',selection_reason='application_link',associated_requirement_ids=[],
        claim_text='Built Python jobs. Processing fell from 34 to 22 minutes. No Kubernetes responsibility.',
        exact_quote='Built Python jobs. Processing fell from 34 to 22 minutes. No Kubernetes responsibility.',
        category='experience',source_section='Experience',employer_or_project='Example Org',role='Engineer',start_date='2024')
    def judge(items):
        captured.extend(items)
        return SupportedClaims([ClaimDecision(claim_id=i['claim_id'],supported=False,reason='The cited unit is minutes, not milliseconds.') for i in items])
    from eregion.domain.schemas import SupportedClaim
    content=TailoredResume(professional_summary=[SupportedClaim(
        text='At Example Org I reduced processing to 22 milliseconds.',
        evidence_ids=['v'],source_entry_id='source')],experience_bullets=[],highlighted_skills=[]).model_dump(mode='json')
    result=ArtifactVerifier(judge).verify(content,'tailored_resume',[evidence])
    assert not result.passed and 'minutes, not milliseconds' in result.issues[0].reason
    cited=captured[0]['evidence'][0]
    assert cited['employer_or_project']=='Example Org' and cited['role']=='Engineer' and cited['start_date']=='2024'
    assert cited['quote']==evidence.exact_quote


def test_large_domain_result_keeps_identity_and_outcome_on_replay():
    from eregion.runtime.request_context import ContextArchive, compact_snapshot
    archive=ContextArchive()
    result={'application_id':'job-one','status':'matched','match_score':91.3,
        'requirement_counts':{'matched':28,'partial':4,'missing':3},'gaps':['Kubernetes'],
        'description':'Full job source. '*1000}
    summary=archive.tool_result(result)
    assert summary['application_id']=='job-one' and summary['match_score']==91.3
    assert summary['status']=='matched' and summary['gaps']==['Kubernetes']
    assert archive.read(summary['context_ref'],path=['description'])['chars']>10000
    replay=compact_snapshot({'history':[{'tool':'score_job','arguments':{},'result':result}]},archive)
    assert replay['history'][0]['result']['match_score']==91.3
    assert replay['history'][0]['result']['application_id']=='job-one'


def test_import_uses_observed_posting_instead_of_task_instruction(career):
    c=career
    url='https://jobs.lever.co/example/actual-job'
    body='Example Python Engineer. Build Python APIs and validate structured datasets. Required: Python and SQL. Location: Remote.'
    c.repository.change(c.state['task_id'],retrieved_jobs={url:{'url':url,'description':body,'title':'Example Python Engineer','source':'rendered_browser'}})
    message=next(m for m in c.state['messages'] if m['role']=='user')
    job=execute(c,'import_job',url=url,message_id=message['id'],title='Example Python Engineer')
    assert job['description']==body and job['description']!=message['text']
    with pytest.raises(ValueError,match='message ID alone'):
        execute(c,'import_job',url='https://jobs.lever.co/example/unread',message_id=message['id'])
    messages=[*c.state['messages'],{'id':'pasted-jd','role':'user','text':'Please analyze this JD:\n'+body+'\nThank you.'}]
    c.repository.change(c.state['task_id'],messages=messages)
    with patch('eregion.runtime.semantics.SemanticDecisions.pasted_job_text',return_value=True):
        with pytest.raises(ValueError,match='posting title'):  # asked for, not recorded as a rejection
            execute(c,'import_job',url='https://jobs.lever.co/example/pasted',message_id='pasted-jd',job_text=body)
        pasted=execute(c,'import_job',url='https://jobs.lever.co/example/pasted',message_id='pasted-jd',job_text=body,
                       title='Example Python Engineer')
    assert pasted['description']==body and not c.repository.get(c.state['task_id']).get('candidate_rejections')
    with pytest.raises(ValueError,match='literal'):
        execute(c,'import_job',url=url,message_id='pasted-jd',job_text=body+' fabricated requirement')


def test_job_quote_cannot_override_observed_web_even_when_verbatim(career):
    c=career;url='https://harbor.qa.example.com/alternate/data-workflow'
    goal='Using my saved resume, browse https://harbor.qa.example.com/alternate/jobs and find a suitable job. Import the real posting, analyze my fit, give the score and evidence gaps, then stop at this checkpoint.'
    body='Data Workflow Engineer at Harbor Research Systems. Required: Python and SQL. Build data pipelines, validate structured records and collaborate with analysts. Location: Toronto.'
    c.repository.change(c.state['task_id'],messages=[{'id':'task','role':'user','text':goal}],
        retrieved_jobs={url:{'url':url,'description':body,'source':'rendered_browser'}})
    with patch('eregion.runtime.semantics.SemanticDecisions.pasted_job_text',side_effect=AssertionError('No extra classification needed for a captured page')):
        result=execute(c,'import_job',url=url,message_id='task',job_text=goal,title='Data Workflow Engineer')
    assert result['description']==body and result['title']=='Data Workflow Engineer'  # title verified in the page text
    c.repository.change(c.state['task_id'],retrieved_jobs={})
    with patch('eregion.runtime.semantics.SemanticDecisions.pasted_job_text',return_value=False) as judge:
        for _ in range(2):
            with pytest.raises(ValueError,match='not identifiable'):
                execute(c,'import_job',url=url,message_id='task',job_text=goal)
        assert judge.call_count==1  # Failed provenance is cached for the same immutable quote.
    short='招聘数据工程师，需掌握 Python、SQL。'
    c.repository.change(c.state['task_id'],messages=[{'id':'short','role':'user','text':'分析这个职位：'+short}])
    with patch('eregion.runtime.semantics.SemanticDecisions.pasted_job_text',return_value=True):
        result=execute(c,'import_job',url='https://harbor.qa.example.com/short',message_id='short',job_text=short,title='数据工程师')
    assert result['description']==short


def test_archive_read_progress_survives_restart_and_context_eviction():
    from eregion.runtime.request_context import ContextArchive, prepare_request
    from eregion.runtime.core.context import ConversationWindow
    archive=ContextArchive();ref=archive.reference({'report':'whole source '*3000,'score':83.7})['context_ref']
    first=archive.read(ref);assert first['read_progress']['next_unread_offset']==3000
    archive.read(ref,3000)
    restored=ContextArchive(json.loads(json.dumps(archive.values)),json.loads(json.dumps(archive.reads)))
    repeated=restored.read(ref,0)
    assert 'already read' in repeated['progress_feedback']
    assert repeated['read_progress']['ranges']==[[0,9000]]
    assert repeated['read_progress']['next_unread_offset']==9000
    assert repeated['offset']==6000 and repeated['requested_offset']==0
    assert restored.read(ref,0,reread=True)['content']==first['content']  # Explicit full-source revisit.
    restored.read(ref,path=['score'])
    assert restored.reading_progress()[-1]['next_unread_offset'] is None
    messages,_=prepare_request('Task',[],ConversationWindow(),{'history':[]},restored)
    live=json.loads(messages[-1].content)
    assert live['context_read_progress'][0]['ranges']==[[0,9000]]
    assert all('content' not in row for row in live['context_read_progress'])


def test_job_summary_never_presents_an_old_snapshot_score_as_current(career):
    c=career
    job=execute(c,'import_job',url='https://jobs.lever.co/example/snapshot')
    aid=job['application_id'];execute(c,'score_job',application_id=aid)
    assert c.harness.job_summary(aid)['match_score'] is not None
    c.runtime.workspace.save_workspace(cleaned_job_description='A new posting requiring Go and SQL.',
        source_url=job['url'],title=job['title'])
    assert c.harness.job_summary(aid)['match_score'] is None


def test_score_overview_is_actionable_without_repeated_citation_pages():
    from eregion.runtime.career.harness import CareerHarness
    from eregion.runtime.request_context import ContextArchive
    report={'explanation':'3 matched; 1 requires confirmation.','matches':[
        {'job_skill':skill,'match_status':status,'requirement_level':'required','resume_evidence':['complete evidence '*500]}
        for skill,status in [('Python','matched'),('SQL','matched'),('API','partial'),('sponsorship','needs_confirmation')]]}
    result={'application_id':'a','match_score':83.7,'match_report':report,'match_overview':CareerHarness.match_overview(report)}
    archived=ContextArchive().tool_result(result)
    assert archived['match_overview']['matched'][0]['requirement']=='Python'
    assert archived['match_overview']['needs_confirmation'][0]['requirement']=='sponsorship'
    assert archived['match_score']==83.7


def test_explicit_selection_cannot_silently_change_after_other_api_approval(career):
    c=career
    job,first=prepare(c);approve(c,first)
    aid=job['application_id'];old=next(i for i in c.runtime.packs.items(first['pack_id']) if i.artifact_type=='tailored_resume')
    app=c.runtime.workspace.get_application(aid)
    newer=c.runtime.pack_workflow.create(aid,expected_version=app.version,idempotency_key='other-chat-pack')
    c.runtime.pack_workflow.generate_many(newer.pack_id,['tailored_resume'],idempotency_key='other-chat-generate')
    new=c.runtime.packs.items(newer.pack_id)[0]
    c.repository.change(c.state['task_id'],material_selections={'tailored_resume':{
        'pack_id':first['pack_id'],'item_id':old.pack_item_id,'version':old.version,
        'superseding_item_id':new.pack_item_id,'superseding_version':new.version}})
    execute(c,'inspect_job',application_id=aid)
    # This review belongs to another entry point, not this conversation's choice.
    c.runtime.packs.review(newer.pack_id,new.pack_item_id,expected_version=new.version,approve=True)
    for extra in ({},{'pack_id':newer.pack_id},{'pack_id':first['pack_id']}):
        with pytest.raises(ValueError,match='explicitly selected'):
            execute(c,'start_application',application_id=aid,**extra)
    assert not c.repository.get(c.state['task_id'])['application_runs']
    # A different source snapshot invalidates that choice even when no latest row remains.
    c.runtime.workspace.save_workspace(cleaned_job_description='New posting: Go engineer building SQL systems.',
        source_url=job['url'],title=job['title'])
    with pytest.raises(ValueError,match='no longer current'):
        execute(c,'start_application',application_id=aid)


def test_material_statuses_stay_actionable_when_full_content_is_archived():
    from eregion.runtime.request_context import ContextArchive
    overview=[{'pack_item_id':'resume','artifact_type':'tailored_resume','status':'awaiting_review','version':4}]
    result={'pack_id':'p','status':'awaiting_review','material_overview':overview,'items':[{'text':'full resume '*3000}]}
    archived=ContextArchive().tool_result(result)
    assert [{k:v for k,v in item.items() if k!='document_text'} for item in archived['material_overview']]==overview
    assert archived['material_overview'][0]['document_text']['chars']>30000


def test_reading_old_resource_preserves_evidence_but_marks_status_historical_after_restart():
    from eregion.runtime.request_context import ContextArchive
    archive=ContextArchive()
    old={'run_id':'r','status':'running','version':4,'pending_fields':['major'],'fields':[{'value':'full'}]*1000}
    ref=archive.tool_result(old)['context_ref']
    current={'run_id':'r','status':'awaiting_review','version':6,'pending_fields':[]}
    archive.tool_result(current)
    restored=ContextArchive(archive.values,archive.reads,json.loads(json.dumps(archive.resources)))
    result=restored.read(ref,path=['fields','999'],historical=True)
    assert result['content']=='{"value":"full"}'
    assert result['historical'] is True and result['latest_observation']['pending_fields']==[]
    assert result['latest_observation']['status']=='awaiting_review'
    restored.tool_result(old)  # Out-of-order older version cannot replace newer state.
    assert restored.read(ref)['historical'] is True


def test_chat_education_correction_keeps_owned_siblings_and_literal_facts():
    from eregion.runtime.auto_apply.resume_profile import extract_chat_profile, ResumeFacts, ResumeEducation, ResumeEducationFact
    current={'education':[{'id':'masters','school':'Northern University','degree':'Master','field_of_study':''},
        {'id':'bachelors','school':'Western College','degree':'Bachelor','field_of_study':'Math'}]}
    text='My NU master major is Data Science. Graduation June 2025.'
    facts=ResumeFacts(education=[ResumeEducation(record_id='masters',facts=[
        ResumeEducationFact(field='field_of_study',value='Data Science',quote='My NU master major is Data Science.'),
        ResumeEducationFact(field='end_date',value='June 2025',quote='Graduation June 2025.'),
        ResumeEducationFact(field='start_date',value='2024',quote='Not in the user message')])])
    with patch('eregion.runtime.auto_apply.resume_profile.extract_facts',return_value=facts):
        parsed=extract_chat_profile(text,current_profile=current,model_factory=lambda:object())
    records=parsed['profile']['education']
    assert len(records)==2 and records[0]['field_of_study']=='Data Science'
    assert records[0]['school']=='Northern University' and records[0]['end_date']=='June 2025'
    assert records[0]['start_date']=='' and records[1]['field_of_study']=='Math'


def test_same_chat_profile_message_is_applied_once_and_does_not_undo_later_changes(career):
    c=career;message=next(m for m in c.state['messages'] if m['role']=='user')
    with patch('eregion.runtime.auto_apply.resume_profile.extract_chat_profile',return_value={'profile':{'city':'Toronto'},'message':None}) as parse:
        execute(c,'remember_profile',message_id=message['id'])
        saved=c.harness.applications.repository.saved_candidate()
        c.harness.applications.repository.save_candidate({**saved['profile'],'city':'Vancouver'})
        result=execute(c,'remember_profile',message_id=message['id'])
        assert result['already_processed'] and result['profile']['city']=='Vancouver' and parse.call_count==1


def test_approved_older_files_remain_selectable_after_another_approval(career):
    from eregion.runtime.application_pack.selection import current_materials,reviewed_selections,latest_materials
    c=career;job,first=prepare(c);approve(c,first)
    aid=job['application_id'];rt=c.runtime
    old=next(i for i in rt.packs.items(first['pack_id']) if i.artifact_type=='tailored_resume')
    app=rt.workspace.get_application(aid)
    newpack=rt.pack_workflow.create(aid,expected_version=app.version,idempotency_key='new-approved-visible')
    rt.pack_workflow.generate_many(newpack.pack_id,['tailored_resume'],idempotency_key='new-approved-resume')
    new=rt.packs.items(newpack.pack_id)[0];rt.packs.review(newpack.pack_id,new.pack_item_id,expected_version=new.version,approve=True)
    new=rt.packs.item(newpack.pack_id,new.pack_item_id)
    visible=current_materials(rt.workspace,rt.packs,rt.pack_workflow,aid)
    assert {old.pack_item_id,new.pack_item_id}<={i.pack_item_id for p,i in visible}
    choice={'tailored_resume':{'pack_id':old.pack_id,'item_id':old.pack_item_id,'version':old.version,
        'superseding_item_id':new.pack_item_id,'superseding_version':new.version}}
    selected=reviewed_selections(latest_materials(rt.workspace,rt.packs,rt.pack_workflow,aid),visible,choice)
    assert next(i for p,i in selected if i.artifact_type=='tailored_resume').pack_item_id==old.pack_item_id


def test_failed_profile_extraction_can_retry_same_message(career):
    c=career;message=next(m for m in c.state['messages'] if m['role']=='user')
    with patch('eregion.runtime.auto_apply.resume_profile.extract_chat_profile',side_effect=[
        {'profile':{},'retryable':True},{'profile':{'city':'Ottawa'},'retryable':False}]) as parse:
        execute(c,'remember_profile',message_id=message['id'])
        result=execute(c,'remember_profile',message_id=message['id'])
        assert parse.call_count==2 and result['profile']['city']=='Ottawa'


def test_explicit_review_can_select_visible_same_job_pack_without_extra_inspection(career):
    from eregion.runtime.career.service import CareerService
    c=career;job,pack=prepare(c)
    item=next(i for i in c.runtime.packs.items(pack['pack_id']) if i.artifact_type=='tailored_resume')
    c.repository.change(c.state['task_id'],packs=[])
    service=SimpleNamespace(runtime=c.runtime,repository=c.repository,harness=c.harness,chat=lambda *args,**kwargs:{'accepted':True})
    CareerService.review_material(service,c.state['task_id'],pack['pack_id'],item.pack_item_id,item.version,True)
    assert pack['pack_id'] in c.repository.get(c.state['task_id'])['packs']
    approved=c.runtime.packs.item(pack['pack_id'],item.pack_item_id)
    c.repository.change(c.state['task_id'],packs=[],jobs=[])
    with pytest.raises(ValueError,match='does not belong'):
        CareerService.review_material(service,c.state['task_id'],pack['pack_id'],item.pack_item_id,approved.version,True)


def test_document_choices_are_scoped_per_job_for_multi_application_chat(career):
    c=career;job,pack=prepare(c);approve(c,pack)
    item=next(i for i in c.runtime.packs.items(pack['pack_id']) if i.artifact_type=='tailored_resume')
    choice={'pack_id':item.pack_id,'item_id':item.pack_item_id,'version':item.version,
        'superseding_item_id':item.pack_item_id,'superseding_version':item.version}
    state={'material_selections':{'tailored_resume':choice}}
    assert c.harness.material_selections_for(state,job['application_id'])=={'tailored_resume':choice}
    assert c.harness.material_selections_for(state,'different-job')=={}
    state['material_selections_by_application']={job['application_id']:{'tailored_resume':choice},'different-job':{'cover_letter':{'pack_id':'other'}}}
    assert c.harness.material_selections_for(state,job['application_id'])=={'tailored_resume':choice}
    assert c.harness.material_selections_for(state,'different-job')=={'cover_letter':{'pack_id':'other'}}


def test_visible_same_job_materials_can_be_inspected_but_other_jobs_are_rejected(career):
    c=career;job,pack=prepare(c)
    c.repository.change(c.state['task_id'],packs=[])
    result=execute(c,'inspect_materials',pack_id=pack['pack_id'])
    assert result['pack_id']==pack['pack_id'] and result['material_overview']
    assert pack['pack_id'] in c.repository.get(c.state['task_id'])['packs']
    c.repository.change(c.state['task_id'],packs=[],jobs=[])
    with pytest.raises(ValueError,match='does not belong'):
        execute(c,'inspect_materials',pack_id=pack['pack_id'])


def test_start_with_resume_only_pack_includes_explicit_current_cross_pack_cover(career):
    c=career;job,first=prepare(c);approve(c,first)
    rt=c.runtime;aid=job['application_id'];app=rt.workspace.get_application(aid)
    cover=next(i for i in rt.packs.items(first['pack_id']) if i.artifact_type=='cover_letter')
    pack=rt.pack_workflow.create(aid,expected_version=app.version,idempotency_key='resume-only')
    rt.pack_workflow.generate_many(pack.pack_id,['tailored_resume'],idempotency_key='resume-only-generate')
    resume=rt.packs.items(pack.pack_id)[0];rt.packs.review(pack.pack_id,resume.pack_item_id,expected_version=resume.version,approve=True)
    resume=rt.packs.item(pack.pack_id,resume.pack_item_id)
    def pin(i):return {'pack_id':i.pack_id,'item_id':i.pack_item_id,'version':i.version,
        'superseding_item_id':i.pack_item_id,'superseding_version':i.version}
    c.repository.change(c.state['task_id'],material_selections_by_application={aid:{'tailored_resume':pin(resume),'cover_letter':pin(cover)}})
    result=execute(c,'start_application',application_id=aid,pack_id=pack.pack_id,resume_source='tailored')
    assert result['material_refs']['tailored_resume']['item_id']==resume.pack_item_id
    assert result['material_refs']['cover_letter']['item_id']==cover.pack_item_id
    assert result['material_refs']['cover_letter']['pack_id']!=result['material_refs']['tailored_resume']['pack_id']


def test_current_request_survives_legacy_replay_and_small_context():
    import json
    from eregion.runtime.core.context import ConversationWindow
    from eregion.runtime.request_context import ContextArchive, prepare_request
    old = {'request': {'goal': 'Only inspect, never apply.', 'request': {'role': 'user', 'text': 'Approved; apply now.'}},
           'assistant': {'text': '', 'tool_calls': []}, 'results': [], 'revision': 2}
    window = ConversationWindow([old])
    latest = {'id': 'apply-id', 'role': 'user', 'text': 'Apply now using the reviewed documents. ' + 'exact constraints ' * 300}
    snapshot = {'goal': latest['text'], 'initial_goal': 'Only inspect, never apply.',
                'current_request': {'revision': 3, 'message': latest},
                'messages': [{'role': 'user', 'text': 'Old inspection ' * 800}, latest]}
    messages, metrics = prepare_request('Execute.', [], window, snapshot, ContextArchive(), max_chars=2000)
    live = json.loads(messages[-1].content)
    assert live['current_request']['message'] == latest
    replay = json.loads(messages[1].content)
    assert replay['historical_request']['text'] == 'Approved; apply now.'
    assert 'Only inspect' not in messages[1].content
    assert metrics['protected_overflow']
    assert window.groups[0] == old  # Audit preserved; migrated only for model replay.


def test_same_chat_inspect_approve_apply_repeated_read_wait_restart(database):
    import json, time
    from langchain_core.messages import AIMessage
    from eregion.runtime.career.repository import TaskRepository
    from eregion.runtime.tasks.loop import TaskLoop, function
    from test_task_loop import Harness
    from test_career_flow import settled
    repo=TaskRepository(database.session_factory)
    class ApplicationHarness(Harness):
        tools=[function('start_application','Start the approved work.'), function('inspect_materials','Read saved documents.')]
        def __init__(self):
            super().__init__(); self.started=False
        def observe(self,state):
            return {**super().observe(state), 'applications': [{'run_id':'child', 'status':'awaiting_review' if self.ready else 'running'}] if self.started else []}
        def execute(self,task_id,name,args,**kw):
            if name=='start_application': self.started=True; return {'run_id':'child','status':'running'}
            return {'pack_id':'pack','status':'approved','content':'full document ' * 700}
        def wait_after_repeated_read(self,state,tool):
            return {'kind':'application','id':'child'} if self.started and not self.ready and tool=='read_context' else None
    harness=ApplicationHarness()
    class Model:
        calls=0
        def invoke(self,messages,tools):
            self.calls+=1
            live=json.loads(messages[-1].content)
            request=live['current_request']['message']['text']
            assert live['goal']==request
            if request in {'Only inspect.', 'I approve these documents.'}:
                return AIMessage(content='',tool_calls=[{'id':str(self.calls),'name':'finish_task','args':{'text':'Inspected.' if request.startswith('Only') else 'Approved.'}}])
            assert request=='Apply now; stop at final review.'
            assert live['initial_goal']=='Only inspect.'
            if harness.ready:
                assert live['applications'][0]['status']=='awaiting_review'
                return AIMessage(content='',tool_calls=[{'id':str(self.calls),'name':'finish_task','args':{'text':'The approved files are filled. The application awaits final review; it has not been submitted.'}}])
            if not harness.started: name,args='start_application',{}
            else:
                refs=[]
                for m in messages:
                    if getattr(m,'type','')=='tool':
                        v=json.loads(m.content)
                        if v.get('context_ref'): refs.append(v['context_ref'])
                name,args=('read_context',{'ref':refs[-1],'offset':0,'reread':True}) if refs else ('inspect_materials',{})
            return AIMessage(content='',tool_calls=[{'id':str(self.calls),'name':name,'args':args}])
    model=Model();loop=TaskLoop(repo,harness,model_factory=lambda:model,budget=20)
    try:
        task=loop.start('Only inspect.','current-request-restart');tid=task['task_id'];settled(repo,tid)
        loop.chat(tid,'I approve these documents.','review');settled(repo,tid)
        loop.chat(tid,'Apply now; stop at final review.','apply');waiting=settled(repo,tid)
        assert waiting['status']=='waiting'
        assert waiting['model_calls']==6 and waiting['budget_base']==2
        assert waiting['history'][-1]['result']['background_wait']['waiting']
        count=waiting['model_calls'];time.sleep(.1);assert repo.get(tid)['model_calls']==count
    finally:loop.close()
    harness.ready=True;harness.version+=1
    recovered=TaskLoop(repo,harness,model_factory=lambda:model,budget=20)
    try:
        deadline=time.monotonic()+5
        while repo.get(tid)['status']!='completed' and time.monotonic()<deadline:time.sleep(.02)
        final=repo.get(tid)
        assert final['status']=='completed'
        assert final['model_calls']==7 and final['budget_base']==2
        assert final['messages'][-1]['text'].startswith('The approved files are filled.')
        assert sum(h['tool']=='start_application' for h in final['history'])==1
    finally:recovered.close()


def test_progress_text_after_work_cannot_complete_task(database):
    import json
    from langchain_core.messages import AIMessage
    from eregion.runtime.career.repository import TaskRepository
    from eregion.runtime.tasks.loop import TaskLoop
    from test_task_loop import Harness
    from test_career_flow import settled
    repo=TaskRepository(database.session_factory);harness=Harness()
    class Model:
        calls=0
        def invoke(self,messages,tools):
            self.calls+=1
            if self.calls==2:
                return AIMessage(content='First source checked. Je vais continuer avec la deuxieme source.')
            if self.calls==3:
                live=json.loads(messages[-1].content)
                assert live['runtime_feedback']['completion_required']
                assert repo.list()[0]['status']=='running'
            name='lookup' if self.calls in (1,3) else 'finish_task'
            return AIMessage(content='',tool_calls=[{'id':str(self.calls),'name':name,
                'args':{} if name=='lookup' else {'text':'Both requested sources were checked.'}}])
    model=Model();loop=TaskLoop(repo,harness,model_factory=lambda:model)
    try:
        task=loop.start('Check two independent sources.','progress-not-complete');s=settled(repo,task['task_id'])
        assert s['status']=='completed' and s['model_calls']==4 and harness.lookups==2
        assert s['messages'][-1]['text']=='Both requested sources were checked.'
    finally:loop.close()


def test_explicit_completion_before_first_action_and_new_apply_turn(database):
    from langchain_core.messages import AIMessage
    from eregion.runtime.career.repository import TaskRepository
    from eregion.runtime.tasks.loop import TaskLoop
    from test_task_loop import Harness
    from test_career_flow import settled
    import json
    repo=TaskRepository(database.session_factory);harness=Harness()
    class Model:
        def __init__(self):self.phases={}
        def invoke(self,messages,tools):
            live=json.loads(messages[-1].content);text=live['current_request']['message']['text']
            count=self.phases.get(text,0)+1;self.phases[text]=count
            if text=='hi':
                return AIMessage(content='',tool_calls=[{'id':'hi','name':'finish_task','args':{'text':'Hello. How can I help?'}}])
            if count==1:
                assert not any(h.get('chat_revision')==live['current_request']['revision'] for h in live['history'])
                return AIMessage(content='I will inspect the three sources now.' if text=='Inspect sources.' else 'I will apply now using your approved documents.')
            if count==2:
                assert live['runtime_feedback']['completion_required']
                assert repo.list()[0]['status']=='running'
            return AIMessage(content='',tool_calls=[{'id':text+str(count),'name':'lookup' if count==2 else 'finish_task',
                'args':{} if count==2 else {'text':'The requested work is now done.'}}])
    model=Model();loop=TaskLoop(repo,harness,model_factory=lambda:model)
    try:
        task=loop.start('Inspect sources.','first-action-promise');tid=task['task_id'];s=settled(repo,tid)
        assert s['model_calls']==3 and harness.lookups==1
        loop.chat(tid,'Apply now.','apply');s=settled(repo,tid)
        assert s['model_calls']==6 and s['budget_base']==3 and harness.lookups==2
        loop.chat(tid,'hi','greeting');s=settled(repo,tid)
        assert s['status']=='completed' and s['model_calls']==7 and harness.lookups==2
        assert s['messages'][-1]['text']=='Hello. How can I help?'
    finally:loop.close()


def test_archive_stale_zero_offsets_advance_until_complete_and_allow_revisit():
    from eregion.runtime.request_context import ContextArchive
    a=ContextArchive();ref=a.reference({'items':[{'id':'a','text':'FULL SOURCE '*1200}]})['context_ref']
    pages=[]
    for _ in range(8):
        result=a.read(ref,path=['items','0','text']);pages.append(result)
        if result.get('complete'):break
    assert len(pages)<8
    assert [p['offset'] for p in pages[:-1]]==list(range(0,pages[0]['chars'],3000))
    assert pages[-1]['content']=='' and pages[-1]['complete']
    assert json.loads(''.join(p['content'] for p in pages))=='FULL SOURCE '*1200
    assert a.read(ref,path=['items','0','text'],reread=True)['content']==pages[0]['content']
    listing=a.read(ref,path=['items'])
    assert listing['available_paths'][0]['path']==['items','0']
    assert listing['available_paths'][0]['identity']=={'id':'a'}


def test_material_document_text_refs_bypass_metadata_without_truncation():
    from eregion.runtime.request_context import ContextArchive
    a=ContextArchive();text='complete source document '*1600
    result={'pack_id':'pack','status':'awaiting_review','items':[{'text':text,'metadata':'audit '*2000}],
            'material_overview':[{'pack_item_id':'item','artifact_type':'tailored_resume','status':'awaiting_review','version':5}]}
    summary=a.tool_result(result);doc=summary['material_overview'][0]['document_text']
    stored=json.loads(a.values[doc['context_ref']])['value']
    assert stored==text and len(stored)>30000
    assert json.loads(a.values[summary['context_ref']])['value']==result


def test_reread_visible_original_is_not_new_progress_and_survives_restart():
    from eregion.runtime.request_context import ContextArchive, prepare_request, encoded
    from eregion.runtime.core.context import ConversationWindow
    a=ContextArchive();source='A complete evidence sentence with its unit and owner. '*85
    ref=a.reference(source)['context_ref'];first=a.read(ref);a.read(ref,first['next_offset'])
    saved=json.loads(json.dumps([a.values,a.reads,a.resources]));a=ContextArchive(*saved)
    messages,_=prepare_request('Decide.',[],ConversationWindow(),{'current_request':{'message':{'text':'Inspect exact wording.'}}},a)
    live=json.loads(messages[-1].content)
    assert json.loads(''.join(v['content'] for v in live['context_evidence']))==source
    for _ in range(5):
        revisit=a.read(ref,reread=True)
        assert revisit['no_new_evidence'] and revisit['available_in_context'] and not revisit['content']
    # A purposeful revisit of an evicted range still retrieves the original.
    a.visible_ranges={}
    assert a.read(ref,reread=True)['content']==first['content']


def test_same_material_revision_projection_and_out_of_order_item_versions():
    import copy
    from eregion.runtime.request_context import ContextArchive
    a=ContextArchive()
    item={'pack_item_id':'resume','version':5,'status':'awaiting_review','verification':{'passed':True},'document_text_path':['items','0','text']}
    full={'pack_id':'pack','status':'awaiting_review','material_overview':[item], 'items':[{'text':'Full original '*800}]}
    ref=a.tool_result(full)['context_ref']
    summary={'pack_id':'pack','status':'awaiting_review','material_overview':[{**item,'verification':None,'document_text_path':None}]}
    a.record_resource(summary)
    assert not a.read(ref,path=['items','0','text']).get('historical')
    assert a.resources['pack_id:pack']['summary']['material_overview'][0]['verification']['passed']
    new=copy.deepcopy(summary);new['material_overview'][0].update(version=6,status='approved');new['status']='approved'
    a.record_resource(new)
    assert a.read(ref,path=['items','0','text'])['historical']
    assert a.resources['pack_id:pack']['summary']['material_overview'][0]['verification'] is None
    old=copy.deepcopy(summary);old['material_overview'][0]['version']=4;a.record_resource(old)
    read=a.read(ref,path=['items','0','text'])
    assert read['historical'] and read['latest_observation']['status']=='approved'
    assert read['latest_observation']['material_overview'][0]['version']==6


def test_recent_original_ranges_remain_available_under_conversation_pressure():
    from eregion.runtime.request_context import ContextArchive,prepare_request
    from eregion.runtime.core.context import ConversationWindow
    a=ContextArchive();text='CURRENT ORIGINAL '*280;ref=a.reference(text)['context_ref']
    v=a.read(ref);a.read(ref,v['next_offset'])
    latest={'role':'user','text':'Compare exact current original.'}
    snapshot={'current_request':{'message':latest},'messages':[{'role':'assistant','text':'Historical prose '*500} for _ in range(20)]+[latest]}
    messages,metrics=prepare_request('Use evidence.',[],ConversationWindow(),snapshot,a,max_chars=18000)
    live=json.loads(messages[-1].content)
    assert json.loads(''.join(v['content'] for v in live['context_evidence']))==text
    assert live['current_request']['message']==latest
    assert not metrics['protected_overflow']


def test_fresh_material_generation_is_explicit_and_idempotent(career):
    from test_career_flow import execute
    c=career;job,initial=prepare(c);state=c.repository.get(c.state['task_id']);tid=state['task_id']
    initial=execute(c,'generate_materials',application_id=job['application_id'],artifacts=['tailored_resume','cover_letter'])
    old_user=next(m for m in state['messages'] if m['role']=='user')
    c.repository.append(tid,id='fresh-request',role='user',text='Generate a fresh resume and cover letter, keeping the originals.')
    repeated=execute(c,'generate_materials',application_id=job['application_id'],artifacts=['tailored_resume','cover_letter'])
    assert repeated['pack_id']==initial['pack_id'] and 'Reused' in repeated['progress_feedback']
    with pytest.raises(ValueError,match='current user request'):
        execute(c,'generate_materials',application_id=job['application_id'],artifacts=['tailored_resume'],fresh=True,message_id=old_user['id'])
    fresh=execute(c,'generate_materials',application_id=job['application_id'],artifacts=['tailored_resume','cover_letter'],fresh=True,message_id='fresh-request')
    again=execute(c,'generate_materials',application_id=job['application_id'],artifacts=['cover_letter','tailored_resume'],fresh=True,message_id='fresh-request')
    assert fresh['pack_id']!=initial['pack_id'] and fresh['pack_id']==again['pack_id']
    assert [(i['pack_item_id'],i['version']) for i in fresh['items']]==[(i['pack_item_id'],i['version']) for i in again['items']]


def test_date_projection_preserves_precision_and_ambiguity():
    from eregion.runtime.auto_apply.education import format_date, combined_bindings
    field={'label':'Start YYYY-MM-DD'}
    for source in ('04/20/2026','20/04/2026','April 20, 2026','2026-04-20'):
        assert format_date(field,source)=='2026-04-20'
    for source in ('03/04/2026','2026-02-31','May 2026','20/20/2026'):
        assert format_date(field,source) is None
    assert format_date({'type':'month'},'May 2027 (expected)')=='2027-05'
    source='2027-05 (expected)'
    assert format_date({'type':'month'},source)=='2027-05'
    assert format_date({'type':'date'},source) is None
    assert source=='2027-05 (expected)'
    assert format_date({'type':'month'},'2027-05 (or June)') is None
    assert format_date({'label':'Graduation'},'May 2027 (expected)')=='May 2027 (expected)'
    assert format_date({'label':'MM/YYYY'},'April 2026')=='04/2026'
    good=[{'field_key':'one','component':c} for c in ('start_date','start_month','start_year')]
    assert combined_bindings(good)==[good[0]]
    for other in ('end_date','school','degree'):
        bad=good+[{'field_key':'one','component':other}]
        assert combined_bindings(bad)==bad


def test_legacy_fast_category_guess_is_not_reused_as_leaf_answer():
    from eregion.runtime.auto_apply.decisions import active_decision, signature
    from test_auto_apply import field
    f=field('How did you hear about us?','select')
    state={'profile':{},'answers':{f['key']:'LinkedIn'}}
    decision={'field':signature(f),'value':'Social Media','mode':'equivalent',
        'sources':{'answer:'+f['key']:'LinkedIn'},'original_answer':'LinkedIn',
        'reason':'Fast semantic mapping from the confirmed source to an observed option.'}
    state['field_decisions']={f['key']:decision}
    assert active_decision(f,state) is None
    decision['value']='LinkedIn'
    assert active_decision(f,state)==decision
    decision.update(value='Job Board',mode='category')
    assert active_decision(f,state)==decision


def test_review_projection_exposes_actual_controls_without_long_letter(career):
    from types import SimpleNamespace
    c=career;original=c.harness.applications
    c.harness.applications=SimpleNamespace(repository=SimpleNamespace(get=lambda rid:{
        'run_id':rid,'status':'awaiting_review','review_fields':[
            {'key':'source','label':'Source','kind':'select','value':'Social Media'},
            {'key':'date','label':'Graduation','kind':'text','value':'2027-05'},
            {'key':'letter','label':'Cover letter','kind':'textarea','value':'letter '*900}]}))
    try:
        result=c.harness.application('review')
        assert result['observed_answers'][0]['value']=='Social Media'
        assert result['observed_answers'][1]['value']=='2027-05'
        assert result['observed_answers'][2]['value']['filled']
        assert len(str(result))<2000
    finally:c.harness.applications=original


def test_dependent_choice_reuses_only_its_exact_confirmed_path_goal():
    from eregion.runtime.auto_apply.agent import AgentTools
    from eregion.runtime.auto_apply.browser import BrowserBlocked
    from test_auto_apply import FakeBrowser, field
    browser=FakeBrowser('');f=field('child','select','Which source?')
    f['options']=[{'label':'Portal X','value':'x'}];browser.fields=[f]
    state={'profile':{},'answers':{'parent':'Portal X'},'option_paths':{
        'parent':{'source_ref':'answer:parent','goal':'Portal X','field_keys':['parent','child']}}}
    session=AgentTools(browser,state,autofill=lambda:{},active=lambda:True)
    session.observe();assert session.source(f,'answer:parent')=='Portal X'
    unrelated={**f,'key':'unrelated'}
    with pytest.raises(BrowserBlocked):session.source(unrelated,'answer:parent')
    sensitive={**f,'label':'Authorization?'}
    with pytest.raises(BrowserBlocked):session.source(sensitive,'answer:parent')
    state['answers']['child']='Portal Y'
    with pytest.raises(BrowserBlocked):session.source(f,'answer:parent')
    state['answers'].pop('child');state['answers']['parent']='Changed'
    with pytest.raises(BrowserBlocked):session.source(f,'answer:parent')


def test_fast_generic_category_proposal_keeps_leaf_unresolved():
    from types import SimpleNamespace
    from eregion.runtime.auto_apply.agent import AgentTools
    from eregion.runtime.auto_apply.fast_options import fill_options
    from test_auto_apply import FakeBrowser, field
    class Browser(FakeBrowser):
        def explore_option(self,key,label):
            self.fields[0]['value']=label
            return []
    browser=Browser('');f=field('channel','select','Where did you hear about us?')
    f['options']=[{'label':'Networks','value':'network'},{'label':'Listings','value':'list'}]
    browser.fields=[f]
    state={'profile':{},'answers':{'channel':'Portal X'}}
    session=AgentTools(browser,state,autofill=lambda:{},active=lambda:True)
    session.fast_choices=lambda *args:SimpleNamespace(status='ok',answers={'f0':{'choice':'o0','confidence':1,'probabilities':{'o0':1,'o1':0,'unknown':0}}})
    assert fill_options(session)==[]
    assert state['field_decisions']['channel']['mode']=='category'
    assert state['option_paths']['channel']['goal']=='Portal X'
    assert not state['option_paths']['channel']['complete']


def test_working_application_defaults_to_progress_with_explicit_detail_access(career):
    from types import SimpleNamespace
    c=career;original=c.harness.applications
    child={'run_id':'run','status':'running','version':2,'fields':[{'key':'body','value':'long source '*2000}]}
    c.harness.applications=SimpleNamespace(repository=SimpleNamespace(get=lambda rid:child))
    try:
        summary=c.harness.application('run')
        assert 'fields' not in summary and summary['working'] and len(str(summary))<1000
        assert c.harness.application('run',include_fields=True)['fields'][0]['value']['filled']
        assert c.harness.application('run',field_key='body')['control']==child['fields'][0]
    finally:c.harness.applications=original


def test_duplicate_application_reply_never_dispatches_again_after_review(career):
    from types import SimpleNamespace
    from test_career_flow import execute
    c=career;state=c.repository.get(c.state['task_id']);message=next(m for m in state['messages'] if m['role']=='user')
    c.repository.link(state['task_id'],'application_runs','run')
    original=c.harness.applications
    def forbidden(*args,**kwargs):raise AssertionError('Duplicate message must not dispatch again')
    child={'run_id':'run','status':'awaiting_review','version':4,'messages':[message]}
    c.harness.applications=SimpleNamespace(repository=SimpleNamespace(get=lambda rid:child),chat=forbidden)
    try:
        for status in ('queued','running','awaiting_review','needs_input'):
            child['status']=status
            result=execute(c,'reply_application',run_id='run',message_id=message['id'])
            assert result['already_delivered'] and result['no_new_evidence']
            assert result['version']==4 and 'fields' not in result
    finally:c.harness.applications=original


def test_application_affordances_hide_filled_autofill_and_unrelated_answer_refs():
    from eregion.runtime.auto_apply.agent import AgentTools
    from eregion.runtime.auto_apply.runner import ApplicationHost
    from test_auto_apply import FakeBrowser,field
    browser=FakeBrowser('');browser.fields=[field('First name'),field('new-policy','select','Can you follow written access rules?')]
    browser.fields[0]['value']='Avery'
    state={'profile':{'first_name':'Avery'},'answers':{'old-policy':'Yes'}}
    session=AgentTools(browser,state,autofill=lambda:{},active=lambda:True)
    host=ApplicationHost(session,None,active=lambda:True,reserve=lambda kind:True,report=lambda *args:None,
        decisions=__import__('eregion.runtime.fast_decisions',fromlist=['FastDecisions']).FastDecisions())
    turn=host.observe()
    assert not turn.snapshot['autofill_available']
    assert 'autofill' not in {t['function']['name'] for t in turn.tools}
    # Empty per-field lists are omitted and refs every field shares are listed once (82e1ce9).
    offered=lambda f:set(f.get('allowed_source_refs',[]))|set(turn.snapshot.get('any_field_source_refs',[]))
    unknown=next(f for f in turn.snapshot['fields'] if f['key']=='new-policy')
    assert 'answer:old-policy' not in offered(unknown)
    state['answers']['new-policy']='Yes'
    turn=host.observe()
    assert turn.snapshot['autofill_available'] and 'autofill' in {t['function']['name'] for t in turn.tools}
    unknown=next(f for f in turn.snapshot['fields'] if f['key']=='new-policy')
    assert 'answer:new-policy' in offered(unknown)


def test_parent_tools_follow_live_browser_and_delivery_state(career):
    from types import SimpleNamespace
    c=career;state=c.repository.get(c.state['task_id']);user=next(m for m in reversed(state['messages']) if m['role']=='user')
    original=c.harness.applications;old_browser=c.harness._browsing
    c.harness.applications=SimpleNamespace(repository=SimpleNamespace(get=lambda rid:{'messages':[user]}))
    state['application_runs']=['run']
    try:
        names=lambda:{t['function']['name'] for t in c.harness.available_tools(state)}
        c.harness._browsing=None
        assert 'browser_open' in names() and 'browser_read' not in names() and 'reply_application' not in names()
        c.harness._browsing=SimpleNamespace(inspect=lambda tid:{'url':'https://example.com'})
        assert 'browser_read' in names()
        state['messages']=[*state['messages'],{'id':'new-answer','role':'user','text':'My answer is No.'}]
        assert 'reply_application' in names()
    finally:c.harness.applications=original;c.harness._browsing=old_browser


def test_actual_application_summary_survives_prepared_request_and_targeted_read(career):
    from types import SimpleNamespace
    from eregion.runtime.request_context import ContextArchive,prepare_request
    from eregion.runtime.core.context import ConversationWindow
    controls=[{'key':str(n),'label':'Observed '+str(n),'value':'2027-05','kind':'text',
        'constraints':{'description':'DOM metadata '*600}} for n in range(24)]
    controls.append({'key':'letter','label':'Cover letter','kind':'textarea','value':'Full original letter '*400})
    c=career;original=c.harness.applications
    c.harness.applications=SimpleNamespace(repository=SimpleNamespace(get=lambda rid:{
        'run_id':rid,'status':'awaiting_review','version':9,'fields':controls,'review_fields':controls}))
    try:
        result=c.harness.application('review',include_fields=True)
        assert 'review_fields' not in result and 'fields' not in result
        assert len(json.dumps(result))<4000
        assert c.harness.application('review',field_key='letter')['control']==controls[-1]
        snap={'current_request':{'message':{'text':'Report the actual prepared application.'}},
            'applications':[result],'history':[{'tool':'inspect_application','result':result}],
            'messages':[{'role':'assistant','text':'Earlier observation '*800} for _ in range(20)]}
        messages,_=prepare_request('Use observed results.',[],ConversationWindow(),snap,ContextArchive(),max_chars=18000)
        live=json.loads(messages[-1].content)
        assert live['applications'][0]['observed_answers'][23]['value']=='2027-05'
        assert live['history'][0]['result']['observed_answers'][23]['value']=='2027-05'
        assert 'DOM metadata' not in messages[-1].content
    finally:c.harness.applications=original


def test_superseded_evidence_does_not_replay_as_current_but_explicit_audit_survives_restart():
    from eregion.runtime.request_context import ContextArchive,prepare_request
    from eregion.runtime.core.context import ConversationWindow
    a=ContextArchive()
    old={'run_id':'app','version':4,'status':'needs_input','pending_fields':['Old question'],
        'review_fields':[{'label':'Source','value':'Social Media'}],'text':'OLD ORIGINAL '*800}
    ref=a.reference(old)['context_ref'];a.record_resource(old)
    first=a.read(ref);assert first['content']
    current={'run_id':'app','version':5,'status':'awaiting_review','pending_fields':[],
        'observed_answers':[{'label':'Source','value':'LinkedIn'}]}
    a.record_resource(current)
    a=ContextArchive(*json.loads(json.dumps([a.values,a.reads,a.resources])))
    redirected=a.read(ref,reread=True)
    assert redirected['historical'] and redirected['no_new_evidence'] and not redirected['content']
    assert redirected['latest_observation']['observed_answers'][0]['value']=='LinkedIn'
    assert not a.evidence_window()
    assert all('next_unread_offset' not in p for p in a.reading_progress())
    messages,_=prepare_request('Use live outcome.',[],ConversationWindow(),{'applications':[current],
        'history':[{'tool':'inspect_application','result':old},{'tool':'read_context','result':first}]},a)
    live=json.loads(messages[-1].content)
    assert 'OLD ORIGINAL' not in messages[-1].content and not live.get('context_evidence')
    explicit=a.read(ref,path=['review_fields'],historical=True)
    assert explicit['historical_access'] and 'Social Media' in explicit['content']
    pinned=a.evidence_window()
    assert pinned and all(p['historical'] for p in pinned)
    assert json.loads(a.values[ref])['value']==old
