"""E5 S2 JobSpy source contracts (offline: fake JobSpy package, no network)."""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_career_flow import career  # noqa: F401  (pytest fixture)
from eregion.runtime.career import jobspy_source as js


def config(**changes):
    values = {'python': sys.executable, 'sites': ('indeed', 'linkedin'), 'results_per_site': 5,
              'timeout_seconds': 20, 'max_requests_per_task': 50}
    return js.JobSpyConfig(**{**values, **changes})


ROW = {'id': 'in-1', 'job_url': 'https://www.indeed.com/viewjob?jk=1', 'job_url_direct': 'https://acme.example/jobs/1',
       'title': 'Backend Engineer', 'company': 'Acme', 'location': 'New York, NY', 'date_posted': '2026-10-10',
       'description': 'Build Python services.', 'min_amount': 150000.0, 'max_amount': 180000.0, 'interval': 'yearly',
       'currency': 'USD', 'salary_source': 'direct_data', 'is_remote': False, 'job_type': 'fulltime', 'job_level': None}


def test_disabled_sends_nothing(monkeypatch):
    monkeypatch.setattr(js.subprocess, 'Popen', lambda *a, **k: pytest.fail('no request when disabled'))
    assert js.search(js.JobSpyConfig(), 'python')['status'] == 'disabled'
    assert js.search(config(python=str(Path('missing-python.exe'))), 'python')['status'] == 'disabled'


def test_normalized_card_labels_where_each_field_came_from():
    card, text = js.normalize('indeed', ROW)
    assert text == 'Build Python services.' and 'description' not in card
    assert card['source'] == 'jobspy:indeed' and card['apply_url'] == 'https://acme.example/jobs/1'
    assert card['salary']['provenance'] == 'source' and card['remote']['provenance'] == 'library_inferred'
    assert card['job_level'] == {'value': None, 'provenance': 'unknown'}
    inferred, missing = js.normalize('linkedin', {**ROW, 'description': '', 'salary_source': 'description'})
    assert missing == '' and inferred['description_status'] == 'missing' and inferred['salary']['provenance'] == 'library_inferred'


def test_one_site_failing_keeps_the_others_and_empty_is_not_blocked(monkeypatch):
    outcomes = {'indeed': {'status': 'ok', 'rows': [ROW]}, 'linkedin': {'status': 'blocked', 'rows': [], 'error': 'HTTP 429'}}
    def fake(cfg, site, query, location, filters, cancelled):
        return {'site': site, 'elapsed_s': 1.0, 'requests': 1, 'requests_observed': True, 'warnings': [], 'attrs': {},
                'error': None, 'applied_filters': {}, 'unapplied_filters': [], **outcomes[site]}
    monkeypatch.setattr(js, 'run_site', lambda cfg, site, q, l, f, cancelled=lambda: False: fake(cfg, site, q, l, f, cancelled))
    found = js.search(config(), 'python', 'New York, NY')
    assert [j['source'] for j in found['jobs']] == ['jobspy:indeed'] and found['status'] == 'ok'
    assert found['sources']['linkedin']['status'] == 'blocked' and found['requests'] == 2
    assert found['descriptions'] == {ROW['job_url']: 'Build Python services.'}
    outcomes['indeed'] = {'status': 'empty', 'rows': []}
    assert js.search(config(), 'python')['status'] == 'no_results'


def test_request_budget_stops_further_sites(monkeypatch):
    monkeypatch.setattr(js, 'run_site', lambda *a, **k: pytest.fail('budget is exhausted'))
    found = js.search(config(max_requests_per_task=10), 'python', requests_used=10)
    assert {s['status'] for s in found['sources'].values()} == {'budget_exhausted'} and found['requests'] == 0


