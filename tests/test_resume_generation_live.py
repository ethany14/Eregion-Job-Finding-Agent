"""Opt-in production resume generation with synthetic data and isolated SQLite.

Run: JOB_AGENT_LIVE_RESUME_GENERATION=1 .venv/bin/python -m pytest \
    tests/test_resume_generation_live.py -q -s
Credentials come from the normal model configuration. No user database is opened.
"""
import io
import json
import os
import time

import pytest
from pypdf import PdfReader
from test_application_pack import setup
from eregion.domain.jev_fit.projection import resume_projection
from eregion.domain.pdf_rendering import render_tailored_resume_pdf
from eregion.domain.schemas import ResumeEvidence, ResumeSourceEntry, TailoredResume
from eregion.runtime.application_pack.types import ItemStatus
from eregion.runtime.application_pack.verifier import ArtifactVerifier
from eregion.runtime.application_pack.workflow import SharedPackModel, resume_analysis_from_snapshot


EDUCATION = {
    'joined_dates': ('Northlake University | M.Eng., Systems EngineeringExpected May 2027\n'
                     'University of Westhaven | B.A., Computer ScienceDec 2025'),
    'legacy_grouped': ('Northlake University | M.Eng., Systems EngineeringExpected May 2027\n'
                       'University of Westhaven | B.A., Computer ScienceDec 2025'),
    'date_ranges': ('Northlake University | M.Eng., Systems Engineering | Aug 2026 - May 2027\n'
                    'University of Westhaven | B.A., Computer Science | Sep 2021 - Dec 2025'),
}


def prepare_resume_case(setup, case):
    workflow, packs, workspace, evidence, saved, _ = setup
    # Remove the fixture's separate attested fact so only this resume is used.
    for item in evidence.list(status='confirmed'):
        evidence.archive(item.evidence_id, item.version)
    text = ('Alex (Jamie) Doe\nalex@example.test\nEDUCATION\n' + EDUCATION[case] +
        '\nEXPERIENCE\nSoftware Engineer | Example Labs | Jun 2024 - Aug 2025\n'
        'Built Python APIs for an internal reporting application.\n'
        'Wrote pytest tests for API request validation.\n'
        'PROJECTS\nInventory Service | Jan 2024 - May 2024\n'
        'Created a FastAPI service with PostgreSQL for tracking inventory.\n'
        'SKILLS\nPython\nSQL\nFastAPI\nPostgreSQL\npytest')
    analysis = resume_projection(text)
    if case == 'legacy_grouped':
        ids = {s.source_entry_id for s in analysis.source_entries if s.entry_type == 'education'}
        analysis.source_entries = [s for s in analysis.source_entries if s.source_entry_id not in ids]
        analysis.evidence = [e for e in analysis.evidence if e.source_entry_id not in ids]
        analysis.source_entries.append(ResumeSourceEntry(source_entry_id='old-education',
            entry_type='education', evidence_ids=['old-education-evidence']))
        analysis.evidence.append(ResumeEvidence(evidence_id='old-education-evidence',
            source_entry_id='old-education', source_section='Education', exact_text=EDUCATION[case]))
        # Reproduce the already-imported legacy Vault record as well.
        evidence.import_resume_evidence(analysis, text, created_by='fit_analysis')
    job, match = workflow._source(saved.application.application_id)
    app, _ = workspace.project_fit_analysis(saved.application.application_id,
        snapshot_id=saved.snapshot.snapshot_id, analysis_id='resume-'+case, resume=analysis,
        job=job, match=match, expected_version=saved.application.version, resume_text=text)
    pack = workflow.create(app.application_id, expected_version=app.version, idempotency_key='pack-'+case)
    source = resume_analysis_from_snapshot(packs.snapshot(pack.pack_id))
    schools = [s for s in source.source_entries if s.entry_type == 'education']
    assert len(schools) == 2
    assert all(s.organization and s.heading and s.end_date for s in schools)
    return pack, source


@pytest.mark.skipif(os.getenv('JOB_AGENT_LIVE_RESUME_GENERATION') != '1',
                   reason='Explicit opt-in required for real model calls')
@pytest.mark.parametrize('case', EDUCATION)
def test_live_resume_generation_verification_reuse_and_pdf(setup, case):
    workflow, packs, _, _, _, _ = setup
    pack, source = prepare_resume_case(setup, case)
    model = SharedPackModel()
    workflow._model = model
    workflow._verifier = ArtifactVerifier(model.verify_claims)
    started = time.monotonic()
    workflow.generate_many(pack.pack_id, ['tailored_resume'], idempotency_key='generate')
    item = packs.items(pack.pack_id)[0]
    verdict = item.verification.model_dump(mode='json') if item.verification else None
    print(json.dumps({'case': case, 'seconds': round(time.monotonic()-started, 2),
                      'status': item.status.value, 'revisions': item.revision_count, 'verification': verdict}))
    assert item.status == ItemStatus.AWAITING_REVIEW, verdict
    assert item.verification.passed
    content = packs.artifact(pack.pack_id, item.pack_item_id)
    resume = TailoredResume.model_validate(content)
    assert resume.header.name == 'Alex (Jamie) Doe'
    education = next(s for s in resume.sections if s.section_type == 'education')
    assert len(education.entries) == 2
    expected = {(s.heading, s.organization, s.start_date, s.end_date)
                for s in source.source_entries if s.entry_type == 'education'}
    assert {(s.heading, s.subheading, s.start_date, s.end_date) for s in education.entries} == expected
    assert {'experience', 'projects', 'skills'} <= {s.section_type for s in resume.sections}
    pdf = render_tailored_resume_pdf(content)
    pages = PdfReader(io.BytesIO(pdf)).pages
    extracted = '\n'.join(page.extract_text() for page in pages)
    assert 'Alex (Jamie) Doe' in extracted
    assert 'Northlake University' in extracted and 'University of Westhaven' in extracted
    if case != 'date_ranges':
        assert 'Expected May 2027' in extracted
    versions = packs.versions(pack.pack_id, item.pack_item_id)
    assert 1 <= len(versions) <= 2
    assert workflow.generate_many(pack.pack_id, ['tailored_resume'], idempotency_key='repeat') == []
    assert packs.versions(pack.pack_id, item.pack_item_id) == versions
    assert packs.item(pack.pack_id, item.pack_item_id).status == ItemStatus.AWAITING_REVIEW
