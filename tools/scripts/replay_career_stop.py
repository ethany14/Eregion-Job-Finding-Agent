"""Replay the career agent's wake-up at the saved Adobe page_budget stop with the real model.

Prepares a copy of the saved test database rolled back to the moment the application stopped
(career history, messages and ledger rows after that moment are removed), marks the career
task as waiting on the stopped application, and serves it with the real career loop, prompt
and tools. Tools that would touch a website or the application are recorded, not executed.

Usage: replay_career_stop.py --source <saved data dir> --data <replay data dir> --stop <ISO time> --port N
Budget: JOB_AGENT_EXPERIMENT_* must be set (shared ledger); task caps stay those of the run.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'tools')]
LOCAL_TOOLS = {'inspect_application', 'wait_for_result', 'ask_user', 'wait_for_user', 'finish_task', 'say',
               'update_plan', 'read_context', 'pause_task'}


def prepare(source, data, stop):
    data.mkdir(parents=True, exist_ok=True)
    target = data / 'eregion-apply-test.sqlite3'
    shutil.copyfile(Path(source) / 'eregion-apply-test.sqlite3', target)
    db = sqlite3.connect(target)
    task_id, task_json = db.execute('select task_id, state_json from career_tasks').fetchone()
    task = json.loads(task_json)
    run_id = task['application_runs'][-1]
    app = json.loads(db.execute('select state_json from auto_applications where run_id=?', (run_id,)).fetchone()[0])
    loop_src = (ROOT / 'backend/eregion/runtime/auto_apply/loop.py').read_text(encoding='utf-8')
    import re
    stop_message = re.search(r'"page_budget": "([^"]+)"', loop_src).group(1)
    app.update(status='needs_input', agent_reason='page_budget', message=stop_message, retry_at=None,
               input_request=None, pause_requested=False, cancel_requested=False)
    app['events'] = [e for e in app.get('events', []) if e['at'] <= stop]
    task['history'] = [h for h in task['history'] if h['at'] <= stop]
    task['messages'] = [m for m in task['messages'] if m.get('at', '') <= stop]
    task.update(status='waiting', wait={'kind': 'application', 'id': run_id, 'token': 'replay-stale-token'},
                pause_requested=False, input_request=None, error=None, model_activity=None)
    db.execute('update career_tasks set state_json=? where task_id=?', (json.dumps(task), task_id))
    db.execute('update auto_applications set state_json=?, status=? where run_id=?', (json.dumps(app), 'needs_input', run_id))
    removed = db.execute('delete from provider_usage where created_at > ?', (stop,)).rowcount
    db.commit()
    kept = db.execute("select count(*) from provider_usage where task_id=? and json_extract(details_json,'$.source')='provider'", (task_id,)).fetchone()[0]
    return {'task_id': task_id, 'run_id': run_id, 'ledger_rows_removed_after_stop': removed, 'provider_attempts_at_stop': kept,
            'history_at_stop': len(task['history']), 'messages_at_stop': len(task['messages'])}


def install_replay_guard(trace_path):
    """Control tools (ask_user, wait_for_result, finish_task, say, ...) are handled by the task
    loop and never reach the harness. Of the harness tools only local reads run for real."""
    from eregion.runtime.career.harness import CareerHarness
    original = CareerHarness.execute

    def execute(self, task_id, name, args, *, call_id):
        executed = name in LOCAL_TOOLS
        with open(trace_path, 'a', encoding='utf-8') as trace:
            trace.write(json.dumps({'tool': name, 'executed': executed, 'args': args, 'call_id': call_id}, default=str) + '\n')
        if not executed:
            return {'error': 'Replay: this tool was recorded but not executed (no website or application action in this replay).'}
        return original(self, task_id, name, args, call_id=call_id)
    CareerHarness.execute = execute


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--data', required=True)
    parser.add_argument('--stop', required=True)
    parser.add_argument('--port', type=int, default=8025)
    args = parser.parse_args()
    for name in ('JOB_AGENT_EXPERIMENT_ID', 'JOB_AGENT_EXPERIMENT_LEDGER', 'JOB_AGENT_EXPERIMENT_MAX_PROVIDER_ATTEMPTS',
                 'JOB_AGENT_EXPERIMENT_MAX_INPUT_TOKENS'):
        if not os.getenv(name):
            raise SystemExit(f'{name} must be set: the replay is budgeted by the shared experiment ledger.')
    data = Path(args.data).resolve()
    summary = prepare(args.source, data, args.stop)
    (data / 'prepared.json').write_text(json.dumps(summary, indent=1), encoding='utf-8')
    os.environ['JOB_AGENT_DATABASE_URL'] = 'sqlite:///' + (data / 'eregion-apply-test.sqlite3').as_posix()
    os.environ['JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS'] = '60'   # the original run's task caps
    os.environ['JOB_AGENT_TASK_MAX_INPUT_TOKENS'] = '1000000'
    os.environ.setdefault('JOB_AGENT_HIDDEN_BROWSER', 'offscreen')
    from scripts.live_apply_server import install_guards
    install_guards(data)
    install_replay_guard(data / 'career-tool-trace.jsonl')
    import uvicorn
    uvicorn.run('eregion.api.main:app', host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
