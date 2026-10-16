"""Regressions for defects found while verifying the 2026-10-10 E2E repairs."""
from eregion.domain.jev_fit.projection import resume_projection


def entries(text, kind):
    analysis = resume_projection(text)
    quotes = {item.evidence_id: item.exact_text for item in analysis.evidence}
    return [(e.heading, e.organization, e.start_date, e.end_date, ' '.join(quotes[i] for i in e.evidence_ids))
            for e in analysis.source_entries if e.entry_type == kind]


def education(body):
    return entries(f'Jane Doe\nEducation\n{body}\nSkills\nPython\n', 'education')


def experience(first, second):
    return entries(f'Jane Doe\nExperience\n{first}\n- Built APIs\n{second}\n- Ran on-call\n'
                   'Education\nB.S. Computer Science, State University, 2017\nSkills\nGo\n', 'experience')


def test_wrapped_in_progress_qualifier_stays_with_its_own_degree():
    rows = education('M.S. in CS, Carnegie Mellon University (Aug 2025 - May 2027\n(expected)) - In progress, GPA 3.8\n'
                     'B.E. in CSE, Anna University (Aug 2019 - May 2023)')
    assert [row[:4] for row in rows] == [('M.S. in CS', 'Carnegie Mellon University', 'Aug 2025', 'May 2027 (expected)'),
                                         ('B.E. in CSE', 'Anna University', 'Aug 2019', 'May 2023')]
    assert 'In progress' in rows[0][4]
    assert 'In progress' not in rows[1][4] and 'expected' not in rows[1][4]


def test_school_names_that_start_with_their_keyword():
    assert education('B.S. in Computer Science, University of Illinois Urbana-Champaign (Aug 2013 - May 2017)')[0][:4] == (
        'B.S. in Computer Science', 'University of Illinois Urbana-Champaign', 'Aug 2013', 'May 2017')
    assert education('B.A. in Economics, University of California, Berkeley (2015 - 2019)')[0][:2] == (
        'B.A. in Economics', 'University of California, Berkeley')
    assert education('University of Michigan\nB.S. in Biology, 2015 - 2019')[0][:2] == ('B.S. in Biology', 'University of Michigan')


def test_additional_experience_header_layouts():
    expected = [('Senior Engineer', 'Acme Corp'), ('Software Engineer', 'Beta Inc')]
    for first, second in [('Acme Corp, Chicago\nSenior Engineer, 2021 - Present', 'Beta Inc\nSoftware Engineer, 2017 - 2021'),
                          ('Senior Engineer\nAcme Corp, 2021 - Present', 'Software Engineer\nBeta Inc, 2017 - 2021'),
                          ('Acme Corp\tMar 2021 – Present\nSenior Engineer', 'Beta Inc\tJul 2017 – Feb 2021\nSoftware Engineer')]:
        assert [row[:2] for row in experience(first, second)] == expected
    assert [row[:2] for row in experience('Senior Engineer at Acme (2021 – Present)', 'Engineer at Beta (2017 – 2021)')] == [
        ('Senior Engineer', 'Acme'), ('Engineer', 'Beta')]


def test_ambiguous_two_line_header_stays_unowned():
    rows = experience('Northwind\nPlatform, 2021 - Present', 'Contoso\nCore, 2017 - 2021')
    assert len(rows) == 1 and rows[0][:4] == (None, None, None, None)


# ---- C4 / D4: requirement splitting ---------------------------------------------------------
from eregion.domain.jev_fit.core import job_units


def texts(jd):
    return [u['text'] for u in job_units(jd)]


def test_abbreviations_do_not_split_requirements():
    units = texts('Requirements: B.S. Computer Science or equivalent. 3+ years of Python and SQL. '
                  'Preferred: Experience with e.g. Kafka. Familiarity with AWS, i.e. EKS and RDS.')
    assert 'B.S. Computer Science or equivalent.' in units
    assert '3+ years of Python and SQL.' in units
    assert 'Preferred: Experience with e.g. Kafka.' in units
    assert 'Familiarity with AWS, i.e. EKS and RDS.' in units


def test_lists_and_in_phrase_dashes():
    assert texts('Qualifications:\n1. Python - expert level\n2. SQL') == ['Qualifications:', '1. Python - expert level', '2. SQL']
    assert texts('Salary range: $170,000 - $210,000') == ['Salary range:', '$170,000 - $210,000']




