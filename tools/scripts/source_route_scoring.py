"""JobSpy independent comparison, layer 2: qualification and recommendation on frozen layer-1 results (spec 11.5).

Each route's snapshot is scored in its own fresh database, analysis cache and ledger, with the same
model route, prompt, resume, scorer and caps. No network search: job sources are not configured
(no MCP, no JobSpy, no Treg), the board client refuses every host, and network tools are refused
with a structured error. A posting without a description stays unscorable; nothing is borrowed
from the other route.

Sample rule (same for both routes, fixed before any layer-2 result is seen): per query, the first
SAMPLE_PER_QUERY valid cards (url + title + company) taken round-robin across the route's sources in
snapshot order, deduplicated by posting identity within the route. Cards with a full description are imported from the frozen body;
the others are listed as included but unscorable ('missing_jd').

Usage:
  source_route_scoring.py build    --snapshot snapshot-A.json --out DIR   # offline, no model
  source_route_scoring.py dry      --snapshot snapshot-A.json --out DIR   # offline, scripted model: wiring proof
  source_route_scoring.py precheck --snapshot snapshot-A.json --out DIR   # builds the first real request, sends nothing
  source_route_scoring.py run      --snapshot snapshot-A.json --out DIR   # PAID: needs separate approval
"""
import argparse
import hashlib
import json
import os
import socket
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'tools' / 'scripts'), str(ROOT / 'tests')]

SAMPLE_PER_QUERY = 8
FULL_JD = 500
CAPS = {'provider_attempts': 40, 'input_tokens': 800000, 'seconds': 1800}
PROFILE_SOURCE = ROOT / '.codex-runs' / 'apply-test-07' / 'eregion-apply-test.sqlite3'
SYNTHETIC_PROFILE = {'first_name': 'Synthetic', 'last_name': 'Candidate', 'email': 'synthetic@example.com'}
INSTRUCTION = ('Assess the saved jobs in this conversation against my saved resume and recommend the best matches with '
               'the evidence for each, saying what is missing. Use only these saved jobs: do not search for new jobs, '
               'do not read other sources and do not apply to anything.')
NETWORK_TOOLS = {'search_jobs', 'read_job', 'screen_jobs', 'browser_open', 'browser_read', 'browser_search',
                 'browser_click', 'browser_type', 'start_application'}


def sample(snapshot):
    """Round-robin across the route's sources in snapshot order, so every source of the route is
    represented (taking the first source's cards first would compare one source per route)."""
    from itertools import zip_longest
    from eregion.runtime.career.alerts import job_key
    included, excluded, seen = [], [], set()
    for query in snapshot['queries']:
        taken = 0
        streams = [[(source, row) for row in data['rows']] for source, data in query['sources'].items()]
        for group in zip_longest(*streams):
            for item in group:
                if item is None:
                    continue
                source, row = item
                try:
                    identity = job_key(row.get('url') or '')
                except ValueError:
                    excluded.append({'url': row.get('url'), 'query': query['id'], 'source': source, 'reason': 'invalid_url'}); continue
                if identity in seen:
                    continue
                if not (row.get('title') and row.get('company')):
                    excluded.append({'url': row['url'], 'query': query['id'], 'source': source, 'reason': 'missing_title_or_company'}); continue
                if taken >= SAMPLE_PER_QUERY:
                    excluded.append({'url': row['url'], 'query': query['id'], 'source': source, 'reason': 'beyond_sample'}); continue
                seen.add(identity); taken += 1
                text = row.get('description') or ''
                included.append({'url': row['url'], 'query': query['id'], 'source': source, 'title': row['title'],
                                 'company': row['company'], 'location': row.get('location') or '', 'description': text,
                                 'jd': 'full' if len(text) >= FULL_JD else 'short' if text else 'missing_jd'})
    return included, excluded


def fingerprint():
    files = [p for p in (ROOT / 'backend' / 'eregion').rglob('*.py') if ' (1)' not in p.name]
    files += [ROOT / 'tools' / 'scripts' / n for n in ('source_route_scoring.py', 'run_guard.py', 'scenario_state.py')]
    # Line endings are normalised: an editor converting LF to CRLF is not a code change.
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes().replace(b'\r\n', b'\n')).hexdigest() for p in files}
    return {'tree_sha256': hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(), 'files': len(hashes)}


def deny_all_transport(counter):
    import httpx
    def handler(request):
        counter.append(request.url.host)
        raise httpx.ConnectError('layer 2 makes no source requests', request=request)
    return httpx.MockTransport(handler)


class NetworkToolRefused(ValueError):
    def __init__(self, name):
        super().__init__(f'{name} is not available in this comparison: use only the saved jobs.')
        self.workflow_detail = {'error_code': 'layer2_no_network', 'no_new_evidence': True,
                                'next': 'Assess and recommend from the saved jobs; a missing description stays missing.'}


