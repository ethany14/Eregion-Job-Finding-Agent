"""R6 offline source-chain coverage for the API-first route (gaps not covered by P3).

Covered elsewhere and reused as-is: normal search, no results, paging/dedupe, missing JD,
timeouts and network errors, disabled API (tests/test_integrated_p3.py, tests/test_board_directory.py).
"""
from test_career_flow import career, execute  # noqa: F401
from test_integrated_p3 import Discovery
from test_remaining_search_completion import prepare

GH = {'url': 'https://job-boards.greenhouse.io/acme/jobs/1', 'title': 'Junior Data Analyst', 'company': 'Acme',
      'location': 'New York, NY'}


def test_api_first_uses_free_boards(career, monkeypatch):
    monkeypatch.setenv('JOB_AGENT_JOB_API_FIRST', '1')
    c = career; prepare(c)
    c.harness.discovery = Discovery(directory=[{'url': 'https://boards.greenhouse.io/x/jobs/1', 'title': 'Data Analyst'}])
    result = execute(c, 'search_jobs', query='junior data analyst', location='New York, NY')
    assert ('directory', 'junior data analyst') in c.harness.discovery.calls and 'error' not in result


def test_candidate_evidence_is_isolated_between_tasks(career, monkeypatch):
    monkeypatch.setenv('JOB_AGENT_JOB_API_FIRST', '1')
    c = career; prepare(c)
    c.repository.change(c.state['task_id'], search_candidates={'k': GH})
    c.harness.discovery = Discovery()
    execute(c, 'read_job', url=GH['url'])
    other, _ = c.repository.create('another user request', 'isolation-key')
    assert c.repository.get(c.state['task_id']).get('retrieved_jobs')
    assert not c.repository.get(other['task_id']).get('retrieved_jobs')
    assert not c.repository.get(other['task_id']).get('search_candidates')


def test_thin_directory_results_are_returned_without_a_paid_supplement(career, monkeypatch):
    monkeypatch.setenv('JOB_AGENT_JOB_API_FIRST', '1')
    c = career; prepare(c)
    c.harness.discovery = Discovery(directory=[GH])
    result = execute(c, 'search_jobs', query='junior data analyst', location='New York, NY', use_directory=True)
    assert [j['url'] for j in result['jobs']] == [GH['url']] and 'error' not in result


def test_one_invalid_board_url_does_not_fail_the_search(career, monkeypatch):
    # Run r6/run-1: every later search failed with "Use a public HTTPS job page ...".
    monkeypatch.setenv('JOB_AGENT_JOB_API_FIRST', '1')
    c = career; prepare(c)
    bad = {**GH, 'url': 'http://careers.example.com/jobs/7'}
    c.harness.discovery = Discovery(directory=[bad, GH])
    result = execute(c, 'search_jobs', query='junior data analyst', location='New York, NY', use_directory=True)
    assert [j['url'] for j in result['jobs']] == [GH['url']] and result['dropped_invalid_urls'] == 1
    assert 'error' not in result


def test_directory_dedupe_drops_an_invalid_board_url_instead_of_raising(monkeypatch):
    # The exact failure replayed from run-1 page 3: JobDiscovery.search_directory raised in job_key.
    from eregion.runtime.career import board_directory
    from eregion.runtime.career.discovery import JobDiscovery
    discovery = JobDiscovery()
    monkeypatch.setattr(board_directory, 'select_boards', lambda q, l: ([{'platform': 'greenhouse', 'company': 'acme'}], []))
    monkeypatch.setattr(discovery, 'search', lambda *a, **k: {'jobs': [
        {'url': 'http://careers.example.com/jobs/7', 'title': 'Data Analyst'}, GH], 'errors': [],
        'sources_checked': [], 'skipped_sources': [], 'board_outcomes': []})
    try:
        result = discovery.search_directory('junior data analyst', 'New York, NY', limit=20)
    finally:
        discovery.close()
    assert [j['url'] for j in result['jobs']] == [GH['url']] and result['dropped_invalid_urls'] == 1
