"""B6 production-loop replays with recorded web IO and scripted decisions.

The opt-in model lane uses the same frozen web and offline scoring. It is paid;
normal runs never initialize a provider or make a network request.
"""
import copy
import hashlib
import json
import os
import socket
import time
import traceback
from pathlib import Path
from threading import local
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from eregion.runtime.career.alerts import job_key
from eregion.runtime.career.discovery import JobDiscovery
from eregion.runtime.career.service import CareerService
from eregion.runtime.provider_usage import UsageLedger, summarize
from eregion.api.services.fit_analysis_service import FitAnalysisService
from eregion.domain.pdf_rendering import render_letter_pdf
from scripts.e2e_repair_evaluation import APPLICANTS
from scripts.remaining_search_acceptance import evaluate_search
from test_career_flow import career
from test_remaining_fixed_catalog import main_handler


FIXTURE = Path(__file__).parent / 'fixtures/b6_offline_web.json'
LIVE_MODEL = os.getenv('JOB_AGENT_B6_REPLAY_LIVE_MODEL') == '1'


@pytest.fixture
def frozen_web():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


@pytest.fixture
def offline_guard(monkeypatch, request):
    """Record attempts even when the real loop converts exceptions to outcomes."""
    monkeypatch.setenv('JOB_AGENT_ANALYSIS_WARMUP', '0')
    attempts = []
    paid = request.node.name == 'test_b6_frozen_web_with_configured_model' and LIVE_MODEL
    if not paid:
        original_pair, original_connect = socket.socketpair, socket.socket.connect
        ipc = local()
        def socketpair(*args, **kwargs):
            # Windows asyncio implements its self-wakeup pipe using the stdlib
            # loopback socketpair. Permit only that internally-created IPC pair;
            # direct connections (including loopback) remain forbidden.
            ipc.socketpair = True
            try:
                return original_pair(*args, **kwargs)
            finally:
                ipc.socketpair = False
        def connect(sock, address):
            if getattr(ipc, 'socketpair', False) and address[0] in {'127.0.0.1', '::1'}:
                return original_connect(sock, address)
            return forbidden(sock, address)
        def forbidden(*args, **kwargs):
            attempts.append([(Path(frame.filename).name, frame.name) for frame in traceback.extract_stack(limit=24)])
            raise AssertionError('B6 offline replay attempted network/provider IO')
        monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
        monkeypatch.setattr(socket, 'socketpair', socketpair)
        monkeypatch.setattr(socket.socket, 'connect', connect)
        monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
        monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden)
        monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', forbidden)
        import eregion.domain.model
        import eregion.runtime.provider_usage
        monkeypatch.setattr(eregion.domain.model, 'create_model', forbidden)
        monkeypatch.setattr(eregion.domain.model, 'shared_model', forbidden)
        monkeypatch.setattr(eregion.runtime.provider_usage, 'provider_call', forbidden)
    yield attempts
    assert attempts == [], f'A caught IO failure must still fail the replay: {attempts}'


