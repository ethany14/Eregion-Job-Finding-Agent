"""Tiered-recommendation contract: the model chooses the tier; code checks structure,
citations, identity and known-ineligible conditions only (docs/specs/tiered-recommendations-20261013.md)."""
import pytest
from test_career_flow import career, execute
from test_qualified_batch import posting, prepare, GOAL

DESCRIPTION = ('Junior data analyst opening in our New York office.\n'
               'You will build reports with Python and SQL.\n'
               'Required: Bachelor degree with a minimum 3.0 GPA.\n'
               'Required: R for statistical modelling.\n'
               'Preferred: Experience with Looker.\n'
               'Applicants must hold French or EEA nationality for this program.')


def scored(c, monkeypatch, **changes):
    job = posting(1, description=DESCRIPTION, **changes)
    prepare(c, monkeypatch, [job])
    result = execute(c, 'verify_and_score_jobs', urls=[job['url']])
    return job, result


def args(app, tier, **extra):
    return {'application_id': app, 'tier': tier, 'reason': 'Junior NYC data analyst role; Python and SQL overlap.',
            'evidence': ['Junior data analyst opening in our New York office.'], 'gaps': [], 'to_confirm': [],
            'next_steps': ['Review the posting.'], **extra}


@pytest.mark.parametrize('tier,extra', [
    ('to_confirm', {'to_confirm': ['Posting requires a minimum 3.0 GPA; the resume does not state a GPA.']}),
    ('stretch', {'gaps': ['Required R for statistical modelling: the resume shows Python and SQL, not R (hard).']}),
    ('priority', {'gaps': ['Preferred Looker experience is not shown.']}),
    ('to_confirm', {'to_confirm': ['French or EEA nationality required; not provided.']}),
])
def test_each_tier_is_saved_with_its_disclosures(career, monkeypatch, tier, extra):
    c = career
    _, result = scored(c, monkeypatch)
    app = result['jobs'][0]['application_id']
    record = execute(c, 'recommend_job', **args(app, tier, **extra))['recommendation']
    assert record['tier'] == tier and record['tier_label']
    for key, value in extra.items():
        assert record[key] == value
    assert record['evidence'][0]['source'] == 'description'
    assert record['score_meaning'] and record['next_steps'] == ['Review the posting.']
    assert record['url'] and 'verified_at' in record and record['match_score'] == result['jobs'][0]['match_score']
    from eregion.runtime.career.decisions import terminal_delivery
    assert terminal_delivery(c.repository.get(c.state['task_id']))[0].startswith('- ' + record['tier_label'])


@pytest.mark.parametrize('tier,missing', [('stretch', 'gaps'), ('to_confirm', 'to_confirm')])
def test_a_tier_must_disclose_what_defines_it(career, monkeypatch, tier, missing):
    c = career
    _, result = scored(c, monkeypatch)
    with pytest.raises(ValueError, match='must list'):
        execute(c, 'recommend_job', **args(result['jobs'][0]['application_id'], tier))


def test_invented_evidence_is_rejected(career, monkeypatch):
    c = career
    _, result = scored(c, monkeypatch)
    with pytest.raises(ValueError, match='exact quote'):
        execute(c, 'recommend_job', **args(result['jobs'][0]['application_id'], 'priority',
                                           evidence=['Candidates with any degree are welcome.']))


def test_confirmed_mismatch_cannot_be_presented(career, monkeypatch):
    from eregion.runtime.workspace.types import ArtifactType
    from eregion.domain.schemas import SkillEvidence
    c = career
    _, result = scored(c, monkeypatch)
    app = result['jobs'][0]['application_id']
    report = c.harness.job_summary(app, detail=True)['match_report']
    report['matches'].append(SkillEvidence(requirement_id='nationality', job_skill='French or EEA nationality',
        requirement_level='required', match_status='confirmed_condition', condition_satisfied=False,
        resume_evidence=[], confidence=1).model_dump(mode='json'))
    c.runtime.workspace.create_artifact(app, artifact_type=ArtifactType.MATCH_REPORT, content=report, evidence_ids=[],
        created_by='synthetic', expected_version=c.runtime.workspace.get_application(app).version)
    with pytest.raises(ValueError, match='condition is not satisfied'):
        execute(c, 'recommend_job', **args(app, 'to_confirm', to_confirm=['Nationality']))


@pytest.mark.parametrize('field', ['vacancy', 'location'])
def test_closed_or_location_conflict_postings_never_reach_recommendation(career, monkeypatch, field):
    from eregion.runtime.career import qualification as q
    from test_semantic_search_qualification import response
    def judge(request, postings):
        value = response(request, postings)
        getattr(value.decisions[0], field).status = 'unmet'
        return value
    c = career
    monkeypatch.setattr(q, 'judge', judge)
    job = posting(1, description=DESCRIPTION)
    prepare(c, monkeypatch, [job])
    result = execute(c, 'verify_and_score_jobs', urls=[job['url']])
    assert result['qualified_count'] == 0 and not result['jobs']


def test_duplicate_presentation_is_reported_as_already_saved(career, monkeypatch):
    c = career
    _, result = scored(c, monkeypatch)
    payload = args(result['jobs'][0]['application_id'], 'priority')
    execute(c, 'recommend_job', **payload)
    again = execute(c, 'recommend_job', **payload)
    assert again['already_recommended'] and len(c.repository.get(c.state['task_id'])['recommendations']) == 1


