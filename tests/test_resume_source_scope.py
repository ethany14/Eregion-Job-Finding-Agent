"""Old resume versions must not become extra employment or project entries."""
from types import SimpleNamespace

from eregion.runtime.application_pack.policy import EvidenceSelectionPolicy
from eregion.runtime.application_pack.workflow import resume_analysis_from_snapshot
from eregion.runtime.job_workflow.workers import EvidenceFreezeWorker
from eregion.domain.schemas import ResumeAnalysis, ResumeEvidence, ResumeSourceEntry
from test_application_pack import setup


def test_snapshot_omits_fully_covered_legacy_education_section_only():
    def item(key, text, school=None, date=None):
        return SimpleNamespace(evidence_version_id=key, claim_text=text,
            category='education', source_section='EDUCATION', source_type='resume',
            employer_or_project=school, role=None, start_date=None, end_date=date)
    first = item('first', 'Example University | MEng\nExpected May 2027', 'Example University', 'Expected May 2027')
    second = item('second', 'Other University | BA\nDec 2025', 'Other University', 'Dec 2025')
    aggregate = item('aggregate', first.claim_text + '\n' + second.claim_text)
    snapshot = SimpleNamespace(items=[aggregate, first, second], resume_entries={
        'aggregate': {'entry_type': 'education', 'heading': None, 'order': 0}})
    result = resume_analysis_from_snapshot(snapshot)
    assert len(result.source_entries) == 2
    assert {e.evidence_id for e in result.evidence} == {'first', 'second'}
    assert result.source_entries[0].end_date == 'Expected May 2027'
    assert len(snapshot.items) == 3  # Frozen provenance is unchanged.

    aggregate.claim_text += '\nUnresolved scholarship detail'
    result = resume_analysis_from_snapshot(snapshot)
    assert len(result.source_entries) == 3
    assert 'aggregate' in {e.evidence_id for e in result.evidence}


def source(role, quote):
    text = f'Test Candidate\nEXPERIENCE\nExample Corp\n{role}\n2022 - 2023\n{quote}'
    analysis = ResumeAnalysis(summary='', skills=[], education=[], evidence=[
        ResumeEvidence(evidence_id='E-1', source_section='Experience', exact_text=quote,
            source_entry_id='SRC-1')], source_entries=[ResumeSourceEntry(
                source_entry_id='SRC-1', entry_type='experience', heading=role,
                organization='Example Corp', start_date='2022', end_date='2023', evidence_ids=['E-1'])])
    return text, analysis


def imported_versions(setup):
    workflow, packs, workspace, evidence, saved, _ = setup
    old_text, old = source('Strategy Intern', 'Built legacy Python reporting APIs.')
    text, current = source('Data Analyst Intern', 'Built Python APIs for data analysis.')
    for body, analysis in [(old_text, old), (text, current)]:
        for item in evidence.import_resume_evidence(analysis, body, created_by='fit_analysis'):
            evidence.link_to_application(item.evidence_id, saved.application.application_id,
                expected_version=item.version)
    return text, current


def test_selection_scopes_resume_before_ranking_but_keeps_confirmed_career_facts(setup):
    text, _ = imported_versions(setup)
    workflow, _, _, evidence, saved, _ = setup
    job, _ = workflow._source(saved.application.application_id)
    all_evidence = evidence.list(status='confirmed')
    selected = EvidenceSelectionPolicy().select(evidence=all_evidence,
        links=evidence.list_for_application(saved.application.application_id), job=job,
        resume_text=text.replace('\n', '\n\n'))
    assert [e.role for e in selected if e.source_type == 'resume'] == ['Data Analyst Intern']
    assert any(e.source_type == 'user_attested' for e in selected)
    assert len([e for e in evidence.list(status='confirmed') if e.current.source_type == 'resume']) == 2
    assert all(e.source_type != 'resume' for e in EvidenceSelectionPolicy().select(
        evidence=all_evidence, links=[], job=job, resume_text='No known resume'))


def test_pack_snapshot_uses_its_analyzed_resume_and_has_one_experience_entry(setup):
    text, analysis = imported_versions(setup)
    workflow, packs, workspace, _, saved, _ = setup
    job, match = workflow._source(saved.application.application_id)
    app, _ = workspace.project_fit_analysis(saved.application.application_id,
        snapshot_id=saved.snapshot.snapshot_id, analysis_id='current-resume', resume=analysis,
        job=job, match=match, expected_version=saved.application.version, resume_text=text)
    pack = workflow.create(app.application_id, expected_version=app.version, idempotency_key='scoped')
    snapshot = packs.snapshot(pack.pack_id)
    assert [e.role for e in snapshot.items if e.source_type == 'resume'] == ['Data Analyst Intern']
    entries = resume_analysis_from_snapshot(snapshot).source_entries
    assert [e.heading for e in entries if e.organization == 'Example Corp'] == ['Data Analyst Intern']
    assert workflow.refresh_staleness(pack.pack_id).status.value != 'stale'


def test_parallel_evidence_freeze_uses_execution_resume_not_entire_vault(setup):
    text, _ = imported_versions(setup)
    workflow, _, workspace, evidence, saved, _ = setup
    job, _ = workflow._source(saved.application.application_id)
    worker = EvidenceFreezeWorker(workspace=workspace, evidence=evidence, pack_workflow=workflow)
    task = SimpleNamespace(application_id=saved.application.application_id,
        task_id='freeze-current', root_task_id='root-current',
        input_spec={'job_snapshot_id': saved.snapshot.snapshot_id})
    context = SimpleNamespace(input_artifacts={
        'resume': {'kind': 'resume_source', 'data': {'resume_text': text}},
        'job': {'kind': 'job_analysis', 'data': job.model_dump(mode='json')},
    })
    result = worker.execute(task, context)
    content = result.output_artifacts[0].content["data"]
    assert [e['role'] for e in content['items'] if e['source_type'] == 'resume'] == ['Data Analyst Intern']