class FrozenDiscovery(JobDiscovery):
    """Real board parsing and posting extraction, with archived error projections.

Public search results and rendered-only jobs are recorded projections. Every
HTTP response comes from MockTransport; unknown requests cannot fall through.
"""
    def __init__(self, fixture, *, blocked_unknown=False):
        self.fixture = fixture
        self.blocked_unknown = blocked_unknown
        self.blocked_requests = []
        self.requests, self.searches, self.unexpected = [], [], []
        super().__init__(transport=httpx.MockTransport(self._serve),
            resolver=lambda *a, **k: [(None, None, None, None, ('8.8.8.8', 443))])

    def fail(self, message, *, missing=False):
        if missing and self.blocked_unknown:
            from eregion.runtime.career.source_health import SourceBlocked
            self.blocked_requests.append(message)
            raise SourceBlocked('Frozen source unavailable: no recorded response')
        self.unexpected.append(message)
        raise AssertionError(message)

    def _serve(self, request):
        key = str(request.url)
        self.requests.append((request.method, key))
        record = self.fixture['responses'].get(key)
        if record is None:
            self.fail(f'Unexpected frozen HTTP request: {request.method} {key}', missing=True)
        if request.method != record.get('method', 'GET'):
            self.fail(f'Unexpected method for {key}')
        error = record.get('recorded_error')
        if error:
            if error['type'] == 'HTTPStatusError':
                # The archive retains the exception class, not the HTTP status.
                # Zero is an unknown sentinel, never a claimed real status code.
                raise httpx.HTTPStatusError(error['message'], request=request,
                    response=httpx.Response(0, request=request))
            assert error['type'] == 'ValueError'
            raise ValueError(error['message'])
        return httpx.Response(record.get('status', 200), text=record['body'],
            headers={'content-type': record.get('content_type', 'text/html')})

    def search(self, query, location='', boards=None, limit=10,
               excluded_hosts=(), excluded_sources=()):
        self.searches.append({'query': query, 'location': location, 'boards': boards,
            'limit': limit, 'excluded_hosts': list(excluded_hosts),
            'excluded_sources': list(excluded_sources)})
        if boards:
            return super().search(query, location, boards, limit,
                excluded_hosts, excluded_sources)
        key = json.dumps([query.casefold().strip(), location.casefold().strip()])
        record = self.fixture['searches'].get(key)
        if record is None:
            self.fail(f'Unexpected frozen public search: {key}', missing=True)
        result = copy.deepcopy(record['result'])
        result['jobs'] = [j for j in result.get('jobs', []) if not any(
            (urlsplit(j['url']).hostname or '') == host or
            (urlsplit(j['url']).hostname or '').endswith('.' + host)
            for host in excluded_hosts)][:limit]
        return result

    def job(self, url):
        record = self.fixture['jobs'].get(url)
        if record:
            # Rendered browser captures have no raw HTTP body in the archive.
            # Serve the captured public text without inventing HTML or metadata.
            return copy.deepcopy(record['job'])
        return super().job(url)


class FrozenBrowser:
    """Task-scoped rendered observations; no browser process or external IO."""
    def __init__(self, fixture, discovery):
        self.fixture, self.discovery = fixture, discovery
        self.states, self.calls = {}, []
        self.pending_capture = None

    def inspect(self, task_id):
        return self.states.get(task_id)

    def call(self, task_id, name, args, **kwargs):
        self.calls.append((name, copy.deepcopy(args)))
        previous = self.inspect(task_id)
        url = args.get('url') if name == 'browser_open' else (previous or {}).get('url')
        capture = self.pending_capture
        if args.get('job_only'):
            record = next((r for r in self.fixture['browser_pages'].values()
                           if r['page']['url'] == url), None)
        elif capture:
            self.pending_capture = None
            record = self.fixture['browser_pages'][capture]
            if record['action'] != name:
                self.discovery.fail(f'Unexpected action for frozen capture {capture}: {name}')
            for key, expected in record['arguments'].items():
                if key != 'observation_id' and args.get(key) != expected:
                    self.discovery.fail(f'Unexpected {key} for frozen capture {capture}')
        else:
            record = next((r for r in self.fixture['browser_pages'].values()
                           if r['page']['url'] == url and r['action'] == name), None)
        if record is None:
            self.discovery.fail(f'Unexpected frozen browser URL/action: {name} {url}', missing=True)
        page = copy.deepcopy(record['page'])
        page['observation_id'] = f'b6-observation-{len(self.calls)}'
        page['page_evidence_hash'] = hashlib.sha256(
            json.dumps(record['page'], sort_keys=True).encode()).hexdigest()
        if args.get('job_only'):
            job = self.fixture['jobs'].get(url)
            if not job:
                self.discovery.fail(f'No bounded frozen posting for {url}', missing=True)
            job = job['job']
            prefix = '\n'.join(filter(None, [job['title'], job['company'], job['location']])) + '\n'
            body = job['description'].removeprefix(prefix)
            page.update(primary_heading=job['title'], job_company=job['company'],
                job_location=job['location'], text=body,
                body_scope='bounded_selector', text_length=len(body))
        offset = args.get('text_offset', 0)
        page['text_offset'] = offset
        page['text_length'] = page.get('text_length', len(page.get('text', '')))
        page['text'] = page.get('text', '')[offset:offset + 7000]
        self.states[task_id] = page
        return copy.deepcopy(page)

    def close_task(self, task_id):
        self.states.pop(task_id, None)

    def close(self):
        self.states.clear()


