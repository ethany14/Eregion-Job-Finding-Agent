import pytest
from test_application_pack import setup
from test_resume_generation_live import EDUCATION as LIVE_EDUCATION, prepare_resume_case

from eregion.domain.schemas import ResumeAnalysis, ResumeEvidence, ResumeSourceEntry, JobAnalysis
from eregion.domain.resume_sources import complete_source_evidence
from eregion.domain.jev_fit.projection import resume_projection
from eregion.domain.domain import normalize_resume_analysis
from eregion.runtime.evidence.repository import CareerEvidenceRepository
from eregion.runtime.application_pack.policy import EvidenceSelectionPolicy
from test_auto_apply import database


EDUCATION = ('Northlake University | M.Eng., Systems EngineeringExpected May 2027\n'
             'University of Westhaven | B.A., Computer ScienceDec 2025')


def legacy():
    return ResumeAnalysis(summary='', skills=[], education=[],
        source_entries=[ResumeSourceEntry(source_entry_id='s', entry_type='education', evidence_ids=['e'])],
        evidence=[ResumeEvidence(evidence_id='e', source_entry_id='s', source_section='Education', exact_text=EDUCATION)])


def test_single_graduation_dates_preserve_school_ownership_and_expected_status():
    parsed = resume_projection('Education\n' + EDUCATION)
    entries = [e for e in parsed.source_entries if e.entry_type == 'education']
    assert [(e.organization, e.heading, e.start_date, e.end_date) for e in entries] == [
        ('Northlake University', 'M.Eng., Systems Engineering', None, 'Expected May 2027'),
        ('University of Westhaven', 'B.A., Computer Science', None, 'Dec 2025')]
    assert all(e.exact_text in EDUCATION for e in parsed.evidence)


def test_legacy_section_is_repaired_without_losing_or_inventing_evidence(database):
    original = 'Education\n' + EDUCATION
    repaired = normalize_resume_analysis(complete_source_evidence(legacy(), original))
    assert len(repaired.source_entries) == 2
    assert all(e.exact_text in original for e in repaired.evidence)
    assert normalize_resume_analysis(complete_source_evidence(repaired, original)) == repaired
    repository = CareerEvidenceRepository(database.session_factory)
    old = repository.import_resume_evidence(legacy(), original, created_by='fit_analysis')
    new = repository.import_resume_evidence(repaired, original, created_by='pack_resume_import')
    selected = EvidenceSelectionPolicy().select(evidence=[*old, *new], links=[], resume_text=original,
        job=JobAnalysis(title='Engineer', summary='', requirements=[], responsibilities=[]))
    assert len(selected) == 2
    assert {e.end_date for e in selected} == {'Expected May 2027', 'Dec 2025'}
    assert all(e.claim_text != EDUCATION for e in selected)


def test_ambiguous_education_is_not_split_or_assigned_an_owner():
    parsed = legacy()
    parsed.evidence[0].exact_text = 'Studied engineering, May 2027\nUnknown program'
    recovered = complete_source_evidence(parsed, parsed.evidence[0].exact_text)
    assert recovered.source_entries == parsed.source_entries


def test_parenthesized_preferred_name_is_preserved_verbatim():
    from eregion.domain.resume_headers import header_lines
    text = 'Alex (Jamie) Doe\nLakeview, NY | alex@example.test\nEducation\n' + EDUCATION
    assert header_lines(text) == ['Alex (Jamie) Doe', 'Lakeview, NY | alex@example.test']


def test_metadata_only_education_is_valid_but_fabricated_metadata_is_rejected():
    from eregion.domain.schemas import TailoredResume, ResumeSection, ResumeEntry
    from eregion.domain.quality import validate_resume_structure
    analysis = normalize_resume_analysis(complete_source_evidence(legacy(), 'Education\n' + EDUCATION))
    resume = TailoredResume(sections=[ResumeSection(section_type='education', title='Education', entries=[
        ResumeEntry(entry_id=s.source_entry_id, heading=s.heading, subheading=s.organization,
                    start_date=s.start_date, end_date=s.end_date, bullets=[])
        for s in analysis.source_entries])])
    assert not validate_resume_structure(resume, analysis.source_entries)
    resume.sections[0].entries[0].end_date = 'May 2024'
    assert 'source_metadata_mismatch' in {i.code for i in validate_resume_structure(resume, analysis.source_entries)}
    resume.sections[0].entries = []
    assert 'empty_section' in {i.code for i in validate_resume_structure(resume, analysis.source_entries)}


# Exercise source repair through the same persisted evidence snapshot as the writer.
@pytest.mark.parametrize('case', LIVE_EDUCATION)
def test_generation_snapshot_preserves_both_schools_and_source_header(setup, case):
    pack, source = prepare_resume_case(setup, case)
    _, packs, _, _, _, _ = setup
    assert packs.snapshot(pack.pack_id).resume_header_lines == [
        'Alex (Jamie) Doe', 'alex@example.test']
    schools = [s for s in source.source_entries if s.entry_type == 'education']
    assert {s.organization for s in schools} == {'Northlake University', 'University of Westhaven'}
    if case != 'date_ranges':
        assert {s.end_date for s in schools} == {'Expected May 2027', 'Dec 2025'}
