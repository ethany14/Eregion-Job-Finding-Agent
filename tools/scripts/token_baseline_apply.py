"""Free request-size baseline for the application agent: production loop, local fixtures, scripted model.

Each scenario serves a local fixture page to real Chrome (every other request is aborted), runs the
production HybridApplicationAgent loop and replaces only the model: a scripted model receives the request
the production code built (system prompt, tools, replayed history, live snapshot), records its sizes, and
returns fixed tool calls. Nothing reaches a provider or a real site.

Reports characters and UTF-8 bytes only (no token estimate). Outputs hold sizes and hashes, never text.
Hand-off waiting makes no model call by construction (tests/test_verification_handoff.py), so it has no
request to measure here.

Usage: token_baseline_apply.py --out FILE [--label NAME] [--only SCENARIO,...]
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'tools' / 'scripts')]
from token_baseline import duplicate_bytes, no_network, sha, size  # noqa: E402

FIXTURES = ROOT / 'tests' / 'fixtures' / 'auto_apply'
URL = 'https://example.wd5.myworkdayjobs.com/en-US/External/job/Engineer_JR123'
RESUME = (ROOT / 'tests' / 'fixtures' / 'resume_source_regressions' / 'avery.txt').read_text(encoding='utf-8')
PROFILE = {'first_name': 'Avery', 'last_name': 'Lin', 'email': 'avery.lin.qa@example.com', 'phone': '+1 607 555 0148',
           'city': 'Ithaca, NY', 'country': 'United States', 'linkedin': 'linkedin.com/in/avery-lin-synthetic-qa',
           'experience': [{'id': 'one', 'title': 'Research', 'company': 'University', 'start_date': 'Aug 2026', 'current': 'Present',
                           'description': 'Builds Python evaluation services and data pipelines for a research group.'},
                          {'id': 'two', 'title': 'Intern', 'company': 'Employer', 'start_date': 'Jul 2022', 'end_date': 'Aug 2022',
                           'description': 'Wrote SQL reports and dashboards for an operations team.'}],
           'education': [{'id': 'a', 'school': 'School A', 'degree': 'Masters', 'field_of_study': 'Systems Engineering',
                          'start_date': 'Aug 2025', 'end_date': 'May 2027'},
                         {'id': 'b', 'school': 'School B', 'degree': 'Bachelors', 'field_of_study': 'Computer Science',
                          'start_date': 'Sep 2021', 'end_date': 'May 2025'}]}
# Answers and pages from earlier steps, as a run carries them by My Experience.
EARLIER_ANSWERS = {'select:previousWorker:1': 'No', 'select:country--country:1': 'United States',
                   'text:legalName--firstName:1': 'Avery', 'text:legalName--lastName:1': 'Lin',
                   'text:email:1': 'avery.lin.qa@example.com', 'text:phone:1': '+1 607 555 0148'}
REVIEW_FIELDS = [{'key': k, 'kind': k.split(':')[0], 'label': k.split(':')[1].replace('--', ' '), 'value': v, 'required': True,
                  'options': [], 'section': 'My Information', 'group': '', 'errors': [], 'invalid': False}
                 for k, v in EARLIER_ANSWERS.items()]
MESSAGES = [{'id': 'm1', 'role': 'user', 'text': 'Apply to this job with my saved resume.'},
            {'id': 'm2', 'role': 'assistant', 'text': 'Do you need visa sponsorship now or in the future?'},
            {'id': 'm3', 'role': 'user', 'text': 'no', 'in_reply_to': 'm2'},
            {'id': 'm4', 'role': 'assistant', 'text': 'Are you willing to relocate to New York, NY?'},
            {'id': 'm5', 'role': 'user', 'text': 'yes, I can relocate', 'in_reply_to': 'm4'}]


def fixture_browser(name, setup=None):
    from eregion.runtime.auto_apply.browser import ApplicationBrowser

    class FixtureBrowser(ApplicationBrowser):
        def _guard_request(self, route):
            if route.request.is_navigation_request():
                route.fulfill(content_type='text/html', body=(FIXTURES / name).read_text(encoding='utf-8'))
            else:
                route.abort()

    browser = FixtureBrowser(URL, headless=True)
    if setup:
        setup(browser.page)
    return browser


def base_state(**changes):
    state = {'automation_mode': 'hybrid', 'answers': {}, 'status': 'running', 'chat_revision': 1, 'messages': [],
             'profile': json.loads(json.dumps(PROFILE)), 'resume_context': {'name': 'resume.pdf', 'text': RESUME}}
    state.update(changes)
    return state


def keyof(snap, part, n=1):
    return next(f['key'] for f in snap['fields'] + snap.get('field_index', [])
                if part in f['key'] and f['key'].endswith(f':{n}'))


def control(snap, label):
    return next(c for c in snap['page']['controls'] if c['label'].startswith(label))


def measure(messages, tools):
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    live_index = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage) and isinstance(m.content, str))
    live_text = messages[live_index].content
    live = json.loads(live_text)
    system = ''.join(m.content for m in messages if isinstance(m, SystemMessage) and isinstance(m.content, str))
    history = [m.model_dump() for i, m in enumerate(messages) if i != live_index and not isinstance(m, SystemMessage)]
    fields = live.get('fields') or []
    request = {'messages': [m.model_dump() for m in messages], 'tools': tools}
    return {'request': size(request), 'system': size(system), 'tools': size(tools), 'tool_count': len(tools),
            'history': size(history), 'history_messages': len(history), 'live_snapshot': size(live),
            'snapshot_fields_bytes': {k: size(v)['bytes'] for k, v in live.items()},
            'field_parts_bytes': {'options': sum(size(f.get('options', []))['bytes'] for f in fields),
                                  'control_input': sum(size(f.get('control_input', ''))['bytes'] for f in fields),
                                  'allowed_source_refs': sum(size(f.get('allowed_source_refs', []))['bytes'] for f in fields),
                                  'fields': len(fields)},
            'resume_text_included': bool((live.get('resume_context') or {}).get('text')),
            'duplicate_bytes': duplicate_bytes(request),
            'assistant_turns_in_history': sum(isinstance(m, AIMessage) for m in messages)}


def run_scenario(name, fixture, state, turns, setup=None, done=None):
    """turns: functions snapshot -> [(tool, args)]; after them the model asks the user, which ends the run."""
    from langchain_core.messages import AIMessage, HumanMessage
    from eregion.runtime.auto_apply.agent import HybridApplicationAgent
    from eregion.runtime.auto_apply.autofill import autofill
    browser = fixture_browser(fixture, setup)
    requests, scripted = [], iter(turns)

    class Model:
        def invoke(self, messages, tools):
            snap = json.loads(next(m.content for m in reversed(messages) if isinstance(m, HumanMessage) and isinstance(m.content, str)))
            requests.append(measure(messages, tools))
            try:
                calls = next(scripted)(snap)
            except StopIteration:
                return AIMessage(content='', tool_calls=[{'id': 'end', 'name': 'ask_user',
                                                          'args': {'text': 'Baseline scenario ended.', 'field_keys': []}}])
            for tool, args in calls:
                if tool != 'autofill':
                    args.setdefault('observation_id', snap['observation_id'])
            return AIMessage(content='', tool_calls=[{'id': f'{len(requests)}-{i}', 'name': n, 'args': a}
                                                     for i, (n, a) in enumerate(calls)])

    finished = (lambda: browser.page.evaluate(done)) if done else (lambda: False)
    try:
        returned = HybridApplicationAgent(model_factory=lambda: Model()).run(
            browser, state, autofill=lambda: autofill(browser, state, lambda: b''), active=lambda: not finished(),
            reserve=lambda kind: True, report=lambda *a: None)
        outcome = browser.page.evaluate('[window.nextCount ?? null, window.submitCount ?? null]')
        if (returned or {}).get('reason') == 'internal_error':
            raise SystemExit(f'token_baseline_apply: scenario {name} ended with an internal error')
    finally:
        browser.close()
    history = state.get('decision_history', [])
    errors = [h['tool'] + ':' + str((h.get('result') or {}).get('error_code') or 'error')
              for h in history if (h.get('result') or {}).get('error')]
    return {'scenario': name, 'fixture': fixture, 'requests': requests,
            'request_count': len(requests), 'total_request_bytes': sum(r['request']['bytes'] for r in requests),
            'outcome': {'ended': (returned or {}).get('reason'), 'next_clicks': outcome[0], 'submits': outcome[1], 'tool_errors': errors,
                        'decisions': len(state.get('field_decisions', {}))}}


WORK = {'jobTitle': 'title', 'companyName': 'company', 'startDate-dateSectionMonth': 'start_month',
        'startDate-dateSectionYear': 'start_year'}


def experience_turns():
    def work(n, record):
        def turn(s):
            parts = dict(WORK, **({'endDate-dateSectionMonth': 'end_month', 'endDate-dateSectionYear': 'end_year'} if n == 2 else {}))
            fields = [{'field_key': keyof(s, p, n), 'component': c} for p, c in parts.items()]
            return [('bind_experience', {'experience_id': record, 'fields': fields}),
                    ('resolve_field', {'field_key': keyof(s, 'current', n), 'mode': 'equivalent', 'value': 'true' if n == 1 else 'false',
                                       'source_refs': [f'experience:{record}:' + ('current' if n == 1 else 'end_date')],
                                       'reason': 'Saved employment evidence.'}),
                    ('autofill', {'field_keys': [f['field_key'] for f in fields]})]
        return turn

    def school(n, record, degree):
        def turn(s):
            return [('bind_education', {'education_id': record, 'fields': [{'field_key': keyof(s, 'schoolName', n), 'component': 'school'},
                                                                           {'field_key': keyof(s, 'degree', n), 'component': 'degree'}]}),
                    ('type_from_source', {'field_key': keyof(s, 'schoolName', n), 'source_ref': f'education:{record}:school'}),
                    ('resolve_field', {'field_key': keyof(s, 'degree', n), 'mode': 'equivalent', 'value': degree,
                                       'source_refs': [f'education:{record}:degree'], 'reason': 'Exact degree option.'}),
                    ('resolve_field', {'field_key': keyof(s, 'fieldOfStudy', n), 'mode': 'leave_empty', 'value': '', 'source_refs': [],
                                       'reason': 'Optional field; no listed option matches the saved field of study.'})]
        return turn

    return [work(1, 'one'), work(2, 'two'), school(1, 'a', 'Masters'), school(2, 'b', 'Bachelors'),
            lambda s: [('click_page_action', {'control_id': control(s, 'Next')['id']})]]


def long_options_turns():
    # The long list is inspected once; the next turns work on other fields while it stays on the page.
    return [lambda s: [('inspect_field', {'field_key': keyof(s, 'field')})],
            lambda s: [('bind_education', {'education_id': 'a', 'fields': [{'field_key': keyof(s, 'school'), 'component': 'school'},
                                                                           {'field_key': keyof(s, 'degree'), 'component': 'degree'}]})],
            lambda s: [('type_from_source', {'field_key': keyof(s, 'school'), 'source_ref': 'education:a:school'}),
                       ('resolve_field', {'field_key': keyof(s, 'degree'), 'mode': 'equivalent', 'value': 'Masters',
                                          'source_refs': ['education:a:degree'], 'reason': 'Exact degree option.'})],
            lambda s: [('resolve_field', {'field_key': keyof(s, 'field'), 'mode': 'leave_empty', 'value': '', 'source_refs': [],
                                          'reason': 'Optional; no listed option matches the saved field of study.'})]]


def search_turns():
    return [lambda s: [('inspect_field', {'field_key': keyof(s, 'fieldOfStudy')})],
            lambda s: [('search_options', {'field_key': keyof(s, 'fieldOfStudy'), 'query': 'Systems Engineering',
                                           'source_ref': 'education:a:field_of_study'})],
            lambda s: [('resolve_field', {'field_key': keyof(s, 'fieldOfStudy'), 'mode': 'leave_empty', 'value': '', 'source_refs': [],
                                          'reason': 'Optional; the search found no option for Systems Engineering.'})]]


SCENARIOS = {
    'personal_info': lambda: run_scenario('personal_info', 'workday.html', base_state(),
                                          [], setup=lambda p: p.evaluate('personal()')),
    # Same page and state, with and without a draft saved for an open question on an earlier page.
    'experience_first_request': lambda: run_scenario(
        'experience_first_request', 'experience_page.html', base_state(answers=dict(EARLIER_ANSWERS), review_fields=REVIEW_FIELDS),
        [], setup=lambda p: (p.locator('#addWork').click(), p.locator('#addSchool').click())),
    'experience_first_request_after_draft': lambda: run_scenario(
        'experience_first_request_after_draft', 'experience_page.html', base_state(
            answers=dict(EARLIER_ANSWERS), review_fields=REVIEW_FIELDS,
            answer_drafts={'textarea:why:1': {'field': {'label': 'Why this company?', 'kind': 'textarea'}, 'text': 'Draft text.'}}),
        [], setup=lambda p: (p.locator('#addWork').click(), p.locator('#addSchool').click())),
    'experience_two_work_two_education': lambda: run_scenario(
        'experience_two_work_two_education', 'experience_page.html',
        base_state(answers=dict(EARLIER_ANSWERS), review_fields=REVIEW_FIELDS, messages=MESSAGES),
        experience_turns(), setup=lambda p: (p.locator('#addWork').click(), p.locator('#addSchool').click()),
        done='window.nextCount > 0'),
    'long_options_after_inspect': lambda: run_scenario(
        'long_options_after_inspect', 'token_long_options.html', base_state(review_fields=REVIEW_FIELDS), long_options_turns()),
    'field_of_study_search_no_option': lambda: run_scenario(
        'field_of_study_search_no_option', 'workday_search.html', base_state(), search_turns()),
    'questions_with_user_answers': lambda: run_scenario(
        'questions_with_user_answers', 'token_questions.html',
        base_state(answers=dict(EARLIER_ANSWERS), review_fields=REVIEW_FIELDS, messages=MESSAGES), []),
    'upload': lambda: run_scenario('upload', 'workday_upload_carryover.html', base_state(), []),
    'review': lambda: run_scenario('review', 'workday.html', base_state(answers=dict(EARLIER_ANSWERS), review_fields=REVIEW_FIELDS),
                                   [], setup=lambda p: p.evaluate("window.saved={first_name:'Avery',last_name:'Lin'}; review()")),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True)
    parser.add_argument('--label', default='current')
    parser.add_argument('--only', default='')
    args = parser.parse_args()
    no_network()
    import eregion.runtime.auto_apply.fast_options as fast_options
    fast_options.fill_options = lambda *a, **k: []  # no nested paid option decisions in a baseline
    only = [s for s in args.only.split(',') if s]
    results = [SCENARIOS[name]() for name in SCENARIOS if not only or name in only]
    summary = {r['scenario']: {'requests': r['request_count'], 'total_request_bytes': r['total_request_bytes'],
                               'first_request_bytes': r['requests'][0]['request']['bytes'] if r['requests'] else 0,
                               'outcome': r['outcome']} for r in results}
    report = {'label': args.label, 'kind': 'apply', 'units': 'characters and UTF-8 bytes; no token estimate',
              'inputs': {'resume_sha256': sha(RESUME), 'profile_sha256': sha(json.dumps(PROFILE, sort_keys=True))},
              'summary': summary, 'scenarios': results}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