def test_live_fixture_missing_source_is_blocked_without_network():
    from eregion.runtime.career.source_health import SourceBlocked
    discovery = FrozenDiscovery({'responses': {}, 'searches': {}, 'jobs': {},
                                 'browser_pages': {}}, blocked_unknown=True)
    with pytest.raises(SourceBlocked):
        discovery.search('unrecorded')
    with pytest.raises(SourceBlocked):
        FrozenBrowser(discovery.fixture, discovery).call('task', 'browser_open',
            {'url': 'https://unrecorded.example.com/'})
    with pytest.raises(SourceBlocked):
        discovery.read('https://unrecorded.example.com/')
    assert len(discovery.blocked_requests) == 3
    assert discovery.unexpected == []
    with pytest.raises(AssertionError):
        discovery.fail('recorded action mismatch')


class ScriptedModel:
    def __init__(self, steps, browser):
        self.steps = copy.deepcopy(steps)
        self.browser = browser
        self.calls, self.snapshots, self.emitted, self.failures = 0, [], [], []
        self.context_refs = {}

    def invoke(self, messages, tools):
        snapshot = json.loads(next(m.content for m in reversed(messages)
            if isinstance(m, HumanMessage)))
        self.snapshots.append(snapshot)
        # The production request deliberately moves recent results out of history
        # into ToolMessages. Bind refs from that same model-visible evidence.
        for message in messages:
            if isinstance(message, ToolMessage) and message.tool_call_id.startswith('b6-call-'):
                result = json.loads(message.content)
                if result.get('context_ref'):
                    self.context_refs[int(message.tool_call_id.removeprefix('b6-call-')) - 1] = result['context_ref']
        for event in snapshot['history']:
            if event.get('result', {}).get('context_ref') and event.get('call_id', '').startswith('b6-call-'):
                self.context_refs[int(event['call_id'].removeprefix('b6-call-')) - 1] = event['result']['context_ref']
        if self.calls >= len(self.steps):
            self.failures.append('Script exhausted')
            raise AssertionError('Script exhausted before the production loop stopped')
        step = self.steps[self.calls]
        self.calls += 1
        name, args = step['tool'], copy.deepcopy(step.get('arguments', {}))
        self.browser.pending_capture = step.get('browser_capture')
        names = {t['function']['name'] for t in tools}
        if name not in names:
            self.failures.append(f'Tool unavailable: {name}')
            raise AssertionError(f'Script requested unavailable tool {name}')
        if 'application_url' in step:
            target = next((j for j in snapshot['jobs']
                if job_key(j['url']) == job_key(step['application_url'])), None)
            if not target:
                self.failures.append('Imported job missing')
                raise AssertionError(step['application_url'])
            args['application_id'] = target['application_id']
            if name == 'recommend_job' and 'tier' not in args:
                # Historical recordings predate the tier contract (tiered-recommendations-v1).
                args.update(tier='priority', evidence=[target['title']], gaps=[], to_confirm=[],
                            next_steps=['Review the posting and apply if interested.'])
        if name == 'finish_task' and 'stop_reason' not in args:
            # Historical recordings predate the stop_reason field (tiered-recommendations-v3).
            args['stop_reason'] = 'Recorded run: searching ended after the recorded sources and candidates.'
        if name == 'finish_task' and step.get('render_final'):
            # The scripted reply follows the saved state: recommendations under their saved tier,
            # every other read/scored job under its own heading (not a recommendation).
            jobs = snapshot['jobs']
            saved = {r['url']: r.get('tier') for r in snapshot.get('recommended_jobs', [])}
            labels = {'priority': 'Priority match', 'stretch': 'Worth trying (known gaps)', 'to_confirm': 'Needs your confirmation'}
            lines = [step['render_final']]
            for tier, label in labels.items():
                group = [job for job in jobs if saved.get(job['url']) == tier]
                if group or tier == 'priority':
                    lines.append(label)
                lines += [f"\n{job['title']}\n{job['url']}\nScore: {job['match_score']}/100" for job in group]
            others = [job for job in jobs if job['url'] not in saved]
            if others:
                lines.append('### Not recommended')
                lines += [f"\n{job['title']}\n{job['url']}\nScore: {job['match_score']}/100" for job in others]
            args['text'] = '\n'.join(lines)
        if args.get('observation_id') == '$current':
            args['observation_id'] = snapshot['browser']['observation_id']
        if args.get('ref') == '$context':
            args['ref'] = self.context_refs[step['context_step']]
        self.emitted.append({'tool': name, 'arguments': args})
        return AIMessage(content='', tool_calls=[{
            'name': name, 'args': args, 'id': f'b6-call-{self.calls}'}])

    def usage(self):
        # Zero provider tokens are known for each scripted invocation. This is
        # explicit offline accounting for the unchanged acceptance evaluator,
        # separate from the actual (empty) provider ledger.
        return {**summarize([{'purpose': 'agent', 'model': 'b6-scripted-offline',
            'source': 'scripted_offline', 'status': 'completed',
            'input_tokens': 0, 'output_tokens': 0} for _ in range(self.calls)]),
            'accounting': 'scripted offline decisions; zero provider tokens, not live usage'}


