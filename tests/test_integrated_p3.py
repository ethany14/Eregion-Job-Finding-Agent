"""P3 candidate batches + API-first routing. Temp DB, no model."""
from types import SimpleNamespace

from test_career_flow import career, execute  # noqa: F401
from test_remaining_search_completion import URL, prepare

class Discovery:
    def __init__(self, directory=(), job=None):
        self.directory, self._job, self.calls = list(directory), job, []

    def search_directory(self, query, location='', limit=10, *, cancelled=None, **_):
        self.calls.append(('directory', query))
        return {'jobs': list(self.directory), 'errors': [], 'sources_checked': [], 'skipped_sources': [], 'board_outcomes': []}

    def search(self, query, location='', boards=None, limit=10, excluded_hosts=(), excluded_sources=()):
        self.calls.append(('public', query))
        return {'jobs': [], 'errors': [], 'sources_checked': [], 'skipped_sources': []}

    def job(self, url):
        self.calls.append(('job', url))
        return self._job or {'url': url, 'title': 'Data Analyst', 'company': 'Synthetic', 'location': 'New York, NY',
                             'description': 'Requirements: 0-2 years of marketing or analytics experience. SQL.'}


def ask(c, text):
    c.repository.append(c.state['task_id'], role='user', text=text)


# --- routing ------------------------------------------------------------------------------

def test_flags_off_keep_the_old_search_route(career, monkeypatch):
    c = career
    prepare(c)
    c.harness.discovery = Discovery()
    execute(c, 'search_jobs', query='junior data analyst', location='New York, NY')
    assert c.harness.discovery.calls == [('public', 'junior data analyst')]


def test_api_first_uses_the_directory(career, monkeypatch):
    monkeypatch.setenv('JOB_AGENT_JOB_API_FIRST', '1')
    c = career
    prepare(c)
    c.harness.discovery = Discovery()
    result = execute(c, 'search_jobs', query='junior data analyst', location='New York, NY')
    assert c.harness.discovery.calls == [('directory', 'junior data analyst')]
    assert result['linkedin_mcp']['error_code'] == 'linkedin_mcp_unavailable'


def test_linkedin_only_never_traverses_other_boards(career, monkeypatch):
    monkeypatch.setenv('JOB_AGENT_JOB_API_FIRST', '1')
    c = career
    prepare(c)
    ask(c, 'Find junior data analyst jobs only on LinkedIn.')
    c.harness.discovery = Discovery()
    result = execute(c, 'search_jobs', query='junior data analyst', location='New York, NY')
    assert result['error_code'] == 'linkedin_mcp_unavailable' and c.harness.discovery.calls == []


# --- reading ------------------------------------------------------------------------------

def test_api_first_does_not_force_browser_for_an_already_open_page(career, monkeypatch):
    monkeypatch.setenv('JOB_AGENT_JOB_API_FIRST', '1')
    c = career
    prepare(c)
    browser_calls = []
    c.harness._browsing = SimpleNamespace(inspect=lambda _t: {'url': URL, 'title': 'open'},
        call=lambda *a, **k: browser_calls.append(a) or {}, close=lambda: None)
    c.harness.discovery = Discovery()
    job = execute(c, 'read_job', url=URL)
    assert job['description'] and browser_calls == [] and ('job', URL) in c.harness.discovery.calls


# --- candidate batches --------------------------------------------------------------------

def test_candidate_gate_finishes_started_work_before_more_discovery(career, monkeypatch):
    monkeypatch.setenv('JOB_AGENT_OPT_CANDIDATE_BATCHES', '1')
    c = career
    prepare(c)
    c.harness.discovery = Discovery()
    execute(c, 'read_job', url=URL)  # read, not imported
    blocked = execute(c, 'search_jobs', query='junior data analyst', location='New York, NY')
    assert blocked['error_code'] == 'candidates_in_progress' and blocked['in_progress'][0]['stage'] == 'import'
    job = execute(c, 'import_job', url=URL)
    blocked = execute(c, 'search_jobs', query='junior data analyst', location='New York, NY')
    assert blocked['error_code'] == 'candidates_in_progress' and blocked['in_progress'][0]['stage'] == 'score'
    execute(c, 'score_job', application_id=job['application_id'])
    blocked = execute(c, 'search_jobs', query='junior data analyst', location='New York, NY')
    assert blocked['error_code'] == 'pending_decisions'  # the decision gate takes over
    execute(c, 'decide_job', application_id=job['application_id'], decision='exclude',
            reason='Requires on-site work in another city than requested.')
    assert 'error_code' not in execute(c, 'search_jobs', query='junior data analyst', location='New York, NY')


def test_candidate_view_bounds_detail_and_reports_target(career, monkeypatch):
    monkeypatch.setenv('JOB_AGENT_OPT_CANDIDATE_BATCHES', '1')
    c = career
    prepare(c)
    from eregion.runtime.career.candidate_queue import DETAIL_LIMIT
    for count in (12, 30):
        candidates = {f'k{i}': {'url': f'https://boards.greenhouse.io/acme/jobs/{100 + i}', 'title': f'Data Analyst {i}'}
                      for i in range(count)}
        c.repository.change(c.state['task_id'], search_candidates=candidates)
        view = c.harness.observe(c.repository.get(c.state['task_id']))['candidate_queue']
        # Bounded detail, and every candidate beyond it is reported as deferred, never dropped.
        assert len(view['next_unread']) == min(count, DETAIL_LIMIT) and not view['target_reached']
        assert view['deferred_unread'] == count - min(count, DETAIL_LIMIT)