# ---- C4: main-model resolution of requirements Jev leaves unknown -----------------------------
import pytest
from eregion.domain.jev_fit.resolve import UnknownDecision, UnknownResolution, resolve_unknowns

EVIDENCE = 'Built Python tools for analytics.\nRan Kubernetes clusters in production for 3 years.\n'


def raw_with_unknowns(*texts_):
    matches = [{'requirement_id': f'j{i}', 'text': t, 'level': 'required', 'weight': 2, 'status': 'unknown',
                'reason': '', 'citations': [], 'initial_decisions': {}, 'focused_decisions': {}} for i, t in enumerate(texts_)]
    extraction = [{'id': f'j{i}', 'classification': 'required'} for i in range(len(texts_))]
    return {'matches': matches, 'extraction': extraction, 'provider_errors': [], 'score': None, 'score_range': [0, 100]}


def decide(*decisions):
    return lambda model, schema, system, human: UnknownResolution(decisions=[UnknownDecision(**d) for d in decisions])


def test_resolver_requires_a_verbatim_quote_for_support():
    raw = raw_with_unknowns('Kubernetes in production', 'Go')
    result = resolve_unknowns(raw, EVIDENCE, model=None, invoke=decide(
        dict(requirement_id='j0', is_candidate_requirement=True, level='required', personal_condition=False, status='matched',
             evidence_quote='Ran Kubernetes clusters in production for 3 years.', reason='explicit'),
        dict(requirement_id='j1', is_candidate_requirement=True, level='required', personal_condition=False, status='missing', reason='absent')))
    by_id = {m['requirement_id']: m for m in result['matches']}
    assert by_id['j0']['status'] == 'matched' and by_id['j0']['resolved_by'] == 'main_model'
    assert by_id['j0']['citations'][0]['exact_text'] == 'Ran Kubernetes clusters in production for 3 years.'
    assert by_id['j1']['status'] == 'missing'
    assert result['score'] == 50.0 and result['status'] == 'completed' and result['main_model_calls'] == 1


def test_resolver_rejects_fabricated_quotes_and_drops_non_requirements():
    raw = raw_with_unknowns('Go', 'We offer great benefits', 'Authorized to work in the US')
    result = resolve_unknowns(raw, EVIDENCE, model=None, invoke=decide(
        dict(requirement_id='j0', is_candidate_requirement=True, level='required', personal_condition=False, status='matched',
             evidence_quote='Wrote Go services at Google.', reason='invented'),
        dict(requirement_id='j1', is_candidate_requirement=False, level='required', personal_condition=False, status='missing', reason='benefit'),
        dict(requirement_id='j2', is_candidate_requirement=True, level='required', personal_condition=True, status='missing', reason='condition')))
    by_id = {m['requirement_id']: m for m in result['matches']}
    assert by_id['j0']['status'] == 'unknown'  # unverifiable support is never adopted
    assert by_id['j1']['status']=='unknown'  # Resolver concerns cannot erase upstream retained requirements.
    assert by_id['j2']['status'] == 'needs_confirmation'
    # The unverified requirement keeps Jev's uncertain probability (.5 -> credit .625); no invented support.
    assert result['score'] == 62.5


def test_resolver_failure_keeps_the_honest_interval():
    raw = raw_with_unknowns('Go')
    def boom(*_):
        raise TimeoutError('provider')
    result = resolve_unknowns(raw, EVIDENCE, model=None, invoke=boom)
    assert result['matches'][0]['status'] == 'unknown' and result['resolver']['status'] == 'failed'


