"""The live-test meter only ends when nothing is running, queued or scheduled (silence alone
is not proof): career task, its applications and in-flight work."""
import importlib.util
import json
import sqlite3
from pathlib import Path


def load_meter(tmp_path, monkeypatch):
    monkeypatch.setenv('APPLY_TEST_DATA', str(tmp_path))
    spec = importlib.util.spec_from_file_location('live_apply_meter', Path(__file__).parents[1] / 'tools/scripts/live_apply_meter.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def test_settled_requires_no_running_queued_or_scheduled_work(tmp_path, monkeypatch):
    meter = load_meter(tmp_path, monkeypatch)
    db = sqlite3.connect(tmp_path / 'eregion-apply-test.sqlite3')
    db.execute('create table career_tasks (task_id text, state_json text)')
    db.execute('create table auto_applications (run_id text, state_json text)')
    def put(task, app):
        db.execute('delete from career_tasks'); db.execute('delete from auto_applications')
        db.execute('insert into career_tasks values (?,?)', ('t', json.dumps({'application_runs': ['a'], **task})))
        db.execute('insert into auto_applications values (?,?)', ('a', json.dumps(app))); db.commit()
    put({'status': 'needs_input'}, {'status': 'needs_input'})
    assert meter.settled('t')
    for task, app in [({'status': 'running'}, {'status': 'needs_input'}),            # career wrap-up still running
                      ({'status': 'needs_input', 'wait': {'id': 'a'}}, {'status': 'needs_input'}),
                      ({'status': 'needs_input'}, {'status': 'queued'}),             # application queued
                      ({'status': 'needs_input'}, {'status': 'needs_input', 'retry_at': 123.0}),  # retry scheduled
                      ({'status': 'needs_input'}, {'status': 'needs_input', 'host_inflight': {'tool': 'autofill'}})]:
        put(task, app)
        assert not meter.settled('t'), (task, app)