@pytest.fixture
def fake_jobspy(tmp_path, monkeypatch):
    """A fake jobspy/curl_cffi pair the real worker imports in a real subprocess."""
    (tmp_path / 'curl_cffi').mkdir()
    (tmp_path / 'curl_cffi' / '__init__.py').write_text('')
    (tmp_path / 'curl_cffi' / 'requests.py').write_text('class Session:\n    def request(self, *a, **k):\n        return None\n')
    (tmp_path / 'jobspy').mkdir()
    (tmp_path / 'jobspy' / '__init__.py').write_text('''
import json, os, time, math
class Frame:
    def __init__(self, rows): self.rows, self.attrs = rows, {"note": "fake"}
    def to_dict(self, orient): return self.rows
def scrape_jobs(**args):
    open(os.environ["FAKE_JOBSPY_ARGS"], "w").write(json.dumps(args))
    mode = os.environ.get("FAKE_JOBSPY_MODE", "ok")
    if mode == "sleep": time.sleep(60)
    if mode == "error": raise RuntimeError("HTTP 403 Forbidden")
    import requests; requests.Session().request("GET", "https://example.invalid/x") if False else None
    return Frame([{"id": "1", "job_url": "https://www.indeed.com/viewjob?jk=1", "title": "T", "company": "C",
                   "description": "D", "min_amount": float("nan"), "salary_source": None, "is_remote": None}])
''')
    monkeypatch.setenv('PYTHONPATH', str(tmp_path))
    monkeypatch.setenv('FAKE_JOBSPY_ARGS', str(tmp_path / 'args.json'))
    return tmp_path


def test_worker_cleans_values_and_maps_supported_filters(fake_jobspy):
    outcome = js.run_site(config(), 'indeed', 'python', 'New York, NY',
                          {'posted': 'week', 'work_type': 'remote', 'employment_type': 'full-time', 'experience_level': 'entry'})
    assert outcome['status'] == 'ok' and outcome['rows'][0]['min_amount'] is None
    sent = json.loads((fake_jobspy / 'args.json').read_text())
    assert (sent['hours_old'], sent['is_remote'], sent['job_type']) == (168, True, 'fulltime')
    assert outcome['unapplied_filters'] == ['experience_level'] and outcome['attrs'] == {'note': 'fake'}


def test_blocked_errors_are_classified(fake_jobspy, monkeypatch):
    monkeypatch.setenv('FAKE_JOBSPY_MODE', 'error')
    assert js.run_site(config(), 'indeed', 'python', None, {})['status'] == 'blocked'


@pytest.mark.parametrize('reason', ['timeout', 'cancelled'])
def test_timeout_and_cancellation_kill_the_worker(fake_jobspy, monkeypatch, reason):
    monkeypatch.setenv('FAKE_JOBSPY_MODE', 'sleep')
    started = time.monotonic()
    stop_at = started + 2
    outcome = js.run_site(config(timeout_seconds=2 if reason == 'timeout' else 60), 'indeed', 'python', None, {},
                          cancelled=(lambda: time.monotonic() > stop_at) if reason == 'cancelled' else (lambda: False))
    assert outcome['status'] == reason and outcome['rows'] == [] and time.monotonic() - started < 15


def test_jobspy_search_through_the_career_harness(career, monkeypatch):
    from test_career_flow import execute
    c = career
    observed = c.harness.observe(c.repository.get(c.state['task_id']))
    assert 'job_sources' not in observed  # disabled: no extra model-visible metadata
    off = execute(c, 'search_jobs', query='Python developer', extra_sources=['indeed'])
    assert off['jobspy']['status'] == 'disabled'
    monkeypatch.setattr(js.JobSpyConfig, 'from_env', classmethod(lambda cls: config(sites=('indeed',))))
    card, text = js.normalize('indeed', {**ROW, 'job_url': 'https://www.indeed.com/viewjob?jk=77'})
    calls = []
    def fake_search(cfg, query, location=None, *, sites=None, filters=None, requests_used=0, cancelled=lambda: False):
        calls.append((query, sites, requests_used))
        return {'jobs': [card], 'descriptions': {card['url']: text}, 'requests': 1, 'status': 'ok',
                'sources': {'indeed': {'status': 'ok', 'requests': 1}}}
    monkeypatch.setattr(js, 'search', fake_search)
    found = execute(c, 'search_jobs', query='Python', extra_sources=['indeed'])
    assert any(j['url'] == card['url'] for j in found['jobs']) and found['jobspy']['network_requests'] == 1
    assert c.harness.observe(c.repository.get(c.state['task_id']))['job_sources']['jobspy']['requests_used_in_task'] == 1
    original_job = c.harness.discovery.job
    c.harness.discovery.job = lambda url: pytest.fail('stored description must be reused')
    try:
        read = execute(c, 'read_job', url=card['url'])
    finally:
        c.harness.discovery.job = original_job
    assert 'Build Python services.' in json.dumps(read)
    execute(c, 'search_jobs', query='Python services', extra_sources=['indeed'])
    assert len(calls) == 2 and calls[-1][2] == 1  # the task's earlier JobSpy requests count against its budget