def test_fit_service_sends_only_jev_unknowns_to_the_main_model(tmp_path, monkeypatch):
    from dataclasses import replace
    import httpx
    from eregion.api.services.fit_analysis_service import FitAnalysisService
    from eregion.domain.jev_fit.settings import Settings
    from test_jev_fit_production import JevEndpoint, NoMainModel
    endpoint = JevEndpoint(); endpoint.unknown = True
    sent = []
    def fake_invoke(model, schema, system, human):
        sent.append(human)
        ids = [line.split()[1] for line in human.splitlines() if line.startswith('- j')]
        return UnknownResolution(decisions=[UnknownDecision(requirement_id=i, is_candidate_requirement=True, level='required',
            personal_condition=False, status='matched', evidence_quote='Built Python tools.', reason='quoted') for i in ids])
    monkeypatch.setattr('eregion.domain.model.invoke_structured', fake_invoke)
    monkeypatch.setattr('eregion.domain.model.shared_model', lambda **_: object())
    config = replace(Settings(key='test-only', cache_path=tmp_path / 'd.sqlite3'), resolve_unknowns=True)
    service = FitAnalysisService(NoMainModel(), jev_enabled=True, jev_config=config, jev_transport=httpx.MockTransport(endpoint))
    result = service._analyze_sync(resume_text='Candidate Example\nEXPERIENCE\nEngineer | Example Lab | 2023-01 | 2026-01\n'
                                   'Built Python tools.\nSKILLS\nPython, SQL\n', job_description='Engineer\nRequired: Python\n')
    requested = [line for line in sent[0].splitlines() if line.startswith('- j')]
    assert len(sent) == 1 and len(requested) == 1 and requested[0].endswith(': Python')
    assert result.main_model_calls == 1 and result.skill_match.overall_score == 100.0
    assert any(r['purpose'] == 'jev_fit_resolver' for r in result.decision_routing)


# ---- D5: an application whose owner conversation is gone can be reopened -----------------------
class _Repo:
    def __init__(self, tasks):self.tasks=tasks
    def get(self, task_id):
        if task_id not in self.tasks:raise LookupError('Conversation not found.')
        return self.tasks[task_id]
    def metadata(self, task_id):
        if task_id not in self.tasks:raise LookupError('Conversation not found.')
        return {'trashed_at': self.tasks[task_id].get('trashed_at')}
    def list(self):return list(self.tasks.values())


class _Runs:
    def __init__(self):self.saved=[]
    def save(self, run, **changes):
        self.saved.append(changes);return {**run, **changes}


def _harness(tasks):
    from types import SimpleNamespace
    from eregion.runtime.career.harness import CareerHarness
    harness=CareerHarness.__new__(CareerHarness)
    harness.repository=_Repo(tasks)
    harness.applications=SimpleNamespace(repository=_Runs())
    return harness


def test_live_owner_still_blocks_other_conversations():
    harness=_harness({'a':{'task_id':'a','status':'needs_input','application_runs':['r1']},'b':{'task_id':'b','status':'running'}})
    with pytest.raises(ValueError, match='owned by another conversation'):
        harness.require_application_owner({'task_id':'b'},{'run_id':'r1','origin_task_id':'a','status':'needs_input'},adopt=True)


def test_deleted_or_cancelled_owner_can_be_adopted_when_reopened():
    for tasks in ({'b':{'task_id':'b','status':'running'}},
                  {'a':{'task_id':'a','status':'cancelled'},'b':{'task_id':'b','status':'running'}},
                  {'a':{'task_id':'a','status':'completed','trashed_at':'now'},'b':{'task_id':'b','status':'running'}}):
        harness=_harness(tasks)
        run=harness.require_application_owner({'task_id':'b'},{'run_id':'r1','origin_task_id':'a','status':'cancelled'},adopt=True)
        assert run['origin_task_id']=='b'
        with pytest.raises(ValueError, match='Reopen the job'):
            harness.require_application_owner({'task_id':'b'},{'run_id':'r1','origin_task_id':'a','status':'cancelled'})


def test_abandoned_run_is_not_adopted_while_still_executing():
    harness=_harness({'b':{'task_id':'b','status':'running'}})
    with pytest.raises(ValueError, match='still finishing'):
        harness.require_application_owner({'task_id':'b'},{'run_id':'r1','origin_task_id':'a','status':'running'},adopt=True)


# ---- D6: runtime feedback never changes an archived source reference ---------------------------
def test_progress_feedback_does_not_change_archive_ref():
    from eregion.runtime.request_context import ContextArchive
    archive=ContextArchive()
    big={'application_id':'job-1','status':'needs_evidence','report':'x'*8000}
    first=archive.tool_result(dict(big))
    second=archive.tool_result({**big,'progress_feedback':'No new evidence from this action.'})
    assert first['context_ref']==second['context_ref']
    assert second['progress_feedback']=='No new evidence from this action.'