def build(snapshot_path, out, *, model_factory=None, allow_synthetic=False):
    from eregion.api.db import upgrade_database
    from eregion.api.session_dependencies import create_session_runtime
    from eregion.runtime.mcp.config import McpRuntimeConfig
    from eregion.runtime.auto_apply.loop import ApplicationLoop
    from eregion.runtime.auto_apply.repository import ApplyRepository
    from eregion.runtime.career.alerts import job_key
    from eregion.runtime.career.discovery import JobDiscovery
    from eregion.runtime.career.service import CareerService
    snapshot = json.loads(Path(snapshot_path).read_text(encoding='utf-8'))
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    db = out / f"route-{snapshot['route']}.sqlite3"
    if db.exists():
        raise SystemExit(f'{db} exists: each build uses a fresh database.')
    # No optional sources; the Jev decision cache defaults to <database>.jev-fit.sqlite3, one per route.
    for key in ('JOB_AGENT_JOBSPY_PYTHON', 'TYPESAFE_FIT_CACHE_PATH', *[k for k in os.environ if 'TREG' in k.upper()]):
        os.environ.pop(key, None)
    # Cold caches: no decision cache next to the new database, and (below) an empty analysis cache.
    jev_cache = db.with_suffix('.jev-fit.sqlite3')
    if jev_cache.exists():
        raise SystemExit(f'{jev_cache} exists: each route starts with a cold Jev decision cache.')
    # Same version for both routes: the first build records the code fingerprint, later builds must match it.
    code = fingerprint()
    recorded = out / 'code-fingerprint.json'
    if recorded.exists() and json.loads(recorded.read_text(encoding='utf-8'))['tree_sha256'] != code['tree_sha256']:
        raise SystemExit('Code changed since the other route was built; both routes must run the same version.')
    recorded.write_text(json.dumps(code, indent=1), encoding='utf-8')
    url = 'sqlite:///' + db.as_posix()
    upgrade_database(url)
    with sqlite3.connect(db) as fresh:
        analysis_rows = fresh.execute('select count(*) from analysis_cache').fetchone()[0]
    if analysis_rows:
        raise SystemExit('The new database already holds cached analyses.')
    runtime = create_session_runtime(database_url=url, mcp_config=McpRuntimeConfig(servers=[]))
    repo = ApplyRepository(runtime.database.session_factory)
    if PROFILE_SOURCE.exists():
        with sqlite3.connect(PROFILE_SOURCE.as_uri() + '?mode=ro', uri=True) as source:
            state_json, resume = source.execute('select state_json, resume_bytes from auto_applications limit 1').fetchone()
        profile, profile_kind = json.loads(state_json)['profile'], 'saved'
    elif allow_synthetic:
        # Offline builds (CI, dry runs, prechecks) have no saved profile: a synthetic one, recorded as such.
        from eregion.domain.pdf_rendering import render_letter_pdf
        profile, profile_kind = SYNTHETIC_PROFILE, 'synthetic'
        resume = render_letter_pdf('Synthetic Candidate\nsynthetic@example.com\nEXPERIENCE\nBuilt Python APIs and data pipelines.')
    else:
        raise SystemExit(f'{PROFILE_SOURCE} is missing: a paid run needs the saved profile; it never uses a synthetic one.')
    repo.save_candidate(profile, resume=resume, name='resume.pdf', extracted=True)
    service = CareerService(runtime, ApplicationLoop(repo), start_alert_scheduler=False,
                            **({'model_factory': model_factory} if model_factory else {}))
    blocked = []
    service.harness.discovery = JobDiscovery(transport=deny_all_transport(blocked))
    service.harness._treg = lambda: None  # no Treg supplement or fallback in this comparison
    original = service.harness.execute
    def execute(task_id, name, args, **kwargs):
        if name in NETWORK_TOOLS or name.startswith('browser_'):
            raise NetworkToolRefused(name)
        return original(task_id, name, args, **kwargs)
    service.harness.execute = execute
    included, excluded = sample(snapshot)
    task, _ = service.repository.create(INSTRUCTION, f"layer2-{snapshot['route']}")
    frozen = {job_key(j['url']): {'url': j['url'], 'title': j['title'], 'company': j['company'], 'location': j['location'],
                                  'description': j['description'], 'source': f"frozen:{j['source']}"}
              for j in included if j['jd'] != 'missing_jd'}
    service.repository.change(task['task_id'], retrieved_jobs=frozen)
    imported = []
    for job in included:
        if job['jd'] == 'missing_jd':
            continue
        result = original(task['task_id'], 'import_job', {'url': job['url']}, call_id='import:' + job_key(job['url'])[:40])
        imported.append(result['application_id'])
    manifest = {'route': snapshot['route'], 'snapshot_sha256': hashlib.sha256(Path(snapshot_path).read_bytes()).hexdigest(),
                'sample_rule': f'first {SAMPLE_PER_QUERY} valid cards per query, round-robin across the route sources in snapshot order, deduplicated by posting identity',
                'included': [{k: v for k, v in j.items() if k != 'description'} for j in included],
                'excluded': excluded, 'imported': len(imported), 'unscorable_missing_jd': sum(j['jd'] == 'missing_jd' for j in included),
                'task_id': task['task_id'], 'database': str(db), 'caps': CAPS, 'instruction': INSTRUCTION,
                'isolation': {'mcp_servers': 0, 'jobspy': 'unset', 'treg': 'unset', 'board_hosts_refused': blocked,
                              'refused_tools': sorted(NETWORK_TOOLS)},
                'cold_start': {'database_new': True, 'jev_cache_existed': False, 'analysis_cache_rows': analysis_rows,
                               'jev_cache_path': str(jev_cache)},
                'code_tree_sha256': code['tree_sha256'], 'profile': profile_kind}
    (out / f"layer2-{snapshot['route']}.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding='utf-8')
    return service, runtime, task['task_id'], manifest, blocked


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['build', 'dry', 'precheck', 'run'])
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    out = Path(args.out)
    route = json.loads(Path(args.snapshot).read_text(encoding='utf-8'))['route']
    ledger = out / f'route-{route}.experiment.sqlite3'
    os.environ.update(JOB_AGENT_EXPERIMENT_ID=f'source-routes-layer2-{route}', JOB_AGENT_EXPERIMENT_LEDGER=str(ledger),
                      JOB_AGENT_EXPERIMENT_MAX_PROVIDER_ATTEMPTS=str(CAPS['provider_attempts'] if args.command == 'run' else 0),
                      JOB_AGENT_EXPERIMENT_MAX_INPUT_TOKENS=str(CAPS['input_tokens'] if args.command == 'run' else 0),
                      JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS=str(CAPS['provider_attempts']),
                      JOB_AGENT_TASK_MAX_INPUT_TOKENS=str(CAPS['input_tokens']), JOB_AGENT_ANALYSIS_WARMUP='0',
                      JOB_AGENT_JOB_API_FIRST='1')
    from run_guard import guarded, require
    with guarded(out / f'exit-{args.command}-{route}', deadline_s=CAPS['seconds']) as run:
        probe, model_factory = {}, None
        if args.command == 'dry':
            from test_career_flow import ToolModel
            model_factory = lambda: ToolModel(lambda snapshot: ('finish_task', {'text': 'Dry run: no assessment made.'}))
        elif args.command == 'precheck':
            from replay_apply_field import precheck_model
            model_factory = precheck_model(probe)
        if args.command == 'run':
            with (out / f'paid-started-{route}.lock').open('x') as lock:  # one paid execution per route and folder
                lock.write(time.strftime('%Y-%m-%dT%H:%M:%S'))
        service, runtime, task_id, manifest, blocked = build(args.snapshot, out, model_factory=model_factory,
                                                             allow_synthetic=args.command != 'run')
        run['closers'] += [('career_service', service.close), ('applications', service.applications.close), ('runtime', runtime.close)]
        run['result'] = {'manifest': {k: manifest[k] for k in ('route', 'imported', 'unscorable_missing_jd', 'task_id')}}
        if args.command == 'build':
            return
        started = time.monotonic()
        service.loop.chat(task_id, INSTRUCTION, f'layer2-{route}-start')
        while time.monotonic() - started < CAPS['seconds'] - 60:
            state = service.repository.get(task_id)
            if state['status'] in {'completed', 'needs_input', 'cancelled', 'failed'} or probe.get('reservation'):
                break
            time.sleep(2)
        from eregion.runtime.provider_usage import UsageLedger, experiment_summary
        state = service.repository.get(task_id)
        usage = UsageLedger(service.repository.sessions).summary(task_id=task_id)
        run['result'].update(status=state['status'], seconds=round(time.monotonic() - started, 1),
                             tools=[h['tool'] for h in state.get('history', [])], board_hosts_refused=blocked,
                             task_usage={k: usage.get(k) for k in ('calls', 'input_tokens', 'output_tokens', 'budget_input_tokens',
                                                                    'unknown_usage_calls', 'refused_before_send')},
                             recommendations=len(state.get('recommendations', [])))
        if ledger.exists():
            run['result']['experiment'] = {k: v for k, v in experiment_summary(str(ledger), f'source-routes-layer2-{route}').items() if k != 'groups'}
        if args.command == 'precheck':
            run['result']['first_request'] = {k: probe.get(k) for k in ('model', 'messages', 'tools', 'payload_bytes', 'reservation')}
            require(probe.get('reservation') and usage['calls'] == 0, f'precheck sent or failed to build a request: {probe} {usage}')
        if args.command == 'dry':
            require(usage['calls'] == 0 and state['status'] == 'completed', f'dry run used the provider: {usage}')
        require(not blocked, f'a source request was attempted: {blocked}')


if __name__ == '__main__':
    main()