@pytest.fixture
def replay(offline_guard, career, frozen_web, monkeypatch, request):
    c = career
    # Replay the historical agent's over-inclusive judgment explicitly. These
    # tests measure workflow/cost and preserve its failing acceptance outcome;
    # they do not claim that these scripted labels are correct model semantics.
    from eregion.runtime.career import qualification
    from eregion.domain.search_qualification import SearchQualification, judge
    def historical_judge(text, postings):
        return SearchQualification.model_validate({'decisions':[
            {'posting_id':j['posting_id'],**{field:{'status':'met','request_quote':text,
                'reason':'Frozen historical model decision, intentionally over-inclusive.',
                'evidence':[{'source':source,'quote':(j.get(source) or j['description'])[:1000]}]}
                for field,source in [('role','title'),('location','location'),('seniority','description'),('vacancy','description')]}}
            for j in postings]})
    monkeypatch.setattr(qualification,'judge',judge if LIVE_MODEL else historical_judge)
    monkeypatch.setenv('TYPESAFE_ENABLED', 'false')
    monkeypatch.setenv('JOB_AGENT_MAIN_CATALOG', 'local')
    c.repository.change(c.state['task_id'], status='cancelled', pause_requested=True)
    c.applications.repository.save_candidate(
        {'first_name': 'Synthetic', 'last_name': 'analyst', 'email': 'analyst@example.com'},
        resume=render_letter_pdf(APPLICANTS['analyst']), name='synthetic.pdf', extracted=True)
    scoring_calls = []
    c.harness.analysis._fit_service = FitAnalysisService(main_handler(scoring_calls,allow_extraction=True),
        jev_enabled=False, cache_size=0, cache_namespace=lambda: 'b6-offline-scoring-v1')
    discovery = FrozenDiscovery(frozen_web, blocked_unknown=(
        request.node.name == 'test_b6_frozen_web_with_configured_model' and LIVE_MODEL))
    browser = FrozenBrowser(frozen_web, discovery)
    c.harness.discovery, c.harness._browsing = discovery, browser

    def run(case, *, model_factory=None, timeout=20):
        model = ScriptedModel(frozen_web['replays'][case]['steps'], browser) if model_factory is None else None
        service = CareerService(c.runtime, c.applications, harness=c.harness,
            model_factory=model_factory or (lambda: model), start_alert_scheduler=False)
        service.loop.budget = 60
        service.loop.retry_delays = ()
        try:
            task = service.start(frozen_web['goal'], f'b6-offline-{case}')
            task_id = task['run_id']
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                state = service.repository.get(task_id)
                if state['status'] in {'completed', 'needs_input', 'cancelled'}:
                    break
                time.sleep(.01)
            else:
                raise AssertionError(f'B6 replay did not settle: {state}')
            provider_usage = UsageLedger(service.repository.sessions).summary(task_id=task_id)
            attempt_rows = []
            if model is None:  # paid lane: keep per-attempt rows for the live report
                from sqlalchemy import select
                from eregion.runtime.provider_usage import ProviderUsageRow
                with service.repository.sessions() as db:
                    attempt_rows = [json.loads(r.details_json) for r in db.scalars(
                        select(ProviderUsageRow).where(ProviderUsageRow.task_id == task_id))]
            if model:
                assert model.failures == []
                assert provider_usage['calls'] == 0
                assert state['model_calls'] == model.calls
            assert discovery.unexpected == [], 'Unexpected fixture IO was caught by the task loop'
            assert not state['application_runs'] and not c.applications.repository.list()
            assert not service.harness.alerts.list()
            retrieved = {job_key(j['url']) for j in state.get('retrieved_jobs', {}).values()}
            jobs = []
            for identifier in state['jobs']:
                job = service.harness.job_summary(identifier, detail=True)
                job['retrieved'] = job_key(job['url']) in retrieved
                job['disqualified'] = any(m.get('match_status') == 'confirmed_condition'
                    and m.get('condition_satisfied') is False
                    for m in (job.get('match_report') or {}).get('matches', []))
                jobs.append(job)
            summary = {'status': state['status'], 'calls': state['model_calls'],
                'error': (state.get('error') or {}).get('code'), 'applications': 0, 'schedules': 0,
                'verified_jobs': jobs, 'recommendations': state.get('recommendations', []),
                'final': next((m['text'] for m in reversed(state['messages'])
                    if m['role'] == 'assistant'), ''),
                'usage': model.usage() if model else provider_usage,
                'provider_usage': provider_usage, 'attempt_rows': attempt_rows}
            return SimpleNamespace(state=state, summary=summary, model=model,
                acceptance=evaluate_search(summary, 'analyst'), discovery=discovery,
                browser=browser, scoring_calls=scoring_calls)
        finally:
            service.close()
    yield run
    assert discovery.unexpected == []


