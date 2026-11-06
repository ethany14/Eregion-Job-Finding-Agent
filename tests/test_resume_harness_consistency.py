"""The tailored-resume writer contract, cleanup and validator must agree.

Each case here was a failure no model revision could fix: the harness itself
produced a draft its own validator rejected. All data is fictional.
"""
from types import SimpleNamespace

from eregion.domain.quality import (ensure_foundational_resume_sections, entry_section,
    validate_resume_structure)
from eregion.domain.resume_selection import ResumeSelection
from eregion.domain.schemas import (ResumeAnalysis, ResumeEntry, ResumeEvidence, ResumeSection,
    ResumeSourceEntry, SupportedClaim, TailoredResume)
from eregion.runtime.application_pack import policy
from eregion.runtime.application_pack.workflow import _header_only, resume_analysis_from_snapshot

EDU_TEXT = 'Example University | M.S., Data Science\nExpected May 2027'
JOB_TEXT = 'Built a forecasting service in Python that cut reporting time by 30 percent.'


def analysis():
    evidence = [
        ResumeEvidence(evidence_id='E-edu', source_section='Education', exact_text=EDU_TEXT, source_entry_id='SRC-edu'),
        ResumeEvidence(evidence_id='E-job', source_section='Experience', exact_text=JOB_TEXT, source_entry_id='SRC-job'),
        ResumeEvidence(evidence_id='E-fact', source_section='Confirmed Career Evidence',
                       exact_text='Used PyTorch for coursework models.', source_entry_id='SRC-fact'),
    ]
    sources = [
        ResumeSourceEntry(source_entry_id='SRC-edu', entry_type='education', heading='M.S., Data Science',
                          organization='Example University', end_date='Expected May 2027', evidence_ids=['E-edu']),
        ResumeSourceEntry(source_entry_id='SRC-job', entry_type='experience', heading='Analyst',
                          organization='Example Corp', start_date='2023', end_date='2024', evidence_ids=['E-job']),
        ResumeSourceEntry(source_entry_id='SRC-fact', entry_type='other', heading='Confirmed Career Evidence',
                          evidence_ids=['E-fact']),
    ]
    return ResumeAnalysis(summary='', skills=[], education=[], evidence=evidence, source_entries=sources)


def errors(resume, data):
    return [i for i in validate_resume_structure(resume, data.source_entries,
            known_evidence_ids={e.evidence_id for e in data.evidence}) if i.severity == 'error']


def test_header_only_education_is_restored_without_bullets_and_passes():
    data = analysis()
    job = ResumeEntry(entry_id='SRC-job', heading='Analyst', subheading='Example Corp', start_date='2023',
        end_date='2024', bullets=[SupportedClaim(text=JOB_TEXT, evidence_ids=['E-job'], source_entry_id='SRC-job')])
    draft = TailoredResume(sections=[ResumeSection(section_type='experience', title='Experience', entries=[job])])
    restored = ensure_foundational_resume_sections(draft, data.source_entries,
        {e.evidence_id: e.exact_text for e in data.evidence})
    education = next(s for s in restored.sections if s.section_type == 'education')
    assert [(e.entry_id, e.bullets) for e in education.entries] == [('SRC-edu', [])]
    assert errors(restored, data) == []


def test_education_without_a_source_entry_or_claims_still_fails():
    data = analysis()
    loose = ResumeEntry(entry_id='SRC-unknown', heading='Some School', bullets=[])
    resume = TailoredResume(sections=[ResumeSection(section_type='education', title='Education', entries=[loose])])
    assert any(i.code == 'empty_section' for i in errors(resume, data))


def test_loose_other_fact_is_never_filed_as_a_job():
    data = analysis()
    state = SimpleNamespace(resume_analysis=data)
    selection = ResumeSelection(summary=[], entries=[
        {'entry_id': 'SRC-job', 'selected_evidence_ids': ['E-job'], 'bullets': []},
        {'entry_id': 'SRC-fact', 'selected_evidence_ids': ['E-fact'], 'bullets': []}])
    resume = selection.expand(state)
    filed = {e.entry_id for s in resume.sections for e in s.entries}
    assert filed == {'SRC-job'}
    assert not any(i.code == 'invalid_section_membership' for i in errors(resume, data))


def test_placement_rule_matches_the_validator_for_roles():
    activity = ResumeSourceEntry(source_entry_id='SRC-a', entry_type='other', heading='Volunteer Tutor',
                                 organization='Example Library', start_date='2022', evidence_ids=[])
    fact = ResumeSourceEntry(source_entry_id='SRC-f', entry_type='other', heading='Confirmed Career Evidence')
    assert entry_section(activity) == 'experience' and entry_section(fact) is None


def version(text, employer, role, start, end, category='experience'):
    return SimpleNamespace(source_reference='resume-1', claim_text=text, employer_or_project=employer,
        role=role, start_date=start, end_date=end, category=SimpleNamespace(value=category),
        source_section='Experience')


def test_two_imports_with_swapped_employer_and_role_are_one_entry():
    old = version(JOB_TEXT, 'Example Corp', 'Analyst', '2023', '2024')
    new = version('Example Corp | Analyst\n2023 - 2024\n' + JOB_TEXT, 'Analyst', 'Example Corp', '2023', '2024')
    assert policy._same_source_entry(old, new)


def test_a_bullet_is_superseded_by_the_same_dated_entry_from_a_newer_import():
    bullet = version(JOB_TEXT, 'Forecast Tool', None, '2023', '2024', category='project')
    block = version('Forecast Tool | Personal Project\n2023 - 2024\n' + JOB_TEXT,
                    'Personal Project', 'Forecast Tool', '2023', '2024')
    assert policy._same_dated_entry(bullet, block)
    # Different dates, or a block without dates (a whole-section aggregate), never supersede.
    assert not policy._same_dated_entry(bullet, version(block.claim_text, 'Personal Project', 'Forecast Tool', '2022', '2024'))
    assert not policy._same_dated_entry(bullet, version(block.claim_text, None, None, None, None))


def snapshot_item(key, text, section='Resume', **fields):
    return SimpleNamespace(evidence_version_id=key, claim_text=text, category='experience', source_section=section,
        source_type='resume', employer_or_project=fields.get('employer'), role=fields.get('role'),
        start_date=fields.get('start'), end_date=fields.get('end'))


def test_contact_header_is_not_resume_evidence():
    header = snapshot_item('h', 'Alex Example\nSpringfield, IL | +1 555-010-0199 | alex@example.com')
    summary = snapshot_item('s', 'Analyst who builds forecasting services for finance teams.', section='Summary')
    assert _header_only(header, [])
    assert _header_only(header, ['Alex Example', 'Springfield, IL | +1 555-010-0199 | alex@example.com'])
    assert not _header_only(summary, [])
    job = snapshot_item('j', JOB_TEXT, section='Experience', employer='Example Corp', role='Analyst', start='2023', end='2024')
    result = resume_analysis_from_snapshot(SimpleNamespace(items=[header, job], resume_entries={}, resume_header_lines=[]))
    assert {e.evidence_id for e in result.evidence} == {'j'}