# ---- D7: browser contexts pin a locale instead of inheriting the OS ----------------------------
def test_browser_context_locale_is_pinned(monkeypatch):
    from eregion.runtime.auto_apply.browser import context_options
    monkeypatch.delenv('JOB_AGENT_BROWSER_LOCALE', raising=False)
    assert context_options()['locale']=='en-US'
    assert context_options()['extra_http_headers']['Accept-Language'].startswith('en-US')
    monkeypatch.setenv('JOB_AGENT_BROWSER_LOCALE','en-GB')
    assert context_options()['locale']=='en-GB'


# ---- C3 / D8: upload obligations follow the actual controls ------------------------------------
def test_required_generic_upload_field_counts_as_the_resume_slot():
    from eregion.runtime.auto_apply.autofill import pending_documents
    refresh={'document_refresh':{'resume':True}}
    assert pending_documents(refresh,[{'key':'f','kind':'file','label':'Attachments','required':True}])==['resume']
    assert pending_documents(refresh,[{'key':'f','kind':'file','label':'Additional documents','required':False}])==[]
    assert pending_documents(refresh,[{'key':'n','kind':'text','label':'First name','required':True}])==[]


def test_resolver_decisions_persist_so_scores_do_not_drift(tmp_path):
    from eregion.domain.jev_fit.resolve import DecisionStore
    store = DecisionStore(tmp_path / 'd.sqlite3')
    calls = []
    def once(model, schema, system, human):
        calls.append(human)
        return UnknownResolution(decisions=[UnknownDecision(requirement_id='j0', is_candidate_requirement=True, level='required',
            personal_condition=False, status='matched', evidence_quote='Ran Kubernetes clusters in production for 3 years.', reason='q')])
    first = resolve_unknowns(raw_with_unknowns('Kubernetes in production'), EVIDENCE, model=None, invoke=once, store=store)
    second = resolve_unknowns(raw_with_unknowns('Kubernetes in production'), EVIDENCE, model=None, invoke=once, store=store)
    assert len(calls) == 1 and first['score'] == second['score'] == 100.0
    assert second['main_model_calls'] == 0 and second['resolver']['cached'] == 1
    # Different evidence is a different decision.
    resolve_unknowns(raw_with_unknowns('Kubernetes in production'), EVIDENCE + 'More.\n', model=None, invoke=once, store=store)
    assert len(calls) == 2


def test_quote_verification_tolerates_only_presentation_differences():
    from eregion.domain.jev_fit.resolve import locate
    source = 'Built 14 dashboards.\n\x7f Wrote SQL pipelines; cut time\nfrom 3 days to 4 hours.\nLed the team’s data–quality work.'
    span = locate('Built 14 dashboards.\n\x07 Wrote SQL pipelines; cut time from 3 days to 4 hours.', source)
    assert span and source[span[0]:span[1]].startswith('Built 14') and source[span[0]:span[1]].endswith('4 hours.')
    assert locate("Led the team's data-quality work.", source)
    assert locate('Built 15 dashboards.', source) is None          # changed facts never verify
    assert locate('Wrote SQL pipelines to cut time', source) is None  # paraphrase never verifies


# ---- 0.2: a wait on an already-finished application must never sleep forever -----------------
def test_finished_application_is_never_waited_on():
    from types import SimpleNamespace
    from eregion.runtime.career.harness import CareerHarness
    harness = CareerHarness.__new__(CareerHarness)
    child = {'run_id': 'r1', 'status': 'submitted', 'chat_revision': 0, 'messages': []}
    harness.applications = SimpleNamespace(repository=SimpleNamespace(get=lambda run_id: child))
    assert harness.capability_finished({'kind': 'application', 'id': 'r1'})
    # A conversation already parked with an identical token is released by the monitor.
    token = harness.wait_token({'id': 'r1'})
    assert harness.wake({'wait': {'kind': 'application', 'id': 'r1', 'token': token}})
    child['status'] = 'running'
    assert not harness.capability_finished({'kind': 'application', 'id': 'r1'})
    assert harness.capability_stopped({'kind': 'application', 'id': 'r1'}) is None
    # Veeva pilot: a run paused by a model timeout is stopped; waiting on it never wakes.
    child.update(status='needs_input', message='The model response timed out.')
    stopped = harness.capability_stopped({'kind': 'application', 'id': 'r1'})
    assert stopped['changed'] and 'reply_application' in stopped['next']


# ---- Step 5: Jev expected score (one consistent number instead of a range) --------------------
from eregion.domain.jev_fit.core import expected_score, score as interval_score


