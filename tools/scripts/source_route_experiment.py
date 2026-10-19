"""JobSpy independent comparison, layer 1: explicit source routes, zero model calls (spec 11.3-11.4).

Two mutually exclusive routes, an experiment configuration only (product defaults unchanged):
  A  original: company board APIs from the verified directory + LinkedIn MCP.
  B  JobSpy:   JobSpy Indeed + JobSpy LinkedIn (counted separately).
Neither route may use browser search, public search engines, the other route, or any
fallback. Collection goes through the adapters' deterministic entry points
(JobDiscovery.search_directory / .job, LinkedInMcpSource.search / .details,
jobspy_source.search), never through search_jobs, which screens cards with a model.

Guards, in every mode:
  - a provider guard refuses and counts any model call (a refused hidden call is a failure,
    not a success: the run is marked invalid);
  - board HTTP goes through a host allowlist transport; any other host is refused and counted;
  - offline mode additionally blocks sockets and subprocesses and uses synthetic fixtures at
    the adapter boundary (HTTP transport, MCP call, JobSpy site runner), so the real parsing,
    budget and metrics code runs with zero network.

Usage:
  source_route_experiment.py manifest OUT.json
  source_route_experiment.py precheck --out DIR            # offline, both routes, proves 0 network / 0 model
  source_route_experiment.py collect --route A|B --out DIR # ONLINE: needs separate approval
"""
import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'tools' / 'scripts')]

MANIFEST = {
    'experiment': 'source-routes-20261018',
    'layer': 1,
    'product_defaults_changed': False,
    'routes': {
        'A': {'name': 'original', 'allowed_sources': ['boards', 'linkedin_mcp'],
              'forbidden': ['jobspy', 'browser_search', 'public_search_engines', 'cross_route_fallback']},
        'B': {'name': 'jobspy', 'allowed_sources': ['jobspy_indeed', 'jobspy_linkedin'],
              'forbidden': ['boards', 'linkedin_mcp', 'browser_search', 'public_search_engines', 'cross_route_fallback']},
    },
    # Pre-registered from the D2 intents; same text, place and window for both routes. Filters a
    # source cannot apply are reported as unapplied, never treated as applied.
    'queries': [
        {'id': 'backend-python', 'query': 'backend python software engineer', 'location': 'New York, NY',
         'filters': {'experience_level': 'entry', 'posted': 'week'}},
        {'id': 'junior-data', 'query': 'junior data engineer', 'location': 'New York, NY',
         'filters': {'experience_level': 'entry', 'posted': 'week'}},
    ],
    'sample_cap_per_route_query': 20,
    'caps': {
        'model_calls': 0,
        'route_seconds': 900,
        'boards': {'directory_fanout': 40, 'detail_reads_per_query': 20, 'allowed_hosts': [
            # Company board pages are the same source as their APIs (a Greenhouse board may publish
            # boards.greenhouse.io/embed links); run 01 refused 2 such detail reads.
            'boards-api.greenhouse.io', 'boards.greenhouse.io', 'job-boards.greenhouse.io', 'api.lever.co', 'jobs.lever.co',
            'api.ashbyhq.com', 'jobs.ashbyhq.com', '*.myworkdayjobs.com']},
        'linkedin_mcp': {'search_pages': 2, 'detail_calls_per_query': 20, 'calls_per_route': 44, 'call_timeout_s': 120},
        'jobspy': {'results_per_site': 20, 'timeout_s_per_site': 150, 'requests_per_route': 60},
    },
    'order': 'A then B on day 1; approval may alternate the order on a repeat. Timestamps are recorded.',
    'data_sent': 'Query text, location and the filters above to the listed sources only. No resume or profile data.',
    'stop_conditions': ['any model call attempt', 'any host outside the route allowlist', 'source blocked / rate limited',
                        'per-source cap reached (reported as capped)', 'route_seconds deadline'],
    'outputs': 'snapshot-<route>.json per route (frozen for layer 2) and metrics.json',
}

FULL_JD = 500


class ModelCallForbidden(BaseException):
    pass


