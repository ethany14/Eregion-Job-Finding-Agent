"""Optional JobSpy job search source (python-jobspy, MIT), run out of process.

JobSpy needs pandas/curl_cffi, which the server environment does not carry, so it runs under
a separate interpreter (JOB_AGENT_JOBSPY_PYTHON) in one bounded subprocess per site. Killing
the subprocess on timeout or cancellation really stops its network requests. Real HTTP
requests are counted inside the worker and recorded beside, not inside, model provider usage.

The source is off unless configured. It returns observed cards only: relevance, seniority
and personal qualification stay with the model. Library-inferred fields are labelled as such
and are never evidence of qualification."""
import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

SITES = ('indeed', 'linkedin')
POSTED_HOURS = {'24h': 24, 'week': 168, 'month': 720}
JOB_TYPES = {'full-time': 'fulltime', 'part-time': 'parttime', 'contract': 'contract', 'internship': 'internship'}

# Runs inside the JobSpy interpreter. Prints one JSON line: rows, request counts, warnings.
WORKER = r'''
import json, math, sys, time, warnings
counts = {"requests": 0, "by_host": {}}
def wrap(cls):
    original = cls.request
    def request(self, method, url, *a, **k):
        counts["requests"] += 1
        host = url.split("/")[2] if "://" in url else "?"
        counts["by_host"][host] = counts["by_host"].get(host, 0) + 1
        return original(self, method, url, *a, **k)
    cls.request = request
import requests
from curl_cffi import requests as curl_requests
wrap(requests.Session); wrap(curl_requests.Session)
from jobspy import scrape_jobs
args = json.loads(sys.argv[1])
caught, error, frame = [], None, None
with warnings.catch_warnings(record=True) as seen:
    warnings.simplefilter("always")
    try:
        frame = scrape_jobs(**args)
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
    caught = [str(w.message)[:300] for w in seen]
def clean(v):
    if v is None or (isinstance(v, float) and math.isnan(v)): return None
    if hasattr(v, "isoformat"):
        try: return v.isoformat()
        except Exception: return None
    if type(v).__name__ in {"NaTType", "NAType"}: return None
    return v if isinstance(v, (str, int, float, bool)) else str(v)
rows = [] if frame is None else [{k: clean(v) for k, v in r.items()} for r in frame.to_dict(orient="records")]
attrs = {} if frame is None else {str(k): str(v)[:500] for k, v in frame.attrs.items()}
print(json.dumps({"rows": rows, "requests": counts, "warnings": caught, "attrs": attrs, "error": error}))
'''


@dataclass(frozen=True)
class JobSpyConfig:
    python: str | None = None
    sites: tuple = ('indeed',)
    results_per_site: int = 20
    timeout_seconds: int = 60
    max_requests_per_task: int = 80

    @property
    def enabled(self):
        return bool(self.python) and Path(self.python).exists() and bool(self.sites)

    @classmethod
    def from_env(cls):
        sites = tuple(s.strip().casefold() for s in os.getenv('JOB_AGENT_JOBSPY_SITES', 'indeed').split(',')
                      if s.strip().casefold() in SITES)
        number = lambda name, default, low, high: max(low, min(high, int(os.getenv(name, default) or default)))
        return cls(python=(os.getenv('JOB_AGENT_JOBSPY_PYTHON') or '').strip() or None, sites=sites,
                   results_per_site=number('JOB_AGENT_JOBSPY_RESULTS', 20, 1, 50),
                   timeout_seconds=number('JOB_AGENT_JOBSPY_TIMEOUT', 60, 10, 300),
                   max_requests_per_task=number('JOB_AGENT_JOBSPY_MAX_REQUESTS', 80, 1, 1000))

    def describe(self):
        """Short model-visible capability metadata (what each site actually honours)."""
        return {'enabled': self.enabled, 'sites': list(self.sites) if self.enabled else [],
                'notes': {'indeed': 'One request per search; cards include the full description and direct apply URL.',
                          'linkedin': 'About one request per posting for descriptions; some descriptions can be missing; '
                                      'no direct apply URL.'},
                'limits': {'results_per_site': self.results_per_site, 'requests_per_task': self.max_requests_per_task}}