def match(full, related=.5, requirement=.99, preferred=0., condition=.01, status='unknown', level='required'):
    return {'requirement_id': f'r{full}{related}', 'level': level, 'weight': 2, 'status': status,
            'probabilities': {'requirement': requirement, 'preferred': preferred, 'condition': condition, 'full': full, 'related': related}}


def test_expected_score_is_a_single_deterministic_number():
    ms = [match(.97, .99, status='matched'), match(.6), match(.03, .2, status='missing')]
    first, second = expected_score(ms), expected_score([dict(m) for m in ms])
    assert first['score'] is not None and first['score'] == second['score']
    assert first['score_range'] == [first['score'], first['score']]
    assert interval_score(ms)['score'] is None  # the old categorical path still yields only a range here


def test_expected_score_is_continuous_across_the_old_thresholds():
    below, above = expected_score([match(.84), match(.9, .9)]), expected_score([match(.86), match(.9, .9)])
    assert abs(below['score'] - above['score']) < 2  # .84 -> .86 used to flip unknown -> matched


def test_expected_score_is_monotonic_and_excludes_personal_conditions():
    weak, strong = expected_score([match(.2, .3)]), expected_score([match(.8, .9)])
    assert strong['score'] > weak['score']
    with_condition = expected_score([match(.9, .9), match(.0, .0, condition=.99)])
    without = expected_score([match(.9, .9)])
    assert abs(with_condition['score'] - without['score']) < 1


def test_flattened_lists_are_restored_without_splitting_proper_nouns():
    flat = ('Responsibilities Own the end-to-end product design process from discovery through delivery Ship meaningful, '
            'high-quality experiences that balance business needs with usability Collaborate with engineering, product, and '
            'other stakeholders to shape the roadmap and goals of the team using Figma Prototyping tools and Python Notebooks')
    units = texts(flat)
    assert any(u.startswith('Ship meaningful') for u in units)
    assert any(u.startswith('Collaborate with engineering') for u in units)
    assert not any(u.startswith(('Figma', 'Python', 'Prototyping')) for u in units)


# Requirement classification is tested through model responses in test_jev_model_owned_classification.






def test_model_duration_interpretation_is_not_replaced_by_a_numeric_regex():
    from eregion.domain.domain import normalize_job_analysis
    from eregion.domain.schemas import JobAnalysis, JobRequirement
    phrase = 'Half a decade designing shipped products'
    jd = 'Senior Designer\nRequirements\n- ' + phrase + '\n'
    item = JobRequirement(requirement_id='r1', requirement_group_id='r1', canonical_name='portfolio', display_name='Portfolio',
        original_text=phrase, source_text=phrase,
        atomic_text=phrase, category='experience', verification_mode='years_experience',
        level='required', minimum_years=5)
    out = normalize_job_analysis(JobAnalysis(title=None, summary='', requirements=[item], responsibilities=[]), jd)
    assert out.requirements[0].minimum_years == 5


def test_level_free_title_needs_opening_level_evidence_for_junior_request():
    # B6 live: "Associate Data Analyst" with a preferred one-year minimum was
    # recommended as a junior match because only the title was checked.
    from fake_search_qualification import recommendation_warning
    policy={'levels':['junior'],'strict':False}
    warning=recommendation_warning(policy,'Associate Data Analyst',
        'A minimum of 1 year data management experience is preferred.')
    assert warning['actual'] is None and 'Level unverified' in warning['message']
    assert recommendation_warning(policy,'Data Analyst','0-2 years of marketing or analytics experience') is None
    assert recommendation_warning(policy,'Data Analyst','This role is an entry-level position.') is None
    assert recommendation_warning(policy,'Data Analyst','Minimum 5 years of experience.')['actual']=='senior'
    assert recommendation_warning(policy,'Junior Data Analyst','') is None
    with pytest.raises(ValueError,match='unverified level'):
        recommendation_warning({'levels':['junior'],'strict':True},'Associate Data Analyst','Preferred 1 year.')
    # Senior requests keep the title-only contract.
    assert recommendation_warning({'levels':['senior'],'strict':False},'Backend Engineer','') is not None