class Guards:
    """Counts what each guard refused. Any non-zero count makes the run invalid."""

    def __init__(self, offline):
        self.offline = offline
        self.model_attempts, self.refused_hosts, self.sockets, self.subprocesses = 0, [], 0, 0
        self.http = {}
        self._saved = []

    def provider_guard(self):
        self.model_attempts += 1
        raise ModelCallForbidden('layer 1 makes no model calls')

    def install(self):
        if not self.offline:
            return
        guards = self
        def refuse_socket(*args, **kwargs):
            guards.sockets += 1
            raise OSError('offline precheck: network is blocked')
        def refuse_popen(*args, **kwargs):
            guards.subprocesses += 1
            raise OSError('offline precheck: subprocesses are blocked')
        for owner, name, replacement in ((socket.socket, 'connect', refuse_socket),
                                         (socket, 'create_connection', refuse_socket),
                                         (socket, 'getaddrinfo', refuse_socket),
                                         (subprocess, 'Popen', refuse_popen)):
            self._saved.append((owner, name, getattr(owner, name)))
            setattr(owner, name, replacement)

    def restore(self):
        for owner, name, original in reversed(self._saved):
            setattr(owner, name, original)
        self._saved.clear()

    def report(self):
        return {'model_attempts': self.model_attempts, 'refused_hosts': self.refused_hosts,
                'blocked_sockets': self.sockets, 'blocked_subprocesses': self.subprocesses, 'http_by_host': self.http,
                'valid': not (self.model_attempts or self.refused_hosts or self.sockets or self.subprocesses)}


def host_allowed(host):
    return any(host == h or (h.startswith('*.') and host.endswith(h[1:])) for h in MANIFEST['caps']['boards']['allowed_hosts'])


def allowlist_transport(inner, guards):
    import httpx

    class Allowlist(httpx.BaseTransport):
        def handle_request(self, request):
            host = request.url.host
            if not host_allowed(host):
                guards.refused_hosts.append(host)
                raise httpx.ConnectError(f'host {host} is outside route A', request=request)
            guards.http[host] = guards.http.get(host, 0) + 1
            return inner.handle_request(request)
    return Allowlist()


# ---- synthetic offline fixtures at the adapter boundary --------------------------------------

def offline_board_transport():
    import httpx
    text = ('Responsibilities: build backend Python services and data pipelines. ' * 12).strip()

    def handler(request):
        url, host = str(request.url), request.url.host
        if host == 'boards-api.greenhouse.io':
            board = request.url.path.split('/')[3]
            if request.url.path.endswith('/jobs'):
                return httpx.Response(200, json={'jobs': [
                    {'id': 101, 'title': 'Backend Python Software Engineer', 'absolute_url': f'https://job-boards.greenhouse.io/{board}/jobs/101',
                     'location': {'name': 'New York, NY'}},
                    {'id': 102, 'title': 'Junior Data Engineer', 'absolute_url': f'https://job-boards.greenhouse.io/{board}/jobs/102',
                     'location': {'name': 'New York, NY'}}]})
            return httpx.Response(200, json={'title': 'Backend Python Software Engineer', 'content': f'<p>{text}</p>',
                                             'location': {'name': 'New York, NY'}, 'absolute_url': url})
        if host == 'api.lever.co':
            return httpx.Response(200, json=[])
        if host == 'api.ashbyhq.com':
            return httpx.Response(200, json={'jobs': []})
        if host.endswith('.myworkdayjobs.com'):
            return httpx.Response(200, json={'jobPostings': []})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


class _Result:
    def __init__(self, payload):
        self.structured_content, self.content, self.is_error = payload, [], False


def offline_linkedin_call(server, tool, arguments, timeout_seconds=None):
    if tool == 'search_jobs':
        ids = ['4400000001', '4400000002', '4400000003']
        refs = [{'kind': 'job', 'text': f'Software Engineer {i}', 'url': f'https://www.linkedin.com/jobs/view/{j}/'}
                for i, j in enumerate(ids)]
        lines = '\n'.join(f'Software Engineer {i}\nExample Co {i}\nNew York, NY' for i in range(len(ids)))
        return _Result({'job_ids': ids, 'references': {'search_results': refs}, 'sections': {'search_results': lines},
                        'total': {'count': 3, 'exact': True}})
    if tool == 'get_job_details':
        job_id = arguments['job_id']
        body = 'Software Engineer\nExample Co\nNew York, NY\nAbout the job\n' + ('Build Python APIs and data services. ' * 30)
        return _Result({'url': f'https://www.linkedin.com/jobs/view/{job_id}/', 'sections': {'job_posting': body},
                        'apply': {'type': 'external', 'url': 'https://example.com/apply'}})
    raise RuntimeError(f'offline fixture has no {tool}')