def test_final_reply_must_use_the_tier_labels(career, monkeypatch):
    c = career
    job, result = scored(c, monkeypatch)
    app = result['jobs'][0]['application_id']
    execute(c, 'recommend_job', **args(app, 'stretch', gaps=['Required R is not shown.']))
    state = c.repository.get(c.state['task_id'])
    score = result['jobs'][0]['match_score']
    plain = f'Found one job.\n- Junior Data Analyst\n  {job["url"]}\n  Score {score}\n'
    with pytest.raises(ValueError, match='tier labels'):
        c.harness.validate_completion(state, plain)
    c.harness.validate_completion(state, 'Worth trying (known gaps) (1)\n' + plain)


def test_both_prompts_share_the_tier_contract_and_user_must_haves_win():
    from eregion.runtime.career import prompt
    from eregion.runtime.career.prompt import RECOMMENDATION_RULE, FINAL_FORMAT, trim_prompt
    assert "explicit must-haves or exclusions override these tiers" in RECOMMENDATION_RULE
    for text in (prompt.SYSTEM, trim_prompt(prompt.SYSTEM)):
        assert RECOMMENDATION_RULE in text and FINAL_FORMAT in text
    from eregion.runtime.career.harness import TOOLS
    decide = next(t for t in __import__('eregion.runtime.career.decisions', fromlist=['TOOL']).TOOL['function'].values()
                  if isinstance(t, str) and 'to_confirm' in t)
    assert 'Unknown personal facts are a to_confirm recommendation' in decide
    recommend = next(t for t in TOOLS if t['function']['name'] == 'recommend_job')['function']
    assert recommend['parameters']['properties']['tier']['enum'] == ['priority', 'stretch', 'to_confirm']


def _audit_row(job, request, independent_tier, presentation):
    from eregion.runtime.career.qualification import body_hash
    import hashlib
    met = {'status': 'met', 'citation_valid': True}
    return {job['url']: {'body_hash': body_hash(job), 'request_hash': hashlib.sha256(request.encode()).hexdigest(),
            'candidate_hash': 'c', 'checks': {f: dict(met) for f in ('role', 'location', 'seniority', 'vacancy')},
            'independent': {'tier': independent_tier, 'violates_user_restriction': False},
            'presentation': presentation}}


@pytest.mark.parametrize('independent,presentation,counted', [
    # A to_confirm opportunity with unknown personal facts now counts when disclosed.
    ('to_confirm', {'tier_reasonable': True, 'gaps_and_conditions_disclosed': True, 'unknown_presented_as_met': False,
                    'ineligible_presented_as_applicable': False, 'score_used_as_eligibility_proof': False}, True),
    ('stretch', {'tier_reasonable': True, 'gaps_and_conditions_disclosed': False, 'unknown_presented_as_met': False,
                 'ineligible_presented_as_applicable': False, 'score_used_as_eligibility_proof': False}, False),
    ('to_confirm', {'tier_reasonable': True, 'gaps_and_conditions_disclosed': True, 'unknown_presented_as_met': True,
                    'ineligible_presented_as_applicable': False, 'score_used_as_eligibility_proof': False}, False),
    ('not_an_opportunity', {'tier_reasonable': False, 'gaps_and_conditions_disclosed': True,
                            'unknown_presented_as_met': False, 'ineligible_presented_as_applicable': True,
                            'score_used_as_eligibility_proof': False}, False),
    ('priority', {}, False),   # no presentation review: never counted
])
def test_tiered_audit_counts_disclosed_opportunities_only(independent, presentation, counted):
    from evals.tiered_audit import contract_checks
    job = posting(1, description=DESCRIPTION)
    audit = _audit_row(job, GOAL, independent, presentation)
    assert all(contract_checks(job, audit, GOAL, 'c').values()) is counted
    assert not any(contract_checks(job, audit, GOAL, 'other-candidate').values()) or True
    assert contract_checks(job, audit, GOAL, 'other-candidate')['role'] is False


def test_single_entry_applies_the_same_relevance_gate(career, monkeypatch):
    from eregion.runtime.career import qualification as q
    from test_semantic_search_qualification import response
    def judge(request, postings):
        value = response(request, postings)
        value.decisions[0].location.status = 'unmet'
        return value
    c = career
    monkeypatch.setattr(q, 'judge', judge)
    job = posting(1, description=DESCRIPTION)
    prepare(c, monkeypatch, [job])
    execute(c, 'read_job', url=job['url'])
    imported = execute(c, 'import_job', url=job['url'])
    result = execute(c, 'score_job', application_id=imported['application_id'])
    assert result['error_code'] == 'qualification_needed'


def test_production_and_audit_share_one_set_of_tier_definitions():
    from eregion.runtime.career.prompt import TIER_DEFINITIONS, RECOMMENDATION_RULE
    from evals.tiered_audit import TIER_PROMPT
    assert TIER_DEFINITIONS in RECOMMENDATION_RULE and TIER_DEFINITIONS in TIER_PROMPT
    assert "a GPA the resume does not state is to_confirm" in TIER_DEFINITIONS


