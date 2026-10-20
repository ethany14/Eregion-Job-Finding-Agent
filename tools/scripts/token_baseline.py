"""Free request-size baselines for token work: real request construction, fake transport, nothing sent.

scoring: JevFitService.analyze_many (the production multi-job path) over frozen public JDs and the
  synthetic test resumes, cold decision cache. A deterministic endpoint answers each yes/no question
  from a hash of its judgment and the source text it targets, so two request protocols that carry the
  same questions get the same answers; equal analysis digests show no span was lost or misreferenced.
apply: see token_baseline_apply.py.

Reports characters and UTF-8 bytes only. No tokenizer compatible with the providers is available, so no
token counts are estimated; provider usage needs a real run. Outputs hold sizes and hashes, never text.

Usage: token_baseline.py scoring --out FILE [--snapshots DIR] [--label NAME]
"""
import argparse
import hashlib
import json
import socket
import sys
import tempfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'backend')]

SNAPSHOTS = ROOT / '.codex-runs' / 'source-routes-01'
RESUMES = [ROOT / 'tests' / 'fixtures' / 'resume_source_regressions' / name for name in ('avery.txt', 'riley.txt')]
# Judgment kinds by the stable question text each stage asks.
JUDGMENTS = [('Does this span state', 'is_requirement'), ('Is this candidate qualification', 'is_preferred'),
             ('Does this span specify', 'is_requirement_refined'), ('Does this qualification explicitly', 'is_preferred_refined'),
             ('Is `requirement` a condition', 'is_condition'), ('Does the requirement ask for personal', 'is_condition_refined'),
             ('Does the evidence support', 'full_support'), ('Does the evidence show any', 'related'),
             ('Has the candidate explicitly', 'fact_known'), ('Does this excerpt establish', 'focused_full'),
             ('Does this excerpt show any', 'focused_related'), ('Does the complete evidence', 'clarified_full'),
             ('Does this excerpt substantiate', 'citation')]


# Each stage asks one family of judgments, so a request's judgments name its stage.
STAGE_OF = {'is_requirement': 'extract_requirements', 'is_preferred': 'extract_requirements',
            'is_requirement_refined': 'refine_extraction_yes_no', 'is_preferred_refined': 'refine_extraction_yes_no',
            'is_condition': 'condition_category_yes_no', 'is_condition_refined': 'condition_category_yes_no',
            'full_support': 'support_yes_no', 'related': 'support_yes_no', 'fact_known': 'support_yes_no',
            'focused_full': 'focused_yes_no', 'focused_related': 'focused_yes_no',
            'clarified_full': 'clarify_full_support_yes_no', 'citation': 'verify_citations_yes_no'}


def sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def size(value):
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return {'chars': len(text), 'bytes': len(text.encode('utf-8'))}


def strings(value, out):
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            strings(v, out)
    elif isinstance(value, list):
        for v in value:
            strings(v, out)
    return out


def duplicate_bytes(value, minimum=40):
    """Bytes of exact repeats of string values of at least `minimum` characters within one payload."""
    seen, total = {}, 0
    for s in strings(value, []):
        if len(s) >= minimum:
            seen[s] = seen.get(s, 0) + 1
    for s, count in seen.items():
        total += (count - 1) * len(s.encode('utf-8'))
    return total


def _loopback(address):
    host = address[0] if isinstance(address, tuple) else address
    if not isinstance(host, str) or host in {'localhost', ''}:
        return True
    import ipaddress
    try:
        return ipaddress.ip_address(host.split('%')[0]).is_loopback
    except ValueError:
        return False


def no_network():
    """The offline suite's isolation: never the repository .env, a fake model, no outside connection.

    Loopback stays open for the local Chrome driver; any other connection fails the run, so the fake
    transport or scripted model is the only provider path.
    """
    import importlib
    import os
    import socket
    import dotenv
    sys.path.insert(0, str(ROOT / 'tests'))
    from conftest import DOTENV_READERS, FAKE_MODEL, REPO_ENV
    real = dotenv.dotenv_values

    def without_repo_env(path=None, *args, **kwargs):
        if path is not None and Path(path).resolve() == REPO_ENV:
            return {}
        return real(path, *args, **kwargs)

    for name in DOTENV_READERS:
        module = importlib.import_module(name)
        if hasattr(module, 'dotenv_values'):
            module.dotenv_values = without_repo_env
    dotenv.dotenv_values = without_repo_env
    os.environ.update(FAKE_MODEL)
    os.environ['JOB_AGENT_ANALYSIS_WARMUP'] = '0'
    os.environ['JOB_AGENT_QUALIFICATION_SECOND_OPINION'] = '0'
    original = socket.socket.connect

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(address):
            raise RuntimeError(f'token_baseline: an outside connection was attempted: {address!r}')
        return original(self, address)
    socket.socket.connect = connect