def offline_jobspy_runner(config, site, query, location, filters, *, cancelled=lambda: False):
    applied = {k: v for k, v in (filters or {}).items() if k == 'posted'}
    rows = [{'id': f'{site}-{i}', 'job_url': f'https://{"www.indeed.com/viewjob?jk=" if site == "indeed" else "www.linkedin.com/jobs/view/"}{4500000000 + i}',
             'job_url_direct': 'https://example.com/apply' if site == 'indeed' else None, 'title': f'Data Engineer {i}',
             'company': '' if i == 2 else f'Example {i}', 'location': 'New York, NY', 'date_posted': '2026-10-15',
             'description': ('Design data pipelines in Python and SQL. ' * 20) if i != 1 else ''} for i in range(3)]
    return {'site': site, 'status': 'ok', 'rows': rows, 'elapsed_s': 0.0, 'requests': 1, 'requests_observed': True,
            'warnings': [], 'attrs': {}, 'error': None, 'applied_filters': applied,
            'unapplied_filters': [k for k in (filters or {}) if k not in applied]}


# ---- collection -------------------------------------------------------------------------------

def jd_class(text, origin):
    if not text:
        return 'missing'
    return f'{origin}_full' if len(text) >= FULL_JD else f'{origin}_short'


def collect_route_a(query, guards, *, offline, li_store, mcp_call):
    import httpx
    from eregion.runtime.career.discovery import JobDiscovery
    from eregion.runtime.career.board_directory import select_boards, DIRECTORY
    from eregion.runtime.career.linkedin_source import LinkedInMcpSource, LinkedInSourceStopped
    caps, cap = MANIFEST['caps'], MANIFEST['sample_cap_per_route_query']
    filters = query['filters']
    out = {}
    # Boards: the verified directory snapshot is recorded before any request.
    boards, stale = select_boards(query['query'], query['location'])
    inner = offline_board_transport() if offline else httpx.HTTPTransport()
    # The SSRF check resolves each host; offline it must not reach DNS (the default resolver was
    # bound at import time, so patching socket alone would not stop it).
    resolver = (lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))]) if offline else socket.getaddrinfo
    discovery = JobDiscovery(transport=allowlist_transport(inner, guards), resolver=resolver)
    if offline:
        discovery.min_interval = 0  # the per-host courtesy delay matters only for real hosts
    before = sum(guards.http.values())
    started = time.monotonic()
    try:
        found = discovery.search_directory(query['query'], query['location'], limit=cap)
        cards = found['jobs'][:cap]
        discover_s = round(time.monotonic() - started, 2)
        detail_started, details = time.monotonic(), 0
        for card in cards[:caps['boards']['detail_reads_per_query']]:
            details += 1
            try:
                job = discovery.job(card['url'])
                card['description'], card['jd'] = job.get('description') or '', jd_class(job.get('description'), 'enriched')
            except Exception as exc:
                card['description'], card['jd'], card['detail_error'] = '', 'missing', type(exc).__name__
        out['boards'] = {'status': 'ok' if cards else 'empty', 'rows': cards, 'discover_s': discover_s,
                         'first_card_s': discover_s if cards else None, 'detail_s': round(time.monotonic() - detail_started, 2),
                         'detail_reads': details, 'requests': sum(guards.http.values()) - before,
                         'errors': found.get('errors', [])[:20],
                         'directory': {'file_sha256': hashlib.sha256(Path(DIRECTORY).read_bytes()).hexdigest(),
                                       'selected': len(boards), 'stale': len(stale), 'searched': found.get('directory_searched', [])},
                         'applied_filters': {}, 'unapplied_filters': sorted(filters)}
    finally:
        discovery.close()
    # LinkedIn MCP: search pages then details, inside the route's call budget.
    source = LinkedInMcpSource(mcp_call, store=li_store, call_budget=caps['linkedin_mcp']['calls_per_route'],
                               deadline_seconds=caps['linkedin_mcp']['call_timeout_s'], max_pages=caps['linkedin_mcp']['search_pages'])
    calls_before = li_store.get('calls_used', 0)
    started = time.monotonic()
    status, cards, unapplied, error = 'ok', [], [], None
    try:
        level = filters.get('experience_level')
        result = source.search(query['query'], query['location'], experience_level=level,
                               max_pages=caps['linkedin_mcp']['search_pages'])
        status, cards = result['status'], result['jobs'][:cap]
        unapplied = sorted(set(result.get('unapplied_filters', [])) | (set(filters) - {'experience_level'}))
    except LinkedInSourceStopped as exc:
        status, error = 'stopped', str(exc)
    except Exception as exc:
        status, error = 'failed', f'{type(exc).__name__}: {str(exc)[:200]}'
    discover_s = round(time.monotonic() - started, 2)
    detail_started = time.monotonic()
    for card in cards[:caps['linkedin_mcp']['detail_calls_per_query']]:
        try:
            detail = source.details(card['provider_job_id'])
            card.update(title=card.get('title') or detail.get('title'), company=card.get('company') or detail.get('company'))
            card['description'], card['jd'] = detail.get('description') or '', jd_class(detail.get('description'), 'enriched')
            card['detail_status'], card['apply_status'] = detail['status'], detail.get('apply_status')
        except Exception as exc:
            card['description'], card['jd'], card['detail_error'] = '', 'missing', type(exc).__name__
            if isinstance(exc, LinkedInSourceStopped):
                break
    out['linkedin_mcp'] = {'status': status, 'error': error, 'rows': cards, 'discover_s': discover_s,
                           'first_card_s': discover_s if cards else None, 'detail_s': round(time.monotonic() - detail_started, 2),
                           'requests': li_store.get('calls_used', 0) - calls_before,
                           'calls_used_in_route': li_store.get('calls_used'), 'stop_reason': li_store.get('stopped'),
                           'applied_filters': {k: v for k, v in filters.items() if k == 'experience_level' and k not in unapplied},
                           'unapplied_filters': unapplied}
    return out