def run_site(config, site, query, location, filters, *, cancelled=lambda: False):
    """One site, one subprocess. Never raises for site failures: returns status and counts."""
    args = {'site_name': [site], 'search_term': query, 'results_wanted': config.results_per_site,
            'country_indeed': 'usa', 'description_format': 'markdown', 'verbose': 0,
            'linkedin_fetch_description': site == 'linkedin'}
    if location:
        args['location'] = location
    applied, unapplied = {}, []
    for key, value in (filters or {}).items():
        if key == 'posted' and value in POSTED_HOURS:
            args['hours_old'] = POSTED_HOURS[value]; applied[key] = value
        elif key == 'work_type' and value == 'remote':
            args['is_remote'] = True; applied[key] = value
        elif key == 'employment_type' and value in JOB_TYPES:
            args['job_type'] = JOB_TYPES[value]; applied[key] = value
        else:
            unapplied.append(key)  # e.g. experience_level: JobSpy has no such search parameter
    started = time.monotonic()
    process = subprocess.Popen([config.python, '-c', WORKER, json.dumps(args)], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
    status, output = 'ok', None
    while True:
        try:
            output, _ = process.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if cancelled():
                status = 'cancelled'
            elif time.monotonic() - started > config.timeout_seconds:
                status = 'timeout'
            else:
                continue
            process.kill()
            process.communicate()
            break
    elapsed = round(time.monotonic() - started, 1)
    data = {}
    if status == 'ok':
        try:
            data = json.loads((output or '').strip().splitlines()[-1])
        except (ValueError, IndexError):
            status = 'parse_failed'
    if status == 'ok' and data.get('error'):
        text = data['error'].casefold()
        status = 'blocked' if any(w in text for w in ('403', '429', 'blocked', 'captcha', 'rate')) else 'failed'
    rows = data.get('rows') or []
    if status == 'ok' and not rows:
        status = 'empty'  # an empty result is reported as such, never as "no jobs exist"
    return {'site': site, 'status': status, 'rows': rows, 'elapsed_s': elapsed,
            'requests': (data.get('requests') or {}).get('requests'), 'requests_observed': bool(data.get('requests')),
            'warnings': (data.get('warnings') or [])[:5], 'attrs': data.get('attrs') or {},
            'error': data.get('error'), 'applied_filters': applied, 'unapplied_filters': unapplied}


def _provenance(value, source):
    return {'value': value, 'provenance': source} if value not in (None, '', []) else {'value': None, 'provenance': 'unknown'}


def normalize(site, row):
    """A JobSpy row as an observed card plus its description (stored separately)."""
    description = (row.get('description') or '').strip()
    salary_origin = {'direct_data': 'source', 'description': 'library_inferred'}.get(row.get('salary_source'), 'unknown')
    location = row.get('location') or ', '.join(p for p in (row.get('city'), row.get('state')) if p)
    card = {
        'url': row.get('job_url'), 'apply_url': row.get('job_url_direct'), 'source': f'jobspy:{site}',
        'source_job_id': row.get('id'), 'title': row.get('title') or '', 'company': row.get('company') or '',
        'location': location or '', 'posted': row.get('date_posted'),
        'description_status': 'complete' if description else 'missing',
        'description_hash': hashlib.sha256(description.encode()).hexdigest()[:16] if description else None,
        'salary': {'min': row.get('min_amount'), 'max': row.get('max_amount'), 'interval': row.get('interval'),
                   'currency': row.get('currency'), 'provenance': salary_origin if row.get('min_amount') is not None else 'unknown'},
        # JobSpy derives remote status from text; level/type come from the site's own fields.
        'remote': _provenance(row.get('is_remote'), 'library_inferred'),
        'job_type': _provenance(row.get('job_type'), 'source'),
        'job_level': _provenance(row.get('job_level'), 'source'),
    }
    return card, description


def search(config, query, location=None, *, sites=None, filters=None, requests_used=0, cancelled=lambda: False):
    """Search the chosen sites in turn within the task's request budget. Partial results survive."""
    if not config.enabled:
        return {'jobs': [], 'descriptions': {}, 'sources': {}, 'requests': 0, 'status': 'disabled'}
    chosen = [s for s in (sites or config.sites) if s in config.sites]
    jobs, descriptions, sources, used = [], {}, {}, 0
    for site in chosen:
        if requests_used + used >= config.max_requests_per_task:
            sources[site] = {'status': 'budget_exhausted', 'requests': 0}
            continue
        if cancelled():
            sources[site] = {'status': 'cancelled', 'requests': 0}
            continue
        outcome = run_site(config, site, query, location, filters, cancelled=cancelled)
        used += outcome['requests'] or 0
        cards = []
        for row in outcome['rows']:
            if not row.get('job_url'):
                continue
            card, description = normalize(site, row)
            cards.append(card)
            if description:
                descriptions[card['url']] = description
        jobs.extend(cards)
        sources[site] = {k: outcome[k] for k in ('status', 'elapsed_s', 'requests', 'requests_observed', 'warnings',
                                                 'applied_filters', 'unapplied_filters', 'error')}
        sources[site]['rows'] = len(cards)
    return {'jobs': jobs, 'descriptions': descriptions, 'sources': sources, 'requests': used,
            'status': 'ok' if any(s.get('status') == 'ok' for s in sources.values()) else 'no_results'}