class FakeJev:
    """Deterministic answers keyed by judgment kind and the source text the question targets."""

    def __init__(self):
        self.requests = []

    @staticmethod
    def judgment(question):
        return next((kind for prefix, kind in JUDGMENTS if question.startswith(prefix)), 'other:' + question[:40])

    @staticmethod
    def case_state(state, ins):
        case = ins.get('context_case')
        return state['cases'][case] if case else state

    @classmethod
    def target(cls, state, ins):
        case = cls.case_state(state, ins)
        span, section = ins.get('span'), ins.get('section')
        if 'span_id' in ins:
            entry = case['spans'][ins['span_id']]
            span, section = entry['text'], entry.get('section')
        requirement, subject = ins.get('requirement'), ins.get('subject')
        if 'requirement_id' in ins:
            entry = case['requirements'][ins['requirement_id']]
            requirement = entry.get('text') if requirement is None else requirement
            subject = entry.get('subject') if subject is None and 'subject' in ins.get('uses', ['subject']) else subject
        return [span, section, requirement, subject, ins.get('excerpt'), ins.get('support_level')]

    @staticmethod
    def resolve(state, questions):
        """Inline {"$ref": ID} values from state.shared_values (independent of the product's decoder)."""
        shared = state.get('shared_values', {})

        def walk(value):
            if isinstance(value, dict):
                if set(value) == {'$ref'}:
                    if value['$ref'] not in shared:
                        raise ValueError('reference to a value this request does not carry')
                    return shared[value['$ref']]
                return {k: walk(v) for k, v in value.items()}
            if isinstance(value, list):
                return [walk(v) for v in value]
            return value
        return state, {k: walk(q) for k, q in questions.items()}

    def __call__(self, request):
        payload = json.loads(request.content)
        state, questions = self.resolve(payload['state'], payload['questions'])
        answers, kinds = {}, {}
        for key, q in questions.items():
            ins = q['instructions']
            kind = self.judgment(ins['question'])
            kinds[kind] = kinds.get(kind, 0) + 1
            digest = int(sha(json.dumps([kind, self.target(state, ins)], ensure_ascii=False)), 16)
            bucket = digest % 20
            answers[key] = {'type': 'noul', 'noul': .96 if bucket < 13 else .04 if bucket < 17 else .5}
        self.requests.append({'size': size(payload), 'state': size(state), 'questions': size(questions),
                              'question_count': len(questions), 'cases': len(state.get('cases', {})) or 1,
                              'duplicate_bytes': duplicate_bytes(payload), 'kinds': kinds})
        return httpx.Response(200, json={'model': payload['model'], 'answers': answers,
                                         'usage': {'prompt_tokens': 1, 'completion_tokens': 1}})


def frozen_jobs(snapshots):
    jobs = []
    for route in 'AB':
        data = json.loads((snapshots / f'snapshot-{route}.json').read_text(encoding='utf-8'))
        for qi, query in enumerate(data['queries']):
            for source, result in query['sources'].items():
                row = next((r for r in result.get('rows', []) if len(r.get('description') or '') > 500), None)
                if row:
                    jobs.append({'id': f'{route}{qi}-{source}', 'job_description': row['description'], 'query': qi})
    return jobs


VOLATILE = {'analysis_id', 'task_id', 'run_id', 'call_id', 'seconds', 'latency_seconds', 'expires_at',
            'created_at', 'generated_at', 'analyzed_at', 'provenance', 'valid_until'}