def collect_route_b(query, guards, *, offline, used):
    from eregion.runtime.career import jobspy_source as js
    caps, cap = MANIFEST['caps']['jobspy'], MANIFEST['sample_cap_per_route_query']
    config = js.JobSpyConfig(python=os.getenv('JOB_AGENT_JOBSPY_PYTHON'),
                             sites=('indeed', 'linkedin'), results_per_site=caps['results_per_site'],
                             timeout_seconds=caps['timeout_s_per_site'], max_requests_per_task=caps['requests_per_route'])
    if not config.enabled:
        raise SystemExit('JOB_AGENT_JOBSPY_PYTHON is required for route B online.')
    original = js.run_site
    if offline:
        js.run_site = offline_jobspy_runner
    out = {}
    try:
        for site in ('indeed', 'linkedin'):
            started = time.monotonic()
            found = js.search(config, query['query'], query['location'], sites=[site], filters=query['filters'],
                              requests_used=used[0])
            used[0] += found['requests']
            source = found['sources'].get(site, {})
            rows = []
            for card in found['jobs'][:cap]:
                text = found['descriptions'].get(card['url'], '')
                rows.append({**card, 'description': text, 'jd': jd_class(text, 'native')})
            elapsed = round(time.monotonic() - started, 2)
            out[f'jobspy_{site}'] = {'status': source.get('status'), 'error': source.get('error'), 'rows': rows,
                                     'discover_s': elapsed, 'first_card_s': elapsed if rows else None, 'detail_s': 0.0,
                                     'detail_note': 'No other source is allowed in route B; descriptions are JobSpy-native only.',
                                     'requests': source.get('requests'), 'warnings': source.get('warnings'),
                                     'applied_filters': source.get('applied_filters', {}),
                                     'unapplied_filters': source.get('unapplied_filters', [])}
    finally:
        js.run_site = original
    return out


def source_metrics(rows):
    from eregion.runtime.career.alerts import job_key
    def key(row):
        try:
            return job_key(row.get('url') or '')
        except ValueError:
            return None
    valid = [r for r in rows if r.get('url') and r.get('title') and r.get('company')]
    jd = {}
    for r in rows:
        jd[r.get('jd', 'missing')] = jd.get(r.get('jd', 'missing'), 0) + 1
    return {'returned': len(rows), 'deduped': len({key(r) for r in rows if key(r)}), 'valid': len(valid),
            'missing_company': sum(1 for r in rows if not r.get('company')), 'jd': jd,
            'direct_apply_url': sum(1 for r in rows if r.get('apply_url')),
            'posted_known': sum(1 for r in rows if r.get('posted')), 'keys': sorted({key(r) for r in valid if key(r)})}


def compare(snapshots):
    """Per source and per route; uniqueness is by canonical posting identity across routes.
    Same title+company is reported as possible overlap only."""
    clean = lambda t: re.sub(r'\W+', ' ', (t or '').casefold()).strip()
    report = {}
    for route, snap in snapshots.items():
        for query in snap['queries']:
            for name, src in query['sources'].items():
                report.setdefault(route, {}).setdefault(query['id'], {})[name] = {
                    **{k: v for k, v in source_metrics(src['rows']).items() if k != 'keys'},
                    **{k: src.get(k) for k in ('status', 'discover_s', 'first_card_s', 'detail_s', 'requests',
                                               'calls_used_in_route', 'detail_reads', 'applied_filters', 'unapplied_filters')}}
    if len(snapshots) == 2:
        (ra, sa), (rb, sb) = snapshots.items()
        for qa, qb in zip(sa['queries'], sb['queries']):
            ka = {k for s in qa['sources'].values() for k in source_metrics(s['rows'])['keys']}
            kb = {k for s in qb['sources'].values() for k in source_metrics(s['rows'])['keys']}
            ta = {(clean(r['title']), clean(r['company'])) for s in qa['sources'].values() for r in s['rows'] if r.get('company')}
            tb = {(clean(r['title']), clean(r['company'])) for s in qb['sources'].values() for r in s['rows'] if r.get('company')}
            report.setdefault('cross_route', {})[qa['id']] = {f'unique_{ra}': len(ka - kb), f'unique_{rb}': len(kb - ka),
                'same_identity': len(ka & kb), 'possible_overlap_title_company': len(ta & tb)}
    return report


