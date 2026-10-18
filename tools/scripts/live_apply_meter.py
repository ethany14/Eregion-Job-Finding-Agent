"""Approved Adobe re-test meter, evidence collector and 20-minute active-time guard.

Usage: APPLY_TEST_DATA=<data dir> APPLY_TEST_BASE=<server> live_apply_meter.py <task_id>   (polls every 5 s; exits when the task waits for the user, reaches
review, ends, or the active-time cap is hit). Totals and evidence continue across restarts.

Evidence (append-only, deduplicated, redacted: no field values or personal data):
  evidence/decisions.jsonl   every application tool decision (tool, field key, failed, error code)
  evidence/engine-events.jsonl  application engine events (slice ends, rejections, stops)
  evidence/app-events.jsonl  application status messages (recoveries, budget stops)
  evidence/career.jsonl      career agent tool calls (tool, short args, failed)
  meter-log.jsonl            per-poll totals: provider attempts, tokens, active/human time
Provider attempts and input tokens are capped in-process by the task usage ledger (60 / 1M),
which now also covers application worker threads; this script only reads it.
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request
from collections import Counter

HERE = os.path.abspath(os.environ.get('APPLY_TEST_DATA', '.codex-runs/apply-test'))
BASE = os.environ.get('APPLY_TEST_BASE', 'http://127.0.0.1:8020')
DB = os.path.join(HERE, 'eregion-apply-test.sqlite3')
EVIDENCE = os.path.join(HERE, 'evidence')
ACTIVE_CAP = 20 * 60
LOG = os.path.join(HERE, 'meter-log.jsonl')
os.makedirs(EVIDENCE, exist_ok=True)


def append_new(name, items, key):
    path = os.path.join(EVIDENCE, name)
    seen = set()
    if os.path.exists(path):
        with open(path, encoding='utf-8') as handle:
            seen = {json.loads(line)['_id'] for line in handle if line.strip()}
    with open(path, 'a', encoding='utf-8') as handle:
        for item in items:
            identity = key(item)
            if identity not in seen:
                seen.add(identity)
                handle.write(json.dumps({'_id': identity, **item}, default=str) + '\n')


def ledger(task_id):
    c = sqlite3.connect(DB)
    rows = [json.loads(r[0]) for r in c.execute('select details_json from provider_usage where task_id=?', (task_id,))]
    provider = [d for d in rows if d.get('source') == 'provider']
    return {
        'provider_attempts': len(provider),
        'pending_attempts': sum(d.get('status') == 'pending' for d in provider),
        'failed_attempts': sum(d.get('status') == 'failed' for d in provider),
        'retries': sum((d.get('attempt') or 1) > 1 for d in provider),
        'input_tokens_known': sum(d['input_tokens'] for d in provider if 'input_tokens' in d),
        'input_tokens_budget': sum(d.get('input_tokens', d.get('reserved_input_tokens', 0)) or 0 for d in provider),
        'unknown_usage_attempts': sum('input_tokens' not in d for d in provider),
        'output_tokens_known': sum(d.get('output_tokens') or 0 for d in provider),
        'provider_seconds': round(sum(d.get('duration_seconds') or 0 for d in provider), 1),
        'by_purpose': dict(Counter(d.get('purpose') for d in provider)),
    }


def collect(task_id, state):
    c = sqlite3.connect(DB)
    task = json.loads(c.execute('select state_json from career_tasks where task_id=?', (task_id,)).fetchone()[0])
    append_new('career.jsonl', [{'at': h.get('at'), 'tool': h['tool'], 'args': str(h.get('arguments'))[:120],
                                 'error': (h.get('result') or {}).get('error') if isinstance(h.get('result'), dict) else None}
                                for h in task.get('history', [])], key=lambda i: i['at'] + i['tool'])
    for run_id in task.get('application_runs') or []:
        row = c.execute('select state_json from auto_applications where run_id=?', (run_id,)).fetchone()
        if not row:
            continue
        app = json.loads(row[0])
        append_new('decisions.jsonl', [{'run': run_id, 'tool': h['tool'], 'call_id': h.get('call_id'),
            'field_key': (h.get('arguments') or {}).get('field_key') if isinstance(h.get('arguments'), dict) else None,
            'failed': h.get('failed'),
            'error_code': (h.get('result') or {}).get('error_code') if isinstance(h.get('result'), dict) else None,
            'page': h.get('page')} for h in app.get('decision_history') or []], key=lambda i: str(i['call_id']))
        append_new('engine-events.jsonl', [{'run': run_id, **{k: v for k, v in e.items() if k in
            ('sequence', 'kind', 'at', 'reason', 'tool', 'failed', 'revision', 'tool_count')}}
            for e in (app.get('loop_runtime') or {}).get('events', [])], key=lambda i: f"{i['run']}:{i.get('sequence')}")
        append_new('app-events.jsonl', [{'run': run_id, **e} for e in app.get('events', [])],
                   key=lambda i: i['run'] + i['at'] + i['status'])


def settled(task_id):
    """Silence is not proof of the end: nothing may be running, queued or scheduled in the
    career task or any of its applications."""
    c = sqlite3.connect(DB)
    task = json.loads(c.execute('select state_json from career_tasks where task_id=?', (task_id,)).fetchone()[0])
    if task['status'] in {'running', 'queued', 'waiting'} or task.get('wait'):
        return False
    for run_id in task.get('application_runs') or []:
        row = c.execute('select state_json from auto_applications where run_id=?', (run_id,)).fetchone()
        app = json.loads(row[0]) if row else {}
        if app.get('status') in {'queued', 'running', 'submitting'} or app.get('retry_at') or app.get('host_inflight'):
            return False
    return True


POLL_GAP = 60  # a poll step longer than this was sleep or downtime, not work or waiting


def stop_server():
    """Active-time cap: stop the test server so no further model call starts, keeping the
    task and application recoverable (the plan forbids cancelling or discarding on a pause)."""
    import re
    import subprocess
    port = BASE.rsplit(':', 1)[-1].strip('/')
    out = subprocess.run(['netstat', '-ano', '-p', 'TCP'], capture_output=True, text=True).stdout
    pids = {m[1] for m in re.finditer(r':' + port + r'\s+\S+\s+LISTENING\s+(\d+)', out)}
    for pid in pids:
        subprocess.run(['taskkill', '/PID', pid, '/T', '/F'], capture_output=True)
    return sorted(pids)


def time_phase(state, pending=0):
    # The public status may come from the paused application while career is
    # still wrapping up. Those provider calls are active work, not human wait.
    if pending or state.get('task_status', state.get('status')) in {'running', 'queued', 'waiting'}:
        return 'active'
    if state.get('application_status') == 'awaiting_review' or state.get('status') in {'completed', 'cancelled', 'failed'}:
        return 'idle'
    if state.get('agent_reason') in {'page_budget', 'application_budget', 'agent_limit'}:
        return 'idle'
    return 'human' if state.get('status') == 'needs_input' else 'active'


def main(task_id):
    active = human = gap = idle_seconds = 0.0
    try:
        with open(LOG, encoding='utf-8') as log:
            previous = json.loads(log.readlines()[-1])
        active, human = float(previous['active_s']), float(previous['human_wait_s'])
        gap = float(previous.get('gap_s', 0)); idle_seconds = float(previous.get('idle_s', 0))
    except (OSError, IndexError, KeyError, ValueError):
        pass
    last = time.monotonic()
    quiet, previous_attempts = 0, None
    while True:
        time.sleep(5)
        now = time.monotonic(); step = now - last; last = now
        state = json.load(urllib.request.urlopen(f'{BASE}/api/career/tasks/{task_id}', timeout=30))
        status, app = state['status'], state.get('application_status')
        usage = ledger(task_id)
        phase = time_phase(state, usage['pending_attempts'])
        waiting_user = phase == 'human'
        # apply-test-06: one ~1 h step (machine asleep) was booked as human wait.
        if step > POLL_GAP:
            gap += step
        elif waiting_user:
            human += step
        elif phase == 'idle':
            idle_seconds += step
        else:
            active += step
        collect(task_id, state)
        record = {'t': time.strftime('%H:%M:%S'), 'status': status, 'application': app,
                  'active_s': round(active), 'human_wait_s': round(human), 'gap_s': round(gap), 'idle_s': round(idle_seconds), **usage,
                  'message': (state.get('message') or '')[:120]}
        with open(LOG, 'a', encoding='utf-8') as log:
            log.write(json.dumps(record) + '\n')
        if active >= ACTIVE_CAP:
            stopped = stop_server()
            print('CAP active time reached; server stopped (state kept)', stopped, json.dumps(record))
            return
        # Re-test gap: the career agent kept working (10 calls) after the application stopped, but
        # the meter had already exited. Stop only after three quiet polls without ledger activity.
        idle = settled(task_id) and record['pending_attempts'] == 0
        record['settled'] = idle
        quiet = quiet + 1 if status == 'needs_input' and idle and record['provider_attempts'] == previous_attempts else 0
        previous_attempts = record['provider_attempts']
        if quiet >= 3 or status in {'completed', 'cancelled', 'failed'} or app in {'awaiting_review', 'cancelled', 'failed'}:
            print('STOP', json.dumps(record))
            return


if __name__ == '__main__':
    main(sys.argv[1])