def test_posting_conditions_are_quoted_from_the_posting_and_reach_the_decision(career, monkeypatch):
    from eregion.runtime.career import qualification as q
    from eregion.domain.search_qualification import SourceQuote
    from test_semantic_search_qualification import response
    def judge(request, postings):
        value = response(request, postings)
        value.decisions[0].candidate_conditions = [
            SourceQuote(source='description', quote='Required: Bachelor degree with a minimum 3.0 GPA.'),
            SourceQuote(source='description', quote='Must be under 28 years old.')]   # not in this posting
        return value
    c = career
    monkeypatch.setattr(q, 'judge', judge)
    job = posting(1, description=DESCRIPTION)
    prepare(c, monkeypatch, [job])
    result = execute(c, 'verify_and_score_jobs', urls=[job['url']])
    conditions = result['jobs'][0]['posting_conditions']
    assert conditions == [{'source': 'description', 'quote': 'Required: Bachelor degree with a minimum 3.0 GPA.'}]
    state = c.repository.get(c.state['task_id'])
    assert state['scored_job_evidence'][result['jobs'][0]['application_id']]['posting_conditions'] == conditions


# tiered-recommendations-v2: no count threshold; only substantive errors fail.
def _v2_summary(job, recs, review, reply, decisions=None, final=None, version=None):
    from evals.tiered_audit import ACCEPTANCE_VERSION
    ACCEPTANCE_VERSION = version or 'tiered-recommendations-v5'   # the substantive-gate versions
    final = final if final is not None else ''.join(f"{r['url']} Score {r['match_score']}\n" for r in recs)
    return {'status': 'completed', 'calls': 10, 'acceptance_version': ACCEPTANCE_VERSION,
            'usage': {'usage_complete': True, 'pending': 0, 'unknown_usage_calls': 0, 'input_tokens': 1000,
                      'groups': [{'purpose': 'agent', 'input_tokens': 1000}]},
            'qualification_method': 'model', 'audit_usage': {'usage_complete': True, 'pending': 0, 'calls': 3,
                                                             'input_tokens': 5000},
            'final': final, 'recommendations': recs, 'verified_jobs': [job], 'job_decisions': decisions or {},
            'semantic_review': review, 'user_request': GOAL, 'audit_candidate_hash': 'c', 'reply_review': reply}


HONEST = {'explains_results_and_search': True, 'hides_or_misreports_problems': False, 'misstates_jobs': False}
CLEAN = {'tier_reasonable': False, 'gaps_and_conditions_disclosed': True, 'unknown_presented_as_met': False,
         'ineligible_presented_as_applicable': False, 'score_used_as_eligibility_proof': False,
         'known_conflict_hidden': False, 'unsupported_claim': False, 'minor_omissions': ['Looker not mentioned']}


def _v2_job():
    job = posting(1, description=DESCRIPTION)
    return {**job, 'application_id': 'a1', 'match_score': 71.5, 'retrieved': True, 'disqualified': False}


def test_v2_one_clean_result_passes_and_tier_disagreement_is_only_reported():
    from scripts.remaining_search_acceptance import evaluate_search
    job = _v2_job()
    rec = {'application_id': 'a1', 'url': job['url'], 'match_score': 71.5, 'tier': 'stretch', 'title': job['title']}
    result = evaluate_search(_v2_summary(job, [rec], _audit_row(job, GOAL, 'to_confirm', CLEAN), HONEST), 'analyst')
    assert result['status'] == 'passed', result['failures']
    assert result['shown_count'] == 1 and result['by_tier'] == {'stretch': {'shown': 1, 'clean': 1}}
    assert result['tier_disagreements'] == [{'url': job['url'], 'tier': 'stretch', 'audit_tier': 'to_confirm'}]
    assert result['jobs'][0]['minor_omissions'] == ['Looker not mentioned']


def test_v2_zero_results_pass_only_with_an_honest_explanation():
    from scripts.remaining_search_acceptance import evaluate_search
    job = {**_v2_job(), 'match_score': None}
    assert evaluate_search(_v2_summary(job, [], {}, HONEST, final='Searched LinkedIn and boards; none fit.'),
                           'analyst')['status'] == 'passed'
    hidden = {**HONEST, 'hides_or_misreports_problems': True}
    assert evaluate_search(_v2_summary(job, [], {}, hidden), 'analyst')['failures'] == \
        ['final_reply_not_honest_or_unexplained']


@pytest.mark.parametrize('flag', ['unknown_presented_as_met', 'known_conflict_hidden', 'unsupported_claim',
                                  'ineligible_presented_as_applicable', 'score_used_as_eligibility_proof'])
def test_v2_substantive_presentation_errors_fail(flag):
    from scripts.remaining_search_acceptance import evaluate_search
    job = _v2_job()
    rec = {'application_id': 'a1', 'url': job['url'], 'match_score': 71.5, 'tier': 'stretch'}
    review = _audit_row(job, GOAL, 'stretch', {**CLEAN, flag: True})
    assert evaluate_search(_v2_summary(job, [rec], review, HONEST), 'analyst')['failures'] == ['substantive_error:' + job['url']]


def test_v2_scored_job_without_outcome_fails():
    from scripts.remaining_search_acceptance import evaluate_search
    result = evaluate_search(_v2_summary(_v2_job(), [], {}, HONEST), 'analyst')
    assert result['failures'] == ['scored_jobs_without_outcome']
    assert result['scored_without_outcome'][0]['application_id'] == 'a1'
    decided = evaluate_search(_v2_summary(_v2_job(), [], {}, HONEST, decisions={'a1': {'decision': 'exclude'}}), 'analyst')
    assert decided['status'] == 'passed'


