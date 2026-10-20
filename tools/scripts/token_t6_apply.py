"""Real-model My Experience run on the local fixture (T6, user-approved 2026-10-20). One paid run per folder.

Run once per arm from the code version under test (a worktree for the earlier version). The fixture page
with two work and two education rows is served to local Chrome; every other request is aborted, so the
only outside connections are the configured model providers. The production HybridApplicationAgent runs
with its configured model and decisions; the model settings come from the main checkout's .env (passed
with --env, values never printed or saved).

Caps per arm, enforced before every send by the experiment ledger and the guard: 12 provider attempts
(nested judgments included), 900,000 input-token units (pending requests hold their reservation, UTF-8
bytes + 1,024), 600 seconds. Reasoning escalation and rejection retries are off.

Usage: token_t6_apply.py run --out DIR --env PATH_TO_.env --label NAME
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'tools' / 'scripts')]

# Attempt 1 of the earlier arm (4 calls) stopped at the first scheduler yield (run() was called once);
# attempt 2 (7 calls) clicked Next, which replaces the page, before the values were read. Values are now
# taken at Next and both remaining arms use 10 attempts, so the approved total (2 x 16) is not exceeded.
# Selective-model check C (approved 2026-10-20) lowers input to 300,000 units per run, 4 runs.
CAPS = {'provider_attempts': 10, 'input_tokens': 300_000, 'seconds': 600}
EXPECTED = {'work': {1: {'jobTitle': 'Research', 'companyName': 'University', 'startDate-dateSectionMonth-input': '8',
                         'startDate-dateSectionYear-input': '2026', 'current': True},
                     2: {'jobTitle': 'Intern', 'companyName': 'Employer', 'startDate-dateSectionMonth-input': '7',
                         'startDate-dateSectionYear-input': '2022', 'endDate-dateSectionMonth-input': '8',
                         'endDate-dateSectionYear-input': '2022', 'current': False}},
            'education': {1: {'schoolName': 'School A', 'degree': 'Masters'}, 2: {'schoolName': 'School B', 'degree': 'Bachelors'}}}


def page_values(page):
    return page.evaluate('''() => Object.fromEntries([...document.querySelectorAll('input,select,textarea')]
        .filter(e => e.id).map(e => [e.id, e.type === 'checkbox' ? e.checked : e.value]))''')


# The fixture's Next replaces the page body; a capture listener keeps the values as they were at Next.
KEEP_AT_NEXT = '''() => document.addEventListener('submit', () => { window.valuesAtNext = Object.fromEntries(
    [...document.querySelectorAll('input,select,textarea')].filter(e => e.id)
    .map(e => [e.id, e.type === 'checkbox' ? e.checked : e.value])); }, true)'''


def final_values(page):
    return page.evaluate('window.valuesAtNext') or page_values(page)


def check(values):
    """Compare the page with the profile; month inputs may be written with or without a leading zero."""
    problems = []
    for kind, prefix in (('work', 'workExperience'), ('education', 'education')):
        for n, expected in EXPECTED[kind].items():
            for field, want in expected.items():
                got = values.get(f'{prefix}-{n}--{field}')
                same = (got == want if isinstance(want, bool)
                        else str(got or '').lstrip('0') == want.lstrip('0') if field.endswith('Month-input') else got == want)
                if not same:
                    problems.append({'field': f'{prefix}-{n}--{field}', 'expected': want, 'got': got})
    return problems


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['run', 'precheck'])
    parser.add_argument('--out', required=True)
    parser.add_argument('--env', required=True)
    parser.add_argument('--label', required=True)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    from dotenv import dotenv_values
    os.environ.update({k: v for k, v in dotenv_values(args.env).items() if v is not None})
    ledger = out / 'experiment-ledger.sqlite3'
    experiment = f'token-t6-apply-{args.label}'
    os.environ.update(JOB_AGENT_REASONING_ESCALATION='0', JOB_AGENT_REASONING_REJECTION_RETRY='0',
                      JOB_AGENT_ANALYSIS_WARMUP='0', JOB_AGENT_EXPERIMENT_ID=experiment,
                      JOB_AGENT_EXPERIMENT_LEDGER=str(ledger),
                      # precheck: a zero-attempt cap refuses the first call before it is sent.
                      JOB_AGENT_EXPERIMENT_MAX_PROVIDER_ATTEMPTS=str(CAPS['provider_attempts'] if args.command == 'run' else 0),
                      JOB_AGENT_EXPERIMENT_MAX_INPUT_TOKENS=str(CAPS['input_tokens']))
    from run_guard import guarded, require
    with guarded(out / f'exit-{args.command}', deadline_s=CAPS['seconds']) as run:
        if args.command == 'run':
            with (out / 'paid-started.lock').open('x') as lock:  # one paid run per folder
                lock.write(time.strftime('%Y-%m-%dT%H:%M:%S'))
        from token_baseline_apply import EARLIER_ANSWERS, MESSAGES, REVIEW_FIELDS, base_state, fixture_browser
        from eregion.runtime.auto_apply.agent import HybridApplicationAgent
        from eregion.runtime.auto_apply.autofill import autofill
        from eregion.runtime.provider_usage import TaskProviderBudgetExceeded, experiment_summary
        state = base_state(answers=dict(EARLIER_ANSWERS), review_fields=REVIEW_FIELDS, messages=MESSAGES)
        browser = fixture_browser('experience_page.html', lambda p: (p.locator('#addWork').click(), p.locator('#addSchool').click(),
                                                                     p.evaluate(KEEP_AT_NEXT)))
        started = time.monotonic()
        finished = lambda: browser.page.evaluate('window.nextCount > 0 || window.submitCount > 0')
        active = lambda: not finished() and time.monotonic() - started < CAPS['seconds'] - 60
        error, returned, slices = None, None, 0
        agent = HybridApplicationAgent()
        try:
            # Like ApplicationLoop: a scheduler yield resumes with the same state and browser.
            while True:
                slices += 1
                returned = agent.run(browser, state, autofill=lambda: autofill(browser, state, lambda: b''),
                                     active=active, reserve=lambda kind: True, report=lambda *a: None)
                if (returned or {}).get('reason') != 'yielded' or not active():
                    break
            values = final_values(browser.page)
            counts = browser.page.evaluate('[window.nextCount ?? 0, window.submitCount ?? 0]')
        except (Exception, TaskProviderBudgetExceeded) as exc:  # a cap stop or error is recorded, not raised
            error = f'{type(exc).__name__}: {str(exc)[:300]}'
            values, counts = final_values(browser.page), browser.page.evaluate('[window.nextCount ?? 0, window.submitCount ?? 0]')
        finally:
            browser.close()
        history = state.get('decision_history', [])
        (out / f'state-{args.command}.json').write_text(json.dumps(
            {'values': values, 'field_decisions': state.get('field_decisions', {}), 'decision_history': history},
            indent=1, default=str), encoding='utf-8')
        usage = experiment_summary(str(ledger), experiment) if ledger.exists() else {}
        decisions = state.get('field_decisions', {})
        current_saved = sorted(k for k in decisions if 'current' in k)
        run['result'] = {
            'label': args.label, 'seconds': round(time.monotonic() - started, 1), 'returned': (returned or {}).get('reason'),
            'slices': slices,
            'error': error, 'next_clicks': counts[0], 'submits': counts[1],
            'problems': check(values), 'current_decisions_saved': current_saved,
            'tools': [h['tool'] for h in history],
            'tool_errors': [h['tool'] + ':' + str((h.get('result') or {}).get('error_code')) for h in history
                            if isinstance(h.get('result'), dict) and h['result'].get('error')],
            'usage': {k: usage.get(k) for k in ('calls', 'input_tokens', 'output_tokens', 'cached_input_tokens',
                                                'unknown_usage_calls', 'refused_before_send', 'budget_input_tokens')},
            'usage_by_purpose': [{k: g.get(k) for k in ('purpose', 'calls', 'input_tokens', 'output_tokens')}
                                 for g in usage.get('groups', [])]}
        require(counts[1] == 0, 'a submit was attempted')
        if args.command == 'precheck':
            require(not usage.get('calls') and usage.get('refused_before_send'), f'precheck sent a request: {run["result"]["usage"]}')


if __name__ == '__main__':
    main()
