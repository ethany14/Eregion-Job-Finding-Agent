"""Layer-1 source capability metrics (offline)."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('cap', Path(__file__).parents[1] / 'tools/scripts/job_source_capability.py')
cap = importlib.util.module_from_spec(spec); spec.loader.exec_module(cap)


def test_metrics_separate_identity_uniqueness_title_overlap_and_description_coverage():
    long = 'x' * 600
    data = {'query': 'backend NY', 'sources': {
        'indeed': {'rows': [{'job_url': 'https://www.indeed.com/viewjob?jk=1', 'title': 'Backend Engineer', 'company': 'Acme', 'description': long,
                             'job_url_direct': 'https://acme.example/1'},
                            {'job_url': 'https://www.indeed.com/viewjob?jk=2', 'title': 'Data Engineer', 'company': 'Beta', 'description': ''},
                            {'job_url': '', 'title': 'No URL', 'company': 'C'}],
                   'requests': 1, 'elapsed_s': 2.0, 'applied_filters': {'posted': 'week'}, 'unapplied_filters': ['experience_level']},
        'linkedin_mcp': {'rows': [{'url': 'https://www.linkedin.com/jobs/view/9/', 'title': 'Backend Engineer', 'company': 'ACME', 'description': long}],
                         'requests': 3, 'elapsed_s': 10.0}}}
    report = cap.metrics(data)['sources']
    assert report['indeed']['returned'] == 3 and report['indeed']['valid'] == 2
    assert report['indeed']['unique_by_identity'] == 2  # different sites never merge by identity
    assert report['indeed']['shared_title_company_with_others'] == 1  # reported, not merged
    assert (report['indeed']['full_description'], report['indeed']['missing_description']) == (1, 1)
    assert report['indeed']['direct_apply_url'] == 1 and report['indeed']['unapplied_filters'] == ['experience_level']
    assert report['linkedin_mcp']['requests'] == 3


def test_saved_probe_rows_can_be_measured_without_requests():
    probe = Path(__file__).parents[1] / '.codex-runs/jobspy-probe/rows-indeed-python_backend_engineer.json'
    if not probe.exists():
        import pytest; pytest.skip('local probe evidence not present')
    import json
    rows = json.loads(probe.read_text(encoding='utf-8'))
    report = cap.metrics({'query': 'python backend engineer / New York, NY', 'sources': {'indeed': {'rows': rows, 'requests': 1}}})
    source = report['sources']['indeed']
    assert source['returned'] == len(rows) and source['valid'] == len(rows) - source['missing_company']