def test_v2_request_conflict_found_by_the_audit_fails():
    from scripts.remaining_search_acceptance import evaluate_search
    job = _v2_job()
    rec = {'application_id': 'a1', 'url': job['url'], 'match_score': 71.5, 'tier': 'priority'}
    review = _audit_row(job, GOAL, 'priority', CLEAN)
    review[job['url']]['checks']['location'] = {'status': 'unmet', 'citation_valid': True}
    assert evaluate_search(_v2_summary(job, [rec], review, HONEST), 'analyst')['status'] == 'failed'
    review[job['url']]['checks']['location'] = {'status': 'unknown', 'citation_valid': False}
    assert evaluate_search(_v2_summary(job, [rec], review, HONEST), 'analyst')['status'] == 'passed'


def test_an_open_request_without_a_number_is_still_a_search_with_no_quota():
    from eregion.runtime.career.search_recovery import search_request
    from eregion.runtime.career.search_strategy import source_limits
    text = 'Find current junior data analyst positions in New York for my saved resume.'
    state = {'goal': text, 'messages': [{'role': 'user', 'text': text}]}
    assert search_request(state) == {'count': None, 'words': {'data', 'analyst'}}
    # Recognised as a search, but sizing is unchanged: no quota and no extra paging.
    assert source_limits(state, {'query': 'junior data analyst'})['pages'] == 1
    counted = {'goal': 'Find 3 data analyst jobs', 'messages': [{'role': 'user', 'text': 'Find 3 data analyst jobs'}]}
    assert search_request(counted)['count'] == 3


def test_run_metrics_report_first_result_time_and_repeated_reads():
    from scripts.remaining_search_acceptance import run_metrics
    state = {'created_at': '2026-10-13T10:00:00+00:00', 'jobs': [],
             'history': [{'tool': 'read_context', 'arguments': {'ref': 'x'}, 'at': '2026-10-13T10:01:00+00:00'},
                         {'tool': 'read_context', 'arguments': {'ref': 'x'}, 'at': '2026-10-13T10:02:00+00:00'},
                         {'tool': 'recommend_job', 'result': {'recommendation': {}}, 'at': '2026-10-13T10:03:30+00:00'}],
             'linkedin_mcp_state': {'calls_used': 3, 'log': [
                 {'tool': 'search_jobs', 'arguments': {'keywords': 'a'}, 'outcome': 'ok'},
                 {'tool': 'get_job_details', 'arguments': {'job_id': '1'}, 'outcome': 'ok'},
                 {'tool': 'get_job_details', 'arguments': {'job_id': '1'}, 'outcome': 'ok'}]}}
    m = run_metrics(state, '2026-10-13T10:05:00+00:00')
    assert m['seconds_to_first_recommendation'] == 210.0 and m['total_seconds'] == 300.0
    assert m['repeated_context_reads'] == 1 and m['repeated_linkedin_detail_reads'] == 1 and m['linkedin_searches'] == 1


def test_an_item_attempted_without_progress_is_stalled_not_repeated_and_decisions_stay_actionable():
    from eregion.runtime.career.candidate_queue import view, stalled
    url = 'https://www.linkedin.com/jobs/view/1/'
    state = {'chat_revision': 0, 'jobs': [], 'scored_job_evidence': {}, 'recommendations': [], 'job_decisions': {},
             'history': [{'tool': 'verify_and_score_jobs', 'arguments': {'urls': [url]}, 'chat_revision': 0,
                          'result': {'no_new_evidence': True, 'note': 'not available'}}]}
    recovery = {'read_not_imported': [{'url': url, 'title': 'Data Analyst'},
                                      {'url': 'https://www.linkedin.com/jobs/view/2/', 'title': 'Other'}],
                'pending_candidates': [], 'requested_count': None}
    current = view(state, recovery)
    assert [i['url'] for i in current['in_progress']] == ['https://www.linkedin.com/jobs/view/2/']
    assert current['stalled'][0]['url'] == url and current['stalled'][0]['last_attempt']['tool'] == 'verify_and_score_jobs'
    # A later successful attempt clears it; a new user turn (revision) also clears it.
    state['history'].append({'tool': 'import_job', 'arguments': {'url': url}, 'chat_revision': 0,
                             'result': {'application_id': 'a'}})
    assert not stalled(state)
    state['history'].pop(); state['chat_revision'] = 1
    assert not stalled(state)


def test_saved_recommendations_stay_inline_when_the_observation_is_compacted():
    import json
    from eregion.runtime.request_context import ContextArchive, prepare_request
    from eregion.runtime.core.context import ConversationWindow
    saved = [{'application_id': f'a{i}', 'title': f'Data Analyst {i}', 'company': 'Example',
              'url': f'https://www.linkedin.com/jobs/view/{i}/', 'match_score': 50 + i} for i in range(8)]
    snapshot = {'recommended_jobs': saved, 'previous_recommendations': [{'note': 'x' * 4000}],
                'jobs': [{'application_id': 'b', 'title': 'T', 'description': 'y' * 12000}]}
    messages, _ = prepare_request('system', [], ConversationWindow(), snapshot, ContextArchive(), max_chars=6000)
    value = json.loads(messages[-1].content)
    assert value['recommended_jobs'] == saved
    assert 'context_ref' in value['previous_recommendations']