def collect(route, out, *, offline):
    from eregion.runtime.provider_usage import usage_scope
    guards = Guards(offline)
    guards.install()
    manager = None
    snapshot = {'route': route, 'offline': offline, 'manifest_sha256': hashlib.sha256(json.dumps(MANIFEST, sort_keys=True).encode()).hexdigest(),
                'started_at': datetime.now(UTC).isoformat(), 'queries': []}
    try:
        with usage_scope(provider_guard=guards.provider_guard, purpose='source_route_layer1'):
            if route == 'A':
                if offline:
                    call = offline_linkedin_call
                else:
                    from eregion.runtime.mcp.config import load_mcp_runtime_config
                    from eregion.runtime.mcp.manager import McpToolManager
                    from eregion.runtime.registry import ToolRegistry
                    manager = McpToolManager(config=load_mcp_runtime_config(), registry=ToolRegistry())
                    manager.start()
                    call = manager.call_tool
                store = {}
                for query in MANIFEST['queries']:
                    snapshot['queries'].append({**query, 'at': datetime.now(UTC).isoformat(),
                        'sources': collect_route_a(query, guards, offline=offline, li_store=store, mcp_call=call)})
                snapshot['linkedin_mcp_log'] = store.get('log', [])
            else:
                used = [0]
                for query in MANIFEST['queries']:
                    snapshot['queries'].append({**query, 'at': datetime.now(UTC).isoformat(),
                        'sources': collect_route_b(query, guards, offline=offline, used=used)})
                snapshot['jobspy_requests_in_route'] = used[0]
    finally:
        if manager is not None:
            manager.stop()
        guards.restore()
    snapshot['finished_at'] = datetime.now(UTC).isoformat()
    snapshot['guards'] = guards.report()
    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / f'snapshot-{route}.json').write_text(json.dumps(snapshot, indent=1, ensure_ascii=False, default=str), encoding='utf-8')
    return snapshot


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    m = sub.add_parser('manifest'); m.add_argument('path')
    p = sub.add_parser('precheck'); p.add_argument('--out', required=True)
    c = sub.add_parser('collect'); c.add_argument('--route', choices=['A', 'B'], required=True); c.add_argument('--out', required=True)
    args = parser.parse_args()
    if args.command == 'manifest':
        Path(args.path).write_text(json.dumps(MANIFEST, indent=1, ensure_ascii=False), encoding='utf-8')
        print(args.path)
        return
    from run_guard import guarded, require
    out = Path(args.out)
    with guarded(out, deadline_s=MANIFEST['caps']['route_seconds'] * (2 if args.command == 'precheck' else 1)) as run:
        if args.command == 'precheck':
            # Offline: a fake JobSpy interpreter path; real one is never started.
            # Offline: an existing interpreter path satisfies the config; Popen is blocked and the site runner is a fixture.
            os.environ['JOB_AGENT_JOBSPY_PYTHON'] = sys.executable
            snapshots = {route: collect(route, out, offline=True) for route in ('A', 'B')}
            metrics = compare(snapshots)
            guards = {route: s['guards'] for route, s in snapshots.items()}
            (out / 'metrics.json').write_text(json.dumps(metrics, indent=1, ensure_ascii=False), encoding='utf-8')
            run['result'] = {'guards': guards, 'metrics_written': True}
            require(all(g['valid'] for g in guards.values()), f'offline precheck touched network or a model: {guards}')
            require(all(g['model_attempts'] == 0 and g['blocked_sockets'] == 0 and g['blocked_subprocesses'] == 0
                        for g in guards.values()), 'zero-call proof failed')
            print(json.dumps({'precheck': 'passed', 'guards': guards}, ensure_ascii=False))
        else:
            snapshot = collect(args.route, out, offline=False)
            run['result'] = {'guards': snapshot['guards']}
            require(snapshot['guards']['valid'], f"route {args.route} run is invalid: {snapshot['guards']}")


if __name__ == '__main__':
    main()