def stable(value):
    if hasattr(value, 'model_dump'):
        value = value.model_dump(mode='json')
    if isinstance(value, dict):
        return {k: stable(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [stable(v) for v in value]
    return value


def digest_analysis(result):
    """Everything the decisions produce (job analysis and match), without timing or run identifiers."""
    keep = {'job': stable(result['job_analysis']), 'match': stable(result['skill_match'])}
    return sha(json.dumps(keep, sort_keys=True, ensure_ascii=False, default=str))


def scoring_once(args):
    from eregion.domain.jev_fit.service import JevFitService
    from eregion.domain.jev_fit.settings import Settings
    no_network()
    jobs = frozen_jobs(Path(args.snapshots))
    runs = []
    for resume_path in RESUMES:
        resume = resume_path.read_text(encoding='utf-8')
        for qi in sorted({j['query'] for j in jobs}):
            batch = [{'id': j['id'], 'job_description': j['job_description']} for j in jobs if j['query'] == qi]
            endpoint = FakeJev()
            with tempfile.TemporaryDirectory() as temp:
                config = Settings(key='baseline-not-a-key', cache_path=Path(temp) / 'decisions.sqlite3')
                service = JevFitService(config, transport=httpx.MockTransport(endpoint))
                results = service.analyze_many(resume_text=resume, jobs=batch)
            calls = {c['call_id'] for r in results.values() for c in r['decision_routing'][0]['calls']}
            if len(calls) != len(endpoint.requests):
                raise SystemExit('token_baseline: request records and captured payloads do not line up')
            requests = []
            for req in endpoint.requests:
                stages = {STAGE_OF.get(kind, 'unclassified') for kind in req['kinds']}
                if len(stages) != 1:
                    raise SystemExit(f'token_baseline: one request mixes stages {sorted(stages)}')
                requests.append({**req, 'purpose': stages.pop()})
            runs.append({'resume_sha256': sha(resume), 'jobs': [{'id': j['id'], 'jd_sha256': sha(j['job_description']),
                         'jd_chars': len(j['job_description'])} for j in batch],
                         'analysis_digests': {job_id: digest_analysis(r) for job_id, r in sorted(results.items())},
                         'requests': requests})
    by_purpose = {}
    for run in runs:
        for req in run['requests']:
            row = by_purpose.setdefault(req['purpose'], {'requests': 0, 'bytes': 0, 'chars': 0, 'state_bytes': 0,
                                                         'question_bytes': 0, 'questions': 0, 'duplicate_bytes': 0})
            row['requests'] += 1
            row['bytes'] += req['size']['bytes']
            row['chars'] += req['size']['chars']
            row['state_bytes'] += req['state']['bytes']
            row['question_bytes'] += req['questions']['bytes']
            row['questions'] += req['question_count']
            row['duplicate_bytes'] += req['duplicate_bytes']
    totals = {k: sum(r[k] for r in by_purpose.values()) for k in ('requests', 'bytes', 'chars', 'questions', 'duplicate_bytes')}
    return runs, by_purpose, totals, jobs


def scoring(args):
    from eregion.domain.jev_fit.core import VERSION
    repeats = [scoring_once(args) for _ in range(args.repeat)]
    runs, by_purpose, totals, jobs = repeats[0]
    digests = [[r['analysis_digests'] for r in rep[0]] for rep in repeats]
    spread = {k: [min(rep[2][k] for rep in repeats), max(rep[2][k] for rep in repeats)] for k in totals}
    report = {'repeats': args.repeat, 'analysis_digests_equal_across_repeats': all(d == digests[0] for d in digests),
              'totals_range': spread, 'label': args.label, 'kind': 'scoring', 'jev_version': VERSION, 'units': 'characters and UTF-8 bytes; no token estimate',
              'input': {'snapshots': str(Path(args.snapshots).relative_to(ROOT)), 'resumes': [p.name for p in RESUMES],
                        'jobs': len(jobs), 'batches': len(runs)},
              'totals': totals, 'by_purpose': by_purpose, 'runs': runs}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'totals': totals, 'totals_range': spread, 'digests_stable': report['analysis_digests_equal_across_repeats'],
                      'by_purpose': by_purpose}, indent=2))


def scoring_cache(args):
    """Cold and warm cache, reported separately: one decision cache, resume A cold, resume B warm, A warm again."""
    from eregion.domain.jev_fit.service import JevFitService
    from eregion.domain.jev_fit.settings import Settings
    no_network()
    jobs = [{'id': j['id'], 'job_description': j['job_description']} for j in frozen_jobs(Path(args.snapshots)) if j['query'] == 0]
    resumes = [path.read_text(encoding='utf-8') for path in RESUMES]
    passes = []
    with tempfile.TemporaryDirectory() as temp:
        config = Settings(key='baseline-not-a-key', cache_path=Path(temp) / 'decisions.sqlite3')
        for label, resume in (('resume_a_cold', resumes[0]), ('resume_b_warm', resumes[1]), ('resume_a_warm', resumes[0])):
            endpoint = FakeJev()
            JevFitService(config, transport=httpx.MockTransport(endpoint)).analyze_many(resume_text=resume, jobs=jobs)
            stages = {}
            for req in endpoint.requests:
                stage = next(iter({STAGE_OF.get(k, 'unclassified') for k in req['kinds']}))
                row = stages.setdefault(stage, {'requests': 0, 'bytes': 0, 'questions': 0})
                row['requests'] += 1
                row['bytes'] += req['size']['bytes']
                row['questions'] += req['question_count']
            passes.append({'pass': label, 'requests': len(endpoint.requests),
                           'bytes': sum(r['size']['bytes'] for r in endpoint.requests), 'by_stage': stages})
    report = {'label': args.label, 'kind': 'scoring-cache', 'jobs': len(jobs), 'passes': passes}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(passes, indent=1))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    s = sub.add_parser('scoring')
    s.add_argument('--out', required=True)
    s.add_argument('--snapshots', default=str(SNAPSHOTS))
    s.add_argument('--label', default='current')
    s.add_argument('--repeat', type=int, default=3)
    c = sub.add_parser('scoring-cache')
    c.add_argument('--out', required=True)
    c.add_argument('--snapshots', default=str(SNAPSHOTS))
    c.add_argument('--label', default='current')
    args = parser.parse_args()
    if args.command == 'scoring':
        scoring(args)
    elif args.command == 'scoring-cache':
        scoring_cache(args)


if __name__ == '__main__':
    main()