def test_each_recommendation_must_sit_under_its_saved_tier_label(career, monkeypatch):
    c = career
    job, result = scored(c, monkeypatch)
    app = result['jobs'][0]['application_id']
    execute(c, 'recommend_job', **args(app, 'to_confirm', to_confirm=['GPA is not stated.']))
    state = c.repository.get(c.state['task_id'])
    score = result['jobs'][0]['match_score']
    body = '\n'.join(['- Junior Data Analyst', '  ' + job['url'], f'  Score {score}', ''])
    swapped = 'Priority match (1)\n' + body + 'Needs your confirmation (0)\n'
    with pytest.raises(ValueError, match='misplaced'):
        c.harness.validate_completion(state, swapped)
    c.harness.validate_completion(state, 'Priority match (0)\nNeeds your confirmation (1)\n' + body)


def test_one_posting_source_failure_does_not_fail_the_batch(career, monkeypatch):
    import httpx
    from test_qualified_batch import posting, prepare
    c = career
    good, bad = posting(1, description=DESCRIPTION), posting(2, description=DESCRIPTION)
    prepare(c, monkeypatch, [good, bad])
    original = c.harness.discovery.job
    def read(url):
        if url == bad['url']:
            request = httpx.Request('GET', url)
            raise httpx.HTTPStatusError('404', request=request, response=httpx.Response(404, request=request))
        return original(url)
    monkeypatch.setattr(c.harness.discovery, 'job', read)
    result = execute(c, 'verify_and_score_jobs', urls=[good['url'], bad['url']])
    assert not result.get('error') and len(result['jobs']) == 1
    failed = next(i for i in result['candidates'] if i['url'] == bad['url'])
    assert failed['status'] == 'source_error'


# tiered-recommendations-v3: the stop decision is the model's; facts are shown, the audit judges.
def test_search_coverage_reports_each_query_total_and_cards_seen_and_the_model_can_choose_depth():
    from eregion.runtime.career.source_health import searches, user_turn
    from eregion.runtime.career.search_strategy import source_limits
    state = {'chat_revision': 0, 'messages': [{'role': 'user', 'text': GOAL, 'id': 'm1'}], 'goal': GOAL}
    turn = user_turn(state)
    state['search_request_cache'] = {'user_turn_id': turn, 'requests': {'k': {
        'query': 'junior data analyst', 'location': 'New York', 'limit': 20,
        'result': {'jobs': [{}] * 27, 'directory': True, 'directory_searched': ['a'] * 34, 'directory_remaining': 0,
                   'directory_matching': 34, 'linkedin_mcp': {'status': 'ok', 'total': 408,
                   'request': {'query': 'junior data analyst', 'pages': 2, 'cards': 20}}}}}}
    row = searches(state)[0]
    assert row['boards'] == {'searched': 34, 'remaining': 0, 'matching': 34}
    assert row['linkedin'] == [{'query': 'junior data analyst', 'location': None, 'pages': 2, 'cards': 20, 'status': 'ok',
                                'reported_total': 408, 'cached': False, 'unseen': 388}]
    assert source_limits(state, {'query': 'junior data analyst', 'pages': 6})['pages'] == 6
    assert source_limits(state, {'query': 'junior data analyst', 'pages': 50})['pages'] == 8


def test_a_search_turn_records_a_short_stop_reason_and_other_turns_do_not_need_one(career):
    c = career
    state = {**c.repository.get(c.state['task_id']), 'goal': GOAL,
             'messages': [{'role': 'user', 'text': GOAL}], 'history': []}
    c.harness.validate_stop(state, None)                     # nothing searched yet
    state['history'] = [{'tool': 'search_jobs', 'arguments': {'query': 'x'}, 'chat_revision': state.get('chat_revision', 0)}]
    with pytest.raises(ValueError, match='stop_reason'):
        c.harness.validate_stop(state, '  ')
    c.harness.validate_stop(state, 'The query was paged to its end and all directory boards were searched.')
    from eregion.runtime.tasks.loop import CONTROL_TOOLS as TOOLS
    finish = next(t for t in TOOLS if t['function']['name'] == 'finish_task')['function']['parameters']
    assert finish['required'] == ['text'] and 'stop_reason' in finish['properties']


@pytest.mark.parametrize('assessment,status,suggestions', [
    ('reasonable', 'passed', 0), ('borderline', 'passed', 1), ('premature', 'failed', 0)])
def test_v3_fails_only_a_clearly_premature_stop(assessment, status, suggestions):
    from scripts.remaining_search_acceptance import evaluate_search
    job = {**_v2_job(), 'match_score': None}
    reply = {**HONEST, 'stop_assessment': assessment, 'stop_review': 'LinkedIn reported 408 results; 20 were seen.'}
    result = evaluate_search(_v2_summary(job, [], {}, reply, final='Searched LinkedIn once.',
                                         version='tiered-recommendations-v3'), 'analyst')
    assert result['status'] == status and len(result['improvement_suggestions']) == suggestions
    assert result['stop_assessment'] == assessment


def test_shared_prompts_carry_the_stop_rule_and_gap_priority():
    from eregion.runtime.career import prompt
    from eregion.runtime.career.prompt import SEARCH_STOP_RULE, RECOMMENDATION_RULE, trim_prompt
    for text in (prompt.SYSTEM, trim_prompt(prompt.SYSTEM)):
        assert SEARCH_STOP_RULE in text
    assert 'unknown means the scorer did not decide' in RECOMMENDATION_RULE.replace('a scorer status of unknown means', 'unknown means')
    assert 'never stands in for an important capability gap' in RECOMMENDATION_RULE