def test_board_search_needs_role_terms_not_one_shared_word():
    # B6 live r3: "junior data analyst" on Datadog's board returned
    # "Accounts Payable Analyst" and "AI Research Scientist - Data".
    import json as _json
    from eregion.runtime.career.discovery import JobDiscovery
    discovery=JobDiscovery()
    jobs=[{'absolute_url':f'https://boards.greenhouse.io/acme/jobs/{i}','title':title,'location':{'name':'New York, NY'}}
          for i,title in enumerate(['Accounts Payable Analyst','AI Research Scientist - Data','Data Analyst','Junior Data Analyst, Growth'])]
    discovery.read=lambda url,**_:(_json.dumps({'jobs':jobs}),url)
    result=discovery.search('junior data analyst','New York, NY',boards=[{'platform':'greenhouse','company':'acme'}])
    assert [j['title'] for j in result['jobs']]==['Data Analyst','Junior Data Analyst, Growth']




def test_bare_list_lines_are_items_but_prose_headings_still_split():
    from eregion.domain.jev_fit.core import job_units
    units = lambda body: [(u['text'], u.get('section')) for u in job_units(body)]
    assert units('Requirements:\nPython\nSQL') == [('Requirements:', 'Requirements'), ('Python', 'Requirements'), ('SQL', 'Requirements')]
    assert units('We build tools.\nWhat You Will Be Doing\n- Ship APIs')[1] == ('What You Will Be Doing', 'What You Will Be Doing')
    assert units('- Ship APIs\nAbout The Team\nWe are small.')[1] == ('About The Team', 'About The Team')
    # A line ending in a connector is wrapped prose, not a heading or a list item.
    assert units('Experience with Python and\nSQL databases.') == [('Experience with Python and\nSQL databases.', None)]


def test_obsolete_local_catalogue_flag_cannot_restore_rule_based_extraction(monkeypatch):
    from eregion.api.services.fit_analysis_service import FitAnalysisService
    service = FitAnalysisService(handler=type('H', (), {'analysis_cache_namespace': lambda self: 'ns'})(), jev_enabled=False)
    monkeypatch.setenv('JOB_AGENT_MAIN_CATALOG', 'local')
    local = service.cache_signature()
    monkeypatch.setenv('JOB_AGENT_MAIN_CATALOG', 'extract')
    assert service.cache_signature() == local
    monkeypatch.delenv('JOB_AGENT_MAIN_CATALOG')
    assert service.cache_signature() == local  # the obsolete flag no longer changes extraction


def test_resume_analysis_is_reused_across_model_extracted_jobs(monkeypatch):
    from eregion.api.services.fit_analysis_service import FitAnalysisService
    from eregion.domain.workflow.handlers import JobAgentStepHandler
    from eregion.domain.schemas import ResumeAnalysis, ResumeEvidence, SkillAssessment, SkillEvidence, JobAnalysis, JobRequirement
    monkeypatch.setenv('TYPESAFE_ENABLED', 'false')
    monkeypatch.setenv('JOB_AGENT_MAIN_CATALOG', 'local')
    calls = []
    def analyze(schema, system, human, *args):
        calls.append(schema)
        if schema is ResumeAnalysis:
            return ResumeAnalysis(summary='Dev', skills=['Python'], education=[],
                evidence=[ResumeEvidence(evidence_id='e', source_section='Skills', exact_text='Python')])
        if schema is JobAnalysis:
            return JobAnalysis(title=None, summary='', responsibilities=[], requirements=[JobRequirement(
                requirement_id='fixture', canonical_name='Python', display_name='Python',
                original_text='Python', source_text='Python', atomic_text='Python',
                category='skill', verification_mode='resume_evidence', level='required')])
        job = JobAnalysis.model_validate_json(human.split('\n\nJob analysis:\n')[1])
        return SkillAssessment(matches=[SkillEvidence(requirement_id=r.requirement_id, job_skill=r.canonical_name,
            requirement_level=r.level, match_status='matched', resume_evidence=['Python'], confidence=1)
            for r in job.requirements], explanation='Grounded', recommendations=[])
    service = FitAnalysisService(JobAgentStepHandler(analyzer=analyze))
    for body in ('Requirements:\nPython\nSQL', 'Python experience preferred.'):
        service._analyze_sync(resume_text='Skills\nPython', job_description=body)
    assert calls.count(ResumeAnalysis) == 1 and calls.count(JobAnalysis) == 2