def test_b6_r1_unverified_associate_cannot_supply_second_qualified_job(replay):
    result = replay('r1')
    assert result.state['status'] == 'completed', result.state['history']
    assert len(result.state['recommendations']) == 2
    associate = next(j for j in result.state['recommendations'] if j['title'] == 'Associate Data Analyst')
    assert not associate.get('seniority_warning')  # No runtime regex override.
    assert result.acceptance['qualified_count'] == 1
    assert result.acceptance['failures'] == ['fewer_than_two_verified_scored_qualifying_recommendations']
    checked = {j['title']: j['checks'] for j in result.acceptance['jobs']}
    assert all(checked['Data Analyst'].values())
    assert checked['Associate Data Analyst']['seniority'] is False
    assert result.scoring_calls and result.summary['provider_usage']['calls'] == 0
    assert result.model.calls == 20
    assert not any(h['result'].get('rejection_code') == 'not_current_vacancy'
                   for h in result.state['history'])  # No keyword-based vacancy decision on read.
    assert [s['tool'] for s in result.model.emitted] == [
        'browser_open', 'search_jobs', 'read_job', 'browser_open', 'browser_read',
        'read_job', 'import_job', 'score_job', 'recommend_job', 'browser_read',
        'read_context', 'browser_click', 'read_job', 'read_context', 'read_context',
        'import_job', 'score_job', 'read_context', 'recommend_job', 'finish_task']