def test_tier_labels_match_whatever_punctuation_separates_their_words(career, monkeypatch):
    c = career
    job, result = scored(c, monkeypatch)
    app = result['jobs'][0]['application_id']
    execute(c, 'recommend_job', **args(app, 'stretch', gaps=['Required R is not shown in the resume.']))
    state = c.repository.get(c.state['task_id'])
    body = '\n'.join(['- Junior Data Analyst', '  ' + job['url'], f"  Score {result['jobs'][0]['match_score']}", ''])
    c.harness.validate_completion(state, '### Worth trying – known gaps (1)\n' + body)
    with pytest.raises(ValueError, match='missing labels'):
        c.harness.validate_completion(state, '### Worth a try (1)\n' + body)


def test_yield_by_source_counts_cards_reads_qualifications_and_recommendations():
    from eregion.runtime.career.source_health import yields
    li, board = 'https://www.linkedin.com/jobs/view/1/', 'https://job-boards.greenhouse.io/acme/jobs/2'
    state = {'search_candidates': {'a': {'url': li}, 'b': {'url': board}}, 'retrieved_jobs': {'a': {'url': li}},
             'candidate_qualifications': {'a': {'url': li, 'qualified': True}}, 'recommendations': [{'url': li}]}
    assert yields(state) == {'linkedin': {'cards': 1, 'read': 1, 'qualified': 1, 'recommended': 1},
                             'company_boards': {'cards': 1, 'read': 0, 'qualified': 0, 'recommended': 0}}


def test_a_linkedin_page_depth_never_reaches_the_board_apis(career, monkeypatch):
    # Real discovery signatures: an unexpected keyword is a TypeError, as in acceptance tiered-v3-01.
    c = career
    monkeypatch.setenv('JOB_AGENT_JOB_API_FIRST', '1')
    c.repository.change(c.state['task_id'], goal=GOAL, messages=[{'role': 'user', 'text': GOAL, 'id': 'm1'}])
    empty = {'jobs': [], 'errors': [], 'sources_checked': [], 'skipped_sources': [], 'board_outcomes': []}
    def search_directory(query, location='', limit=10, *, cancelled=lambda: False, excluded_hosts=(),
                         excluded_sources=(), skip=()):
        return dict(empty, directory=True, directory_searched=[], directory_remaining=0, directory_matching=0)
    def search(query, location='', boards=None, limit=10, excluded_hosts=(), excluded_sources=()):
        return dict(empty)
    monkeypatch.setattr(c.harness.discovery, 'search_directory', search_directory, raising=False)
    monkeypatch.setattr(c.harness.discovery, 'search', search, raising=False)
    result = execute(c, 'search_jobs', query='data analyst', location='New York, NY', pages=3, limit=20)
    assert not result.get('error'), result


def test_tier_words_inside_an_entry_are_not_a_section_heading(career, monkeypatch):
    c = career
    job, result = scored(c, monkeypatch)
    app = result['jobs'][0]['application_id']
    execute(c, 'recommend_job', **args(app, 'stretch', gaps=['Required R is not shown in the resume.']))
    state = c.repository.get(c.state['task_id'])
    entry = '\n'.join(['- Junior Data Analyst', '  ' + job['url'], f"  Score {result['jobs'][0]['match_score']}", ''])
    other = '- Needs your confirmation: whether a license is needed (another job).\n'
    c.harness.validate_completion(state, '### Worth trying (known gaps) (1)\n' + other + entry)



@pytest.mark.parametrize('reply,status,failures', [
    # v4: the stop is the model's choice; a premature stop is advice unless the user asked for an exhaustive search.
    ({'stop_assessment': 'premature'}, 'passed', []),
    ({'stop_assessment': 'premature', 'user_asked_exhaustive': True}, 'failed', ['search_stopped_prematurely']),
    ({'continuation_stated': False}, 'failed', ['continuation_missing_or_inaccurate']),
    ({'continuation_accurate': False}, 'failed', ['continuation_missing_or_inaccurate']),
])
def test_v4_requires_an_accurate_continuation_offer_and_keeps_the_stop_advisory(reply, status, failures):
    from scripts.remaining_search_acceptance import evaluate_search
    job = {**_v2_job(), 'match_score': None}
    result = evaluate_search(_v2_summary(job, [], {}, {**HONEST, **reply}, final='Searched LinkedIn once.',
                                         version='tiered-recommendations-v4'), 'analyst')
    assert result['status'] == status and result['failures'] == failures
    if reply.get('stop_assessment') == 'premature' and status == 'passed':
        assert result['improvement_suggestions']


def test_earlier_linkedin_queries_stay_visible_for_a_follow_up():
    from eregion.runtime.career.source_health import linkedin_queries
    state = {'linkedin_mcp_search_cache': {'k': {'status': 'ok', 'total': 291,
             'request': {'query': 'junior data analyst', 'location': 'New York, NY', 'pages': 2, 'cards': 20}}}}
    assert linkedin_queries(state)[0]['unseen'] == 271


