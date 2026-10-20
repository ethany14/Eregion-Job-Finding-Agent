"""Where an application run's calls went (read-only; makes no requests, writes nothing but the report).

Per run directory (a saved live application test, e.g. .codex-runs/apply-test-35): main-model calls per
business step, nested paid judgments recorded in the run (answer verification, form option decisions),
tool actions per step, and the calls that bought no progress: rejected batches, reads of a field whose
result did not change, repeated identical searches and turns that only reported progress.

Sources: final-state.json (timing summary, step budgets) and full-actions.jsonl (every action with its
arguments and result). Outer career-loop calls live in the run's experiment ledger; when that database
cannot be read consistently the report says so instead of guessing. No personal values are printed:
field keys, tool names, error codes and counts only.

Usage: apply_call_breakdown.py RUN_DIR [RUN_DIR ...] [--out report.json]
"""
import argparse
import ast
import collections
import hashlib
import json
from pathlib import Path

PROGRESS_ONLY = {'say', 'update_plan'}
READS = {'read_field', 'inspect_field', 'view_field'}
REJECTED_CODES = {'batch_conflict', 'batch_incomplete', 'stale_observation'}


def arguments(row):
    value = row.get('arguments')
    if isinstance(value, str):
        try:
            value = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            value = {}
    return value if isinstance(value, dict) else {}


def result(row):
    value = row.get('result')
    return value if isinstance(value, dict) else {}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def turns(rows):
    """Model actions grouped into the turn that chose them: actions on one observation, or adjacent
    actions without one. Host actions (host:*) end a turn."""
    groups, current, key = [], [], None
    for row in rows:
        if row['tool'].startswith('host:'):
            if current:
                groups.append(current)
            current, key = [], None
            continue
        observation = arguments(row).get('observation_id')
        if current and observation and key and observation != key:
            groups.append(current)
            current = []
        current.append(row)
        key = observation or key
    if current:
        groups.append(current)
    return groups


def analyze(run):
    source = run / 'final-state.json' if (run / 'final-state.json').exists() else run / 'latest-application.json'
    state = json.loads(source.read_text(encoding='utf-8'))
    state = state.get('state', state) if isinstance(state.get('state'), dict) else state
    rows = [json.loads(line) for line in (run / 'full-actions.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    timing = state.get('timing_summary', {})
    nested = {name: timing[name].get('provider_calls', timing[name].get('count'))
              for name in timing if name in {'answer_verification', 'form_options', 'fast_options', 'draft_answer'}}
    model = timing.get('model', {})
    by_tool = collections.Counter(r['tool'] for r in rows)
    by_page = collections.defaultdict(collections.Counter)
    for r in rows:
        by_page[str(r.get('page') or '')[-60:]][r['tool']] += 1
    errors = collections.Counter(result(r).get('error_code') or ('error' if result(r).get('error') else None)
                                 for r in rows if result(r).get('error'))
    rejected = [r for r in rows if result(r).get('error_code') in REJECTED_CODES
                or 'Batch rejected' in str(result(r).get('error', ''))]
    seen_reads, repeated_reads = {}, []
    seen_searches, repeated_searches = set(), []
    for r in rows:
        args = arguments(r)
        if r['tool'] in READS and args.get('field_key'):
            value = digest({k: v for k, v in result(r).items() if k not in {'observation_id', 'at'}})
            if seen_reads.get((r['tool'], args['field_key'])) == value:
                repeated_reads.append({'tool': r['tool'], 'field_key': args['field_key']})
            seen_reads[(r['tool'], args['field_key'])] = value
        if r['tool'] == 'search_options':
            signature = (args.get('field_key'), str(args.get('query', '')).strip().casefold())
            if signature in seen_searches:
                repeated_searches.append({'field_key': signature[0]})
            seen_searches.add(signature)
    grouped = turns(rows)
    progress_only = [g for g in grouped if all(r['tool'] in PROGRESS_ONLY for r in g)]
    return {'run': run.name, 'state_source': source.name,
            'main_model_calls': model.get('provider_calls', model.get('count')),
            'main_model_tokens': {'input': model.get('input_tokens_total'), 'output': model.get('output_tokens_total'),
                                  'samples': model.get('input_tokens_samples')},
            'main_model_calls_by_step': state.get('step_budgets', {}).get('counts', {}),
            'nested_paid_calls': nested,
            'outer_career_calls': 'unknown: not recorded in final-state.json; read the run experiment ledger',
            'actions': len(rows), 'actions_by_tool': dict(by_tool.most_common()),
            'model_turns_reconstructed': len(grouped),
            'rejected_batches': {'actions': len(rejected), 'codes': dict(collections.Counter(
                result(r).get('error_code') or 'batch_rejected' for r in rejected))},
            'repeated_unchanged_reads': {'count': len(repeated_reads),
                                         'fields': dict(collections.Counter(r['field_key'] for r in repeated_reads))},
            'repeated_identical_searches': {'count': len(repeated_searches),
                                            'fields': dict(collections.Counter(r['field_key'] for r in repeated_searches))},
            'progress_only_turns': len(progress_only),
            'error_codes': {str(k): v for k, v in errors.items() if k},
            'actions_by_page': {page: dict(c) for page, c in by_page.items()}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('runs', nargs='+')
    parser.add_argument('--out')
    args = parser.parse_args()
    report = [analyze(Path(run)) for run in args.runs]
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text, encoding='utf-8')
    for item in report:
        print(json.dumps({k: v for k, v in item.items() if k not in {'actions_by_page', 'actions_by_tool'}}, indent=1, ensure_ascii=False))


if __name__ == '__main__':
    main()