def test_b6_r2_seen_public_searches_trigger_board_guidance_and_early_stop(replay):
    result = replay('r2')
    searches = [h for h in result.state['history'] if h['tool'] == 'search_jobs']
    assert len(searches) >= 2
    assert searches[1]['result']['no_new_evidence']
    assert 'boards' in searches[1]['result']['progress_feedback']
    assert result.state['status'] == 'needs_input'
    assert result.state['error']['code'] == 'no_progress'
    assert result.state['model_calls'] < 30  # Well before the unchanged 60-call cap.
    assert not result.state.get('recommendations') and not result.state['jobs']
    assert searches[-1]['arguments']['limit'] == 2
    assert all(h['result']['no_new_evidence'] for h in searches[1:])
    assert not result.state['candidate_rejections']  # Read alone makes no semantic rejection.


def test_b6_r3_recorded_boards_use_role_terms_not_a_shared_word(replay, frozen_web):
    result = replay('r3')
    assert result.state['status'] == 'completed', result.state['history']
    assert result.model.calls == 2
    searches = [h for h in result.state['history'] if h['tool'] == 'search_jobs']
    assert searches
    # Consult full board results directly too: production histories may compact
    # them to context references. This is the same real discovery implementation.
    args = frozen_web['replays']['r3']['steps'][0]['arguments']
    recorded_titles = [j['title'] for url in frozen_web['recorded_board_urls']
                       if 'body' in frozen_web['responses'][url]
                       for j in json.loads(frozen_web['responses'][url]['body'])['jobs']]
    assert len(recorded_titles) == 20
    assert 'Accounts Payable Analyst' in recorded_titles
    assert any(t.startswith('AI Research Scientist - Data') for t in recorded_titles)
    jobs = result.discovery.search(**args)['jobs']
    # The archived top 20 rows are all unrelated to the requested role. Their
    # removal is expected. The single synthetic posting is a positive control.
    assert jobs == []
    assert searches[0]['result']['jobs'] == []
    assert {url for _, url in result.discovery.requests} >= set(frozen_web['recorded_board_urls'])
    controlled = result.discovery.search(**{**args,
        'boards': [*args['boards'], frozen_web['synthetic']['board']]})
    assert [j['title'] for j in controlled['jobs']] == ['Junior Data Analyst']
    assert all({'data', 'analyst'} <= set(j['title'].casefold().split()) for j in controlled['jobs'])
    errors = {e['board']['company']: e['error'] for e in controlled['errors']}
    assert errors == {'ramp': 'ValueError', 'plaid': 'HTTPStatusError'}


def test_b6_correct_path_passes_original_acceptance_with_exact_saved_scores(replay):
    result = replay('correct')
    assert result.state['status'] == 'completed', result.state['history']
    assert result.acceptance['status'] == 'passed', result.acceptance
    assert result.acceptance['qualified_count'] == 2
    assert len(result.state['recommendations']) == 2
    assert {r['company'] for r in result.state['recommendations']} == {'GoodApple', 'B6 Synthetic Analytics'}
    assert 'blocked' in result.summary['final'].casefold()
    assert 'linkedin' in result.summary['final'].casefold()
    assert result.state['search_source_health']['sources']
    assert any(s['boards'] for s in result.discovery.searches)
    assert result.scoring_calls
    assert result.summary['provider_usage']['calls'] == 0
    by_url = {job_key(j['url']): j for j in result.summary['verified_jobs']}
    for recommendation in result.state['recommendations']:
        saved = by_url[job_key(recommendation['url'])]
        assert recommendation['match_score'] == saved['match_score']
        assert saved['url'] in result.summary['final']
        assert f"Score: {saved['match_score']}/100" in result.summary['final']
        assert not recommendation.get('seniority_warning')