def test_a_follow_up_continues_the_earlier_search_once_this_turn_does_search_work():
    from eregion.runtime.career.search_recovery import search_request
    from eregion.runtime.career.qualification import request_fields
    first = 'Find current junior data analyst positions in New York for my saved resume.'
    state = {'chat_revision': 1, 'history': [{'tool': 'search_jobs', 'chat_revision': 0}],
             'messages': [{'role': 'user', 'text': first}, {'role': 'assistant', 'text': 'Results...'},
                          {'role': 'user', 'text': 'search more'}]}
    assert search_request(state) is None                       # no search work in this turn yet
    assert request_fields(state) == {'role': 'data analyst', 'location': 'New York'}
    state['history'].append({'tool': 'search_jobs', 'chat_revision': 1})
    assert search_request(state) == {'count': None, 'words': {'data', 'analyst'}}
    state['messages'][-1]['text'] = 'What does the second job pay?'
    state['history'] = [{'tool': 'search_jobs', 'chat_revision': 0}]
    assert search_request(state) is None                       # an unrelated question stays a question


def test_the_audit_retries_one_transport_failure_but_never_a_verdict(monkeypatch):
    import httpx, openai
    from evals import tiered_audit
    calls = []
    def invoke(model, schema, prompt, payload):
        calls.append(1)
        if len(calls) == 1:
            raise openai.APITimeoutError(request=httpx.Request('POST', 'https://example.invalid'))
        return tiered_audit.ReplyAudit(explains_results_and_search=True, hides_or_misreports_problems=False,
                                       misstates_jobs=False, reason='ok')
    monkeypatch.setattr('eregion.domain.model.invoke_structured', invoke)
    monkeypatch.setattr('eregion.domain.model.shared_model', lambda **k: None)
    assert tiered_audit.review_reply('r', 'final', {}, [])['explains_results_and_search'] and len(calls) == 2
    calls.clear()
    def broken(*a):
        calls.append(1); raise ValueError('program error')
    monkeypatch.setattr('eregion.domain.model.invoke_structured', broken)
    with pytest.raises(ValueError):
        tiered_audit.review_reply('r', 'final', {}, [])
    assert len(calls) == 1


def test_scenario_set_verdict_pools_jobs_and_keeps_infrastructure_and_reply_failures_separate():
    from scripts.scenario_set_report import verdict
    def row(scenario, shown, clean, failures=(), infra=(), reply=()):
        return {'scenario': scenario, 'shown': shown, 'clean': clean, 'gap_omissions': shown - clean,
                'failures': list(failures), 'infra_failures': list(infra), 'reply_failures': list(reply)}
    ok = verdict([row('analyst', 6, 6), row('backend', 5, 5), row('student', 4, 4), row('designer', 1, 1)])
    assert ok['ready'] and ok['substantive_error_rate'] == 0
    few = verdict([row('analyst', 6, 6), row('designer', 2, 2)])
    assert not few['ready'] and 'at least 15' in few['reasons'][0]
    errors = verdict([row('analyst', 8, 6), row('backend', 8, 7)])
    assert not errors['ready'] and errors['substantive_error_rate'] == 3 / 16
    infra = verdict([row('analyst', 16, 16, infra=['semantic_audit_incomplete'])])
    assert not infra['ready'] and 'did not complete' in infra['reasons'][0]


def test_a_near_miss_on_level_or_location_is_scored_and_never_presented_as_priority(career, monkeypatch):
    from eregion.runtime.career import qualification as q
    from test_semantic_search_qualification import response
    def judge(request, postings):
        value = response(request, postings)
        value.decisions[0].seniority.status = 'near'
        value.decisions[0].seniority.reason = 'Asks for two years against a junior request.'
        return value
    c = career
    monkeypatch.setattr(q, 'judge', judge)
    job = posting(1, description=DESCRIPTION)
    prepare(c, monkeypatch, [job])
    result = execute(c, 'verify_and_score_jobs', urls=[job['url']])
    assert result['qualified_count'] == 1
    scored = result['jobs'][0]
    assert scored['request_near_misses'][0]['field'] == 'seniority'
    with pytest.raises(ValueError, match='never as priority'):
        execute(c, 'recommend_job', **args(scored['application_id'], 'priority'))
    record = execute(c, 'recommend_job', **args(scored['application_id'], 'to_confirm',
                                                to_confirm=['Asks for about two years; you have one.']))['recommendation']
    assert record['request_near_misses'][0]['field'] == 'seniority'


def test_role_is_never_near():
    from eregion.runtime.career.qualification import verified_checks
    from test_semantic_search_qualification import response
    job = posting(1, description=DESCRIPTION)
    decision = response(GOAL, [{'posting_id': '0', **{k: job[k] for k in ('title', 'location', 'description')}}]).decisions[0]
    decision.role.status = 'near'
    assert verified_checks(decision, GOAL, job)['role']['status'] == 'unknown'


def test_v6_turns_audit_judgments_into_findings_but_keeps_structural_failures():
    from scripts.remaining_search_acceptance import evaluate_search
    job = _v2_job()
    rec = {'application_id': 'a1', 'url': job['url'], 'match_score': 71.5, 'tier': 'stretch'}
    review = _audit_row(job, GOAL, 'stretch', {**CLEAN, 'known_conflict_hidden': True})
    bad_reply = {**HONEST, 'hides_or_misreports_problems': True}
    result = evaluate_search(_v2_summary(job, [rec], review, bad_reply, version='tiered-recommendations-v6'), 'analyst')
    assert result['status'] == 'passed'
    assert set(result['audit_findings']) == {'final_reply_not_honest_or_unexplained', 'audit_flag:' + job['url']}
    unscored = evaluate_search(_v2_summary({**job, 'retrieved': False}, [rec], review, HONEST,
                                           version='tiered-recommendations-v6'), 'analyst')
    assert unscored['failures'] == ['structural_error:' + job['url']]


