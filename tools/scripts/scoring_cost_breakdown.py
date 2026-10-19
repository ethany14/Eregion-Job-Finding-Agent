"""Scoring cost breakdown for a finished layer-2 route run (read-only; makes no requests).

Per route: wall-time split (main model, qualification, Jev, everything else), cost per purpose,
per scoring chunk (requests, tokens, seconds, jobs) and per job (source, description length,
extracted requirements, and the Jev stage work its requirements needed). Jev stage requests are
shared by every job of a chunk, so per-job request counts do not exist; the per-job stage
question counts (focused, clarified, citation checks) are the attributable work instead.

Usage: scoring_cost_breakdown.py RUN_DIR [A B ...] [--out report.json]
"""
import argparse
import collections
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'backend')]


def ts(value):
    return datetime.fromisoformat(str(value).replace(' ', 'T')).replace(tzinfo=None) if value else None


def union_seconds(intervals):
    total, end = 0.0, None
    for start, stop in sorted(intervals):
        if end is None or start > end:
            total += (stop - start).total_seconds(); end = stop
        elif stop > end:
            total += (stop - end).total_seconds(); end = stop
    return round(total, 1)


def category(purpose):
    if purpose == 'agent':
        return 'main_model'
    if purpose.startswith('search_qualification'):
        return 'qualification'
    if purpose.startswith('jev:'):
        return 'jev_scoring'
    return 'other_model'


def route(run_dir, name):
    from eregion.runtime.career.alerts import job_key
    manifest = json.loads((run_dir / f'layer2-{name}.json').read_text(encoding='utf-8'))
    db = sqlite3.connect(f"file:{run_dir / f'route-{name}.sqlite3'}?mode=ro", uri=True)
    state = [json.loads(x) for (x,) in db.execute('select state_json from career_tasks')][0]
    ledger_path = run_dir / f'route-{name}.experiment.sqlite3'
    rows = []
    if ledger_path.exists():  # a run that made no provider call writes no ledger
        ledger = sqlite3.connect(f'file:{ledger_path}?mode=ro', uri=True)
        rows = [(ts(at), json.loads(d)) for at, d in ledger.execute('select created_at, details_json from provider_usage order by created_at')]
    started = ts(state['messages'][0]['at'])
    finished = ts([m for m in state['messages'] if m['role'] == 'assistant'][-1]['at'])
    intervals, purposes = collections.defaultdict(list), collections.defaultdict(lambda: collections.Counter())
    for at, d in rows:
        seconds = d.get('duration_seconds') or 0
        intervals[category(d['purpose'])].append((at, at + timedelta(seconds=seconds)))
        p = purposes[d['purpose']]
        p['calls'] += 1; p['input_tokens'] += d.get('input_tokens') or 0; p['output_tokens'] += d.get('output_tokens') or 0
        p['provider_seconds'] += round(d.get('provider_duration_seconds') or seconds, 3)
    wall = round((finished - started).total_seconds(), 1)
    busy = {k: union_seconds(v) for k, v in intervals.items()}
    any_model = union_seconds([i for v in intervals.values() for i in v])
    # Chunks: each Jev chunk starts with requirement extraction after a non-extraction stage.
    chunks, previous = [], None
    for at, d in rows:
        if not d['purpose'].startswith('jev:'):
            continue
        stage = d['purpose'].split(':')[-1]
        if not chunks or (stage == 'extract_requirements' and previous != 'extract_requirements'):
            chunks.append({'requests': 0, 'input_tokens': 0, 'output_tokens': 0, 'start': at, 'end': at, 'stages': collections.Counter()})
        c = chunks[-1]
        c['requests'] += 1; c['input_tokens'] += d.get('input_tokens') or 0; c['output_tokens'] += d.get('output_tokens') or 0
        c['end'] = at + timedelta(seconds=d.get('duration_seconds') or 0); c['stages'][stage] += 1; previous = stage
    # Jobs: match reports in creation order are assigned to chunks by time.
    # The frozen descriptions the route scored are in the task's retrieved_jobs (the manifest omits them).
    text_of = {k: (v.get('description') or '') for k, v in (state.get('retrieved_jobs') or {}).items()}
    source_of = {job_key(j['url']): {**j, 'description': text_of.get(job_key(j['url']), '')} for j in manifest['included']}
    jobs = []
    for application_id, created, content in db.execute(
            "select a.application_id, a.created_at, a.content_json from application_artifacts a where a.artifact_type='match_report' order by a.created_at"):
        url = db.execute('select j.canonical_url from applications p join jobs j on p.job_id=j.job_id where p.application_id=?', (application_id,)).fetchone()[0]
        frozen = source_of.get(job_key(url), {})
        report = json.loads(content)
        decisions = (report.get('decision_details') or {}).get('decisions') or []
        created_at = ts(created)
        # A chunk's reports are saved when it finishes: the latest chunk that started before the report.
        chunk = max((i for i, c in enumerate(chunks) if c['start'] <= created_at), default=None)
        jobs.append({'source': frozen.get('source'), 'title': (frozen.get('title') or '')[:60], 'company': (frozen.get('company') or '')[:30],
                     'description_chars': len(frozen.get('description') or ''), 'requirements': len(decisions),
                     'focused': sum(1 for x in decisions if any((x.get('focused') or {}).values())),
                     'clarified': sum(1 for x in decisions if x.get('clarified_full_support')),
                     'citation_checks': sum(1 for x in decisions if x.get('citation_verification')),
                     'score': report.get('overall_score'), 'chunk': chunk})
    for i, c in enumerate(chunks):
        c['jobs'] = sum(1 for j in jobs if j['chunk'] == i)
        c['seconds'] = round((c['end'] - c['start']).total_seconds(), 1)
        c['start_offset_s'] = round((c['start'] - started).total_seconds(), 1)
        c['stages'] = dict(c['stages']); del c['start'], c['end']
    agent = [d for _, d in rows if d['purpose'] == 'agent']
    unscored = [j for j in source_of.values() if job_key(j['url']) not in {job_key(db.execute(
        'select j.canonical_url from applications p join jobs j on p.job_id=j.job_id where p.application_id=?', (a,)).fetchone()[0])
        for (a,) in db.execute("select application_id from application_artifacts where artifact_type='match_report'")}]
    return {
        'route': name, 'wall_seconds': wall,
        'busy_seconds': {**busy, 'any_model': any_model, 'no_model_call_running': round(wall - any_model, 1)},
        'purposes': {k: dict(v) for k, v in purposes.items()},
        'main_model_turns': {'count': len(agent), 'seconds': [round(d.get('duration_seconds') or 0, 1) for d in agent],
                             'input_tokens': [d.get('input_tokens') for d in agent], 'output_tokens': [d.get('output_tokens') for d in agent]},
        'chunks': chunks, 'jobs': jobs,
        'unscored': [{'source': j['source'], 'title': j['title'][:60], 'description_chars': len(j.get('description') or '')} for j in unscored],
        'description_chars_by_source': {s: round(sum(len(j['description']) for j in source_of.values() if j['source'] == s) /
                                                 max(1, sum(1 for j in source_of.values() if j['source'] == s)))
                                        for s in {j['source'] for j in source_of.values()}},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('run_dir')
    parser.add_argument('routes', nargs='*', default=['A', 'B'])
    parser.add_argument('--out')
    args = parser.parse_args()
    report = {name: route(Path(args.run_dir), name) for name in args.routes}
    text = json.dumps(report, indent=1, ensure_ascii=False, default=str)
    if args.out:
        Path(args.out).write_text(text, encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main()