def test_b6_unknown_fixture_io_fails_loudly(offline_guard):
    empty_web = {'responses': {}, 'searches': {}, 'browser_pages': {}}
    discovery = FrozenDiscovery(empty_web)
    with pytest.raises(AssertionError, match='Unexpected frozen HTTP'):
        discovery.read('https://unrecorded.example.com/no-network-fallback')
    with pytest.raises(AssertionError, match='Unexpected frozen public search'):
        discovery.search('unrecorded query')
    browser = FrozenBrowser(empty_web, discovery)
    with pytest.raises(AssertionError, match='Unexpected frozen browser URL'):
        browser.call('task', 'browser_open', {'url': 'https://unrecorded.example.com/'})
    assert len(discovery.unexpected) == 3


def test_b6_recorded_blocked_and_aggregator_outcomes(offline_guard, frozen_web):
    discovery = FrozenDiscovery(frozen_web)
    browser = FrozenBrowser(frozen_web, discovery)
    for capture, blocked in [('r1:0', 'credentials'), ('r2:7', 'credentials'),
                             ('r2:9', 'bot_challenge'), ('r3:4', 'bot_challenge')]:
        record = frozen_web['browser_pages'][capture]
        browser.pending_capture = capture
        page = browser.call('frozen-outcomes', 'browser_open', record['arguments'])
        assert page['blocked'] == blocked
    for url, record in frozen_web['responses'].items():
        if not record.get('recorded_error'):
            continue
        error = record['recorded_error']
        expected = httpx.HTTPStatusError if error['type'] == 'HTTPStatusError' else ValueError
        with pytest.raises(expected):
            discovery.read(url)
    assert discovery.unexpected == []


@pytest.mark.skipif(not LIVE_MODEL, reason='Opt-in paid model against frozen B6 web; never run by offline validation')
def test_b6_frozen_web_with_configured_model(replay, monkeypatch):
    from eregion.runtime.tasks.loop import TaskModel
    output = os.getenv('OPT_LIVE_OUTPUT')
    assert output, 'Paid attempts require an unused OPT_LIVE_OUTPUT directory'
    target = Path(output)
    assert not (target / 'result.json').exists(), 'Never overwrite a paid attempt'
    monkeypatch.setenv('JOB_AGENT_TASK_MAX_INPUT_TOKENS', str(min(400000,
        int(os.getenv('JOB_AGENT_TASK_MAX_INPUT_TOKENS', '400000')))))
    monkeypatch.setenv('JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS', str(min(60,
        int(os.getenv('JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS', '60')))))
    started = time.monotonic()
    result = replay('correct', model_factory=TaskModel, timeout=600)
    if output:
        from evals.opt_report import wire_summary
        target = Path(output)
        target.mkdir(parents=True, exist_ok=True)
        actions = [a.get('tool') for a in result.state.get('actions', [])]
        (target / 'result.json').write_text(json.dumps({'status': result.state['status'], 'decisions': result.state['model_calls'],
            'error': result.summary['error'], 'acceptance': result.acceptance, 'provider_usage': result.summary['provider_usage'],
            'wire': wire_summary(result.summary['attempt_rows']), 'actions': actions,
            'seconds': time.monotonic() - started,
            'blocked_frozen_requests': len(result.discovery.blocked_requests),
            'attempt_errors': [r.get('error_type') for r in result.summary['attempt_rows'] if r.get('error_type')],
            'flags': {k: v for k, v in os.environ.items() if k.startswith('JOB_AGENT_OPT_')}}, indent=1, default=str),
            encoding='utf-8')
    assert result.state['status'] == 'completed'
    assert result.acceptance['status'] == 'passed', result.acceptance