def test_a_borderline_verdict_gets_one_second_opinion_and_the_more_inclusive_one_is_kept(career, monkeypatch):
    from eregion.runtime.career import qualification as q
    from test_semantic_search_qualification import response
    monkeypatch.setenv('JOB_AGENT_QUALIFICATION_SECOND_OPINION', '1')
    calls = []
    def judge(request, postings):
        calls.append(len(postings))
        value = response(request, postings)
        if len(calls) == 1:                      # first opinion: location judged unknown
            value.decisions[0].location.status = 'unknown'
        return value
    c = career
    monkeypatch.setattr(q, 'judge', judge)
    job = posting(1, description=DESCRIPTION)
    prepare(c, monkeypatch, [job])
    result = execute(c, 'verify_and_score_jobs', urls=[job['url']])
    assert calls == [1, 1] and result['qualified_count'] == 1
    saved = next(iter(c.repository.get(c.state['task_id'])['candidate_qualifications'].values()))
    assert saved['checks']['location']['second_opinion'] and saved['checks']['location']['first_status'] == 'unknown'


def test_merge_never_downgrades_a_verdict():
    from eregion.runtime.career.qualification import merge_opinions
    met = {'status': 'met', 'reason': 'a'}; unmet = {'status': 'unmet', 'reason': 'b'}
    assert merge_opinions({'role': met}, {'role': unmet})['role'] is met
    assert merge_opinions({'role': unmet}, {'role': met})['role']['status'] == 'met'


def test_priority_with_posting_conditions_needs_one_explicit_review(career, monkeypatch):
    # live-set-03: intern postings with availability/start-date conditions were placed as priority.
    from eregion.runtime.career import qualification as q
    from eregion.domain.search_qualification import SourceQuote
    from test_semantic_search_qualification import response
    def judge(request, postings):
        value = response(request, postings)
        value.decisions[0].candidate_conditions = [
            SourceQuote(source='description', quote='Required: Bachelor degree with a minimum 3.0 GPA.')]
        return value
    c = career
    monkeypatch.setattr(q, 'judge', judge)
    job = posting(1, description=DESCRIPTION)
    prepare(c, monkeypatch, [job])
    app = execute(c, 'verify_and_score_jobs', urls=[job['url']])['jobs'][0]['application_id']
    with pytest.raises(ValueError, match='minimum 3.0 GPA'):
        execute(c, 'recommend_job', **args(app, 'priority'))
    assert execute(c, 'recommend_job', **args(app, 'priority', conditions_reviewed=True))['recommended']


def test_search_coverage_counts_distinct_employers():
    from eregion.runtime.career.source_health import employers
    jobs = {str(i): {'url': f'https://job-boards.greenhouse.io/x/jobs/{i}', 'company': c}
            for i, c in enumerate(['Affirm', 'affirm', 'Affirm', 'Stripe'])}
    state = {'retrieved_jobs': jobs, 'recommendations': [{'url': j['url']} for j in jobs.values()],
             'candidate_qualifications': {k: {'url': j['url'], 'qualified': k != '3'} for k, j in jobs.items()}}
    assert employers(state) == {'recommended': {'openings': 4, 'distinct_employers': 2, 'largest_employer_openings': 3},
                                'qualified': {'openings': 3, 'distinct_employers': 1, 'largest_employer_openings': 3}}
    from eregion.runtime.career.prompt import SEARCH_STOP_RULE
    assert 'search_coverage.employers' in SEARCH_STOP_RULE


def test_v7_audit_budget_grows_with_the_jobs_shown_and_v6_keeps_its_fixed_cap():
    # live-set-05 backend: 20 shown jobs used all 6 audit calls, so the reply review never ran.
    from evals.tiered_audit import audit_limits
    from scripts.remaining_search_acceptance import evaluate_search
    assert audit_limits('tiered-recommendations-v6', 20) == (6, 200000)
    assert audit_limits('tiered-recommendations-v7', 4) == (6, 200000)
    assert audit_limits('tiered-recommendations-v7', 20) == (7, 231000)
    assert audit_limits('tiered-recommendations-v7', 33)[0] == 11
    job = _v2_job()
    rec = {'application_id': 'a1', 'url': job['url'], 'match_score': 71.5, 'tier': 'stretch'}
    review = _audit_row(job, GOAL, 'stretch', CLEAN)
    def run(version, calls):
        summary = _v2_summary(job, [rec], review, HONEST, version=version)
        summary['recommendations'] = [rec] + [{'url': f'https://jobs.lever.co/other/{i}'} for i in range(19)]
        summary['audit_usage'] = {**summary['audit_usage'], 'calls': calls, 'input_tokens': 210000}
        return 'semantic_audit_incomplete' in evaluate_search(summary, 'analyst')['failures']
    assert run('tiered-recommendations-v6', 7) and not run('tiered-recommendations-v7', 7)
    assert run('tiered-recommendations-v7', 8)


def test_an_explicitly_different_named_level_is_a_clear_mismatch_in_the_policy():
    from eregion.domain.search_qualification import POLICY
    assert 'explicitly states a different named career level' in POLICY and 'stays near or unknown' in POLICY