@pytest.mark.parametrize('text,rejected', [
    ('Many boards errored; this is a coverage gap, not proof no vacancies exist.', False),
    ("Blocked sources don't mean no jobs exist.", False),
    ('No jobs exist for this search in New York.', True),
    ('I checked Indeed. No vacancies are available.', True),
])
def test_absence_claim_check_allows_the_disclaimer_that_denies_it(text, rejected):
    # Paid P1: the honest summary "coverage gap, not proof no vacancies exist" was
    # rejected as an absence claim, forcing the loop into the no-progress stop.
    from eregion.runtime.career.source_health import validate_coverage
    state = {'messages': [{'role': 'user', 'id': 'u1', 'text': 'find jobs'}],
             'search_source_health': {'user_turn_id': 'u1', 'sources': {'https://www.indeed.com': {'blocked': True}}}}
    try:
        validate_coverage(state, text + ' Indeed was blocked.')
        raised = False
    except ValueError as exc:
        raised = 'absence of vacancies' in str(exc)
    assert raised is rejected


def test_new_grad_title_satisfies_junior_request_and_vice_versa():
    # Paid P1b: "Graduate Data Analyst" (qualified by the evaluator) got
    # "Seniority mismatch: requested junior; this posting is new_grad".
    from fake_search_qualification import recommendation_warning
    assert recommendation_warning({'levels': ['junior'], 'strict': False}, 'Graduate Data Analyst', '') is None
    assert recommendation_warning({'levels': ['new_grad'], 'strict': False}, 'Junior Data Analyst', '') is None
    assert recommendation_warning({'levels': ['junior'], 'strict': False}, 'Data Analyst Intern', '')['actual'] == 'intern'
    assert recommendation_warning({'levels': ['junior'], 'strict': False}, 'Senior Data Analyst', '')['actual'] == 'senior'


def test_inspect_application_defaults_to_what_blocks_progress():
    # W6 review: the raw field list (~19k characters) made the career agent page through it
    # six times. The default view names only the blocking fields; include_fields has the rest.
    from types import SimpleNamespace
    from eregion.runtime.career.harness import CareerHarness
    harness = CareerHarness.__new__(CareerHarness)
    fields = [{'key': 'end', 'label': 'To*', 'kind': 'text', 'required': True, 'value': '', 'options': [{'label': str(i)} for i in range(200)]},
              {'key': 'title', 'label': 'Job Title*', 'kind': 'text', 'required': True, 'value': 'Research'}]
    child = {'run_id': 'r1', 'status': 'needs_input', 'version': 3, 'fields': fields, 'messages': [], 'agent_reason': 'page_budget'}
    harness.applications = SimpleNamespace(repository=SimpleNamespace(get=lambda run_id: child))
    view = harness.application('r1')
    assert 'fields' not in view and view['blocking_fields'] == [{'key': 'end', 'label': 'To*', 'kind': 'text'}]
    assert len(str(view)) < 1000
    assert [f['key'] for f in harness.application('r1', include_fields=True)['fields']] == ['end', 'title']


def test_a_limit_stop_tells_the_career_agent_to_report_the_blocker_not_to_forward_again():
    # Adobe re-test (a455c9f): after a page-budget stop the career agent re-inspected the full
    # field list and searched jobs for 10 calls, never telling the user what was blocking.
    from types import SimpleNamespace
    from eregion.runtime.career.harness import CareerHarness
    harness = CareerHarness.__new__(CareerHarness)
    child = {'run_id': 'r1', 'status': 'needs_input', 'version': 4, 'messages': [], 'agent_reason': 'page_budget',
             'message': 'This page used its model-call budget without finishing.',
             'fields': [{'key': 'major', 'label': 'Field of Study', 'kind': 'text', 'required': False, 'value': ''}]}
    harness.applications = SimpleNamespace(repository=SimpleNamespace(get=lambda run_id: child))
    stopped = harness.capability_stopped({'kind': 'application', 'id': 'r1'})
    assert stopped['agent_reason'] == 'page_budget' and stopped['blocking_fields'] == []
    assert 'Tell the user what blocks it' in stopped['next'] and 'reply_application' not in stopped['next']
    child.update(agent_reason='user_question', input_request={'id': 'q1', 'text': 'Start year?'})
    assert 'input_request' in harness.capability_stopped({'kind': 'application', 'id': 'r1'})['next']
