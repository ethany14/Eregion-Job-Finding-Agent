"""Real-model A/B of the Jev wire encoding (T6, user-approved 2026-10-20). One paid execution per folder.

Arms, run one after the other on the same frozen public JD and synthetic resume, each with a new cold
decision cache: inline (the earlier encoding: encode_wire replaced by identity inside this process) and
shared (current). Real Jev settings come from the project .env (the key is never printed or saved).

Caps, enforced before every send by the experiment ledger and the guard: 20 provider attempts and
330,000 input-token units in total (pending requests hold their reservation, UTF-8 bytes + 1,024),
600 seconds. Reasoning escalation and rejection retries are off, so nothing is retried. If an arm ends
with a provider error or unknown usage, the next arm does not start.

Usage: token_t6_scoring.py precheck|run --out DIR
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'tools' / 'scripts')]
from token_baseline import RESUMES, SNAPSHOTS, FakeJev, frozen_jobs, sha  # noqa: E402

JOB_ID = 'B0-jobspy_indeed'
CAPS = {'provider_attempts': 20, 'input_tokens': 330_000, 'seconds': 600}
EXPERIMENT = 'token-t6-scoring-20261020'


class Recording(httpx.BaseTransport):
    """Forwards to the real transport and keeps each request and response body for the comparison."""

    def __init__(self, inner):
        self.inner, self.exchanges = inner, []

    def handle_request(self, request):
        response = self.inner.handle_request(request)
        response.read()
        try:
            body = response.json()
        except ValueError:
            body = None
        self.exchanges.append({'request': json.loads(request.content), 'status': response.status_code, 'response': body})
        return response


def stable(result):
    def strip(value):
        if hasattr(value, 'model_dump'):
            value = value.model_dump(mode='json')
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k not in {'analysis_id', 'valid_until', 'latency_seconds'}}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value
    return {'job': strip(result['job_analysis']), 'match': strip(result['skill_match']),
            'status': result['status'], 'model_calls': result['model_calls']}


def arm(name, encoder, config, jobs, resume, transport):
    from eregion.domain.jev_fit import transport as wire
    from eregion.domain.jev_fit.service import JevFitError, JevFitService
    original = wire.encode_wire
    if encoder == 'inline':
        wire.encode_wire = lambda state, questions: (state, questions)
    started = time.monotonic()
    error = None
    try:
        results = JevFitService(config, transport=transport).analyze_many(resume_text=resume, jobs=jobs)
    except JevFitError as exc:
        results, error = {}, str(exc)[:300]
    finally:
        wire.encode_wire = original
    return {'arm': name, 'seconds': round(time.monotonic() - started, 1), 'error': error,
            'results': {k: stable(v) for k, v in results.items()}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['precheck', 'run'])
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for key in ('JOB_AGENT_REASONING_ESCALATION', 'JOB_AGENT_REASONING_REJECTION_RETRY'):
        os.environ[key] = '0'
    ledger = out / 'experiment-ledger.sqlite3'
    if args.command == 'run':
        os.environ.update(JOB_AGENT_EXPERIMENT_ID=EXPERIMENT, JOB_AGENT_EXPERIMENT_LEDGER=str(ledger),
                          JOB_AGENT_EXPERIMENT_MAX_PROVIDER_ATTEMPTS=str(CAPS['provider_attempts']),
                          JOB_AGENT_EXPERIMENT_MAX_INPUT_TOKENS=str(CAPS['input_tokens']))
    from run_guard import guarded, require
    with guarded(out / f'exit-{args.command}', deadline_s=CAPS['seconds']) as run:
        from eregion.domain.jev_fit.settings import Settings
        from eregion.runtime.provider_usage import experiment_summary
        jobs = [{'id': j['id'], 'job_description': j['job_description']} for j in frozen_jobs(SNAPSHOTS) if j['id'] == JOB_ID]
        resume = RESUMES[0].read_text(encoding='utf-8')
        require(len(jobs) == 1, 'frozen JD missing')
        live = Settings.load()
        require(bool(live.key) and live.model == 'jev-1.13.0', 'Jev settings: a key and jev-1.13.0 are required')
        run['result'] = {'job': JOB_ID, 'jd_sha256': sha(jobs[0]['job_description']), 'resume_sha256': sha(resume),
                         'model': live.model, 'caps': CAPS}
        if args.command == 'precheck':
            # Same arms with a fake endpoint; any non-loopback connection fails the precheck.
            import socket
            from token_baseline import _loopback
            connect = socket.socket.connect

            def guarded_connect(self, address):
                if self.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(address):
                    raise RuntimeError(f'precheck attempted an outside connection: {address!r}')
                return connect(self, address)
            socket.socket.connect = guarded_connect
            from dataclasses import replace
            for name in ('inline', 'shared'):
                cache = out / f'precheck-{name}.sqlite3'
                cache.unlink(missing_ok=True)
                fake = FakeJev()
                result = arm(name, name, replace(live, cache_path=cache), jobs, resume, httpx.MockTransport(fake))
                run['result'][name] = {'requests': len(fake.requests), 'bytes': sum(r['size']['bytes'] for r in fake.requests),
                                       'reservation': sum(r['size']['bytes'] + 1024 for r in fake.requests), 'error': result['error']}
            require(not ledger.exists(), 'precheck must not open the paid experiment ledger')
            return
        with (out / 'paid-started.lock').open('x') as lock:  # one paid execution per folder
            lock.write(time.strftime('%Y-%m-%dT%H:%M:%S'))
        from dataclasses import replace
        arms = []
        for name in ('inline', 'shared'):
            cache = out / f'cold-{name}.sqlite3'
            require(not cache.exists(), f'{cache} exists: each arm starts cold')
            recording = Recording(httpx.HTTPTransport())
            outcome = arm(name, name, replace(live, cache_path=cache), jobs, resume, recording)
            (out / f'exchanges-{name}.json').write_text(json.dumps(recording.exchanges, indent=1), encoding='utf-8')
            usage = experiment_summary(str(ledger), EXPERIMENT)
            outcome['requests'] = len(recording.exchanges)
            outcome['http_statuses'] = sorted({e['status'] for e in recording.exchanges})
            outcome['request_bytes'] = sum(len(json.dumps(e['request'], ensure_ascii=False).encode()) for e in recording.exchanges)
            outcome['tokens'] = {k: sum((e['response'] or {}).get('usage', {}).get(k) or 0 for e in recording.exchanges)
                                 for k in ('prompt_tokens', 'completion_tokens')}
            outcome['experiment_after_arm'] = {k: usage.get(k) for k in ('calls', 'input_tokens', 'output_tokens', 'cached_input_tokens',
                                                                          'unknown_usage_calls', 'refused_before_send')}
            arms.append(outcome)
            (out / f'arm-{name}.json').write_text(json.dumps(outcome, indent=1), encoding='utf-8')
            if outcome['error'] or usage.get('unknown_usage_calls') or any(s != 200 for s in outcome['http_statuses']):
                run['result']['stopped_after'] = name
                break
        run['result']['arms'] = [{k: v for k, v in a.items() if k != 'results'} for a in arms]


if __name__ == '__main__':
    main()
