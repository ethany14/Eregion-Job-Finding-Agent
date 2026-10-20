"""Production routing/cache contracts; can also run directly with unittest."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from eregion.api.services.fit_analysis_service import FitAnalysisService
from eregion.domain.jev_fit.service import JevFitError
from eregion.domain.jev_fit.settings import Settings


class NoMainModel:
    def execute(self, *_):
        raise AssertionError('The main model must not run when Jev is enabled')


class JevEndpoint:
    """Deterministic provider IO, not a quality benchmark."""
    def __init__(self):
        self.requests = []
        self.failure = None
        self.unknown = False

    def __call__(self, request):
        payload = json.loads(request.content)
        self.requests.append(payload)
        if self.failure == 'timeout':
            raise httpx.ReadTimeout('injected', request=request)
        if self.failure == 'http':
            return httpx.Response(503)
        answers = {}
        for key, question in payload['questions'].items():
            args = question['instructions']
            prompt = args['question']
            probability = .99
            if prompt.startswith('Does this span'):
                probability = .99 if args['span'].strip().startswith(('Required:', 'Preferred:')) else .01
            elif prompt.startswith('Is this candidate'):
                probability = .99 if args['span'].strip().startswith('Preferred:') else .01
            elif prompt.startswith('Is `requirement`'):
                probability = .01
            elif self.unknown:
                probability = .5
            elif 'Kubernetes' in args.get('requirement', args.get('subject', '')):
                probability = .01
            answers[key] = {'type': 'noul', 'noul': probability}
        if self.failure == 'malformed':
            answers = {}
        return httpx.Response(200, json={'model': 'jev-0.0.1' if self.failure == 'version' else payload['model'],
            'answers': answers, 'usage': {'prompt_tokens': 100, 'completion_tokens': 10}})


class JevProductionTests(unittest.TestCase):
    resume = 'Candidate Example\nEXPERIENCE\nEngineer | Example Lab | 2023-01 | 2026-01\nBuilt Python tools.\nSKILLS\nPython, SQL\n'
    jd = 'Engineer\nRequired: Python\nPreferred: Kubernetes\n'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Settings(key='test-only', cache_path=Path(self.temp.name)/'decisions.sqlite3')
        self.endpoint = JevEndpoint()

    def tearDown(self):
        self.temp.cleanup()

    def service(self, **kwargs):
        return FitAnalysisService(NoMainModel(), jev_enabled=True,
            jev_config=kwargs.pop('config', self.config),
            jev_transport=httpx.MockTransport(self.endpoint), **kwargs)

    def analyze(self, service=None, **kwargs):
        return (service or self.service())._analyze_sync(
            resume_text=kwargs.pop('resume_text', self.resume),
            job_description=kwargs.pop('job_description', self.jd), **kwargs)

    def test_enabled_routes_only_jev_and_persists_warm_cache(self):
        with patch.dict(os.environ, {'TYPESAFE_ENABLED': 'true'}):
            service = self.service()
            service._jev_enabled = None  # Exercise the actual environment switch.
            cold = self.analyze(service)
        self.assertEqual(cold.skill_match.algorithm, 'jev-only')
        self.assertEqual(cold.main_model_calls, 0)
        self.assertGreater(cold.fast_model_calls, 0)
        with patch('eregion.domain.jev_fit.transport.httpx.Client', side_effect=AssertionError('Warm cache opened HTTP')):
            warm = self.analyze()  # New service, same persistent cache.
        self.assertEqual(warm.model_calls, 0)
        self.assertEqual(cold.skill_match.overall_score, warm.skill_match.overall_score)
        self.assertEqual(len(self.endpoint.requests), cold.fast_model_calls)

    def test_disabled_routes_legacy_without_jev_key(self):
        from test_fit_analysis_service import AnalysisOnlyHandler
        handler = AnalysisOnlyHandler()
        service = FitAnalysisService(handler, jev_config=replace(self.config, key=''),
            jev_transport=httpx.MockTransport(self.endpoint))
        # Routing contract only: opt into the legacy free extraction explicitly.
        with patch.dict(os.environ, {'TYPESAFE_ENABLED': 'false', 'JOB_AGENT_MAIN_CATALOG': 'extract'}):
            result = self.analyze(service)
        self.assertEqual(result.skill_match.algorithm, 'legacy')
        self.assertEqual(result.main_model_calls, 3)
        self.assertEqual(self.endpoint.requests, [])

    def test_batch_shares_requests_and_reuses_single_cache(self):
        service = self.service()
        jobs = [{'id': str(i), 'source_version': str(i), 'job_description': self.jd} for i in range(3)]
        results = asyncio.run(service.analyze_many(resume_text=self.resume, jobs=jobs))
        paid = len(self.endpoint.requests)
        self.assertEqual(set(results), {'0', '1', '2'})
        self.assertEqual(sum(r.fast_model_calls for r in results.values()), paid)
        self.assertLessEqual(paid, 5)  # Shared exact questions, not three full runs.
        self.assertEqual(sum(r.decision_routing[0]['input_tokens'] for r in results.values()), paid*100)
        self.assertEqual(self.analyze().model_calls, 0)  # Different application ID is irrelevant.

    def test_cache_invalidates_resume_jd_facts_and_model(self):
        self.analyze()
        for kwargs in ({'resume_text': self.resume+'Updated source.'},
                       {'job_description': self.jd+'Required: SQL\n'},
                       {'candidate_facts': {'country': 'Canada'}}):
            self.assertGreater(self.analyze(**kwargs).model_calls, 0)
        model = self.service(config=replace(self.config, model='jev-1.13.1'))
        self.assertGreater(self.analyze(model).model_calls, 0)

    def test_distinct_jobs_batch_without_borrowing_document_context(self):
        original=self.endpoint.__call__
        def endpoint(request):
            payload=json.loads(request.content)
            if 'cases' in payload['state']:
                for q in payload['questions'].values():
                    args=q['instructions'];own=payload['state']['cases'][args['context_case']]
                    if 'span' in args:
                        self.assertIn(args['span'],own['job_description'])
            return original(request)
        service=self.service();service._jev_transport=httpx.MockTransport(endpoint)
        jobs=[{'id':'python','job_description':'Engineer\nRequired: Python\n'},
              {'id':'kubernetes','job_description':'Engineer\nRequired: Kubernetes\n'}]
        values=asyncio.run(service.analyze_many(resume_text=self.resume,jobs=jobs))
        self.assertTrue(any('cases' in p['state'] for p in self.endpoint.requests))
        self.assertLessEqual(len(self.endpoint.requests),5)
        self.assertGreater(values['python'].skill_match.overall_score,values['kubernetes'].skill_match.overall_score)
        self.assertEqual(sum(v.fast_model_calls for v in values.values()),len(self.endpoint.requests))
        # A single-job retry reuses its own mapped decisions, never the other's.
        self.assertEqual(self.analyze(job_description=jobs[0]['job_description']).model_calls,0)

    def test_shared_requirement_reuses_same_resume_across_different_jobs(self):
        self.analyze(job_description='Engineer\nRequired: Python\n')
        before=len(self.endpoint.requests)
        self.analyze(job_description='Different Engineer\nRequired: Python\nRequired: SQL\n')
        sent=[q for p in self.endpoint.requests[before:] for q in p['questions'].values()]
        self.assertFalse(any(q['instructions'].get('requirement')=='Python' and
                             q['instructions']['question']=='Does the evidence support this requirement?' for q in sent))
        self.assertTrue(any(q['instructions'].get('requirement')=='SQL' for q in sent))

    def test_presentation_whitespace_reuses_decisions_but_keeps_original_spans(self):
        first=self.analyze()
        spaced=self.jd+'\n'
        second=self.analyze(job_description=spaced)
        self.assertEqual(second.model_calls,0)
        self.assertEqual(first.skill_match.overall_score,second.skill_match.overall_score)

    def test_uncertain_answers_score_by_probability_and_expire_by_config(self):
        # Uncertain evidence (p=.5) contributes its expected credit
        # .5 + (1-.5) x .5 x .5 = .625 instead of turning the score into a range.
        self.endpoint.unknown = True
        from unittest.mock import patch as patched
        service = self.service()
        result = self.analyze(service)
        self.assertEqual(result.status, 'completed')
        self.assertAlmostEqual(result.skill_match.overall_score, 62.5)
        self.assertEqual(result.skill_match.score_range, [62.5, 62.5])
        self.assertIn('certainty 0%', result.skill_match.explanation)
        self.assertEqual(self.analyze(service).model_calls, 0)
        with patched('time.time', return_value=result.skill_match.valid_until+1):
            repeated = self.analyze(service)
        self.assertGreater(repeated.model_calls, 0)

    def test_failed_malformed_wrong_model_and_timeout_never_fall_back_or_cache(self):
        for failure in ('http', 'malformed', 'version', 'timeout'):
            with self.subTest(failure=failure):
                self.endpoint.failure = failure
                before = len(self.endpoint.requests)
                with self.assertRaises(JevFitError):
                    self.analyze()
                self.assertEqual(len(self.endpoint.requests), before+1)
        self.endpoint.failure = None
        self.assertGreater(self.analyze().model_calls, 0)

    def test_empty_key_fails_without_any_provider_call(self):
        with self.assertRaisesRegex(ValueError, 'API_KEY'):
            self.analyze(self.service(config=replace(self.config, key='')))
        self.assertEqual(self.endpoint.requests, [])

    def test_source_projection_keeps_separate_owners_and_degree_status(self):
        from eregion.domain.jev_fit.projection import resume_projection
        source = ('EXPERIENCE\nEngineer | First Lab | 2022-01 | 2023-02\nBuilt Python tools.\n'
                  'Analyst | Second Lab | 2023-03 | 2024-04\nBuilt SQL reports.\n'
                  'PROJECT\nPublic data study | 2024-01 | 2024-03\nIndependent prototype.\n'
                  'EDUCATION\nBachelor of Science | Lakeview University | 2018-09 | 2022-05\n'
                  'Incomplete; left without graduating.\n'
                  'Master of Science | Westbridge Institute | 2026-08 | 2027-05\n'
                  'Currently enrolled; expected graduation May 2027.\n')
        parsed = resume_projection(source)
        quotes = {e.evidence_id:e.exact_text for e in parsed.evidence}
        owners = {e.organization:' '.join(quotes[i]for i in e.evidence_ids) for e in parsed.source_entries if e.entry_type=='experience'}
        self.assertIn('Built Python tools.', owners['First Lab'])
        self.assertNotIn('SQL', owners['First Lab'])
        self.assertIn('Built SQL reports.', owners['Second Lab'])
        project = next(e for e in parsed.source_entries if e.entry_type=='project')
        self.assertIsNone(project.organization)
        degrees = [e for e in parsed.source_entries if e.entry_type=='education']
        self.assertEqual(len(degrees), 2)
        self.assertIn('Incomplete', quotes[degrees[0].evidence_ids[0]])
        self.assertIn('expected', quotes[degrees[1].evidence_ids[0]])

    def test_rejected_budget_does_not_create_a_paid_ledger_entry(self):
        from eregion.runtime.coordination.model_usage import model_usage_scope
        from eregion.runtime.provider_usage import usage_scope
        class Reject:
            def reserve_model_call(self, *_, **kwargs): raise RuntimeError('budget exhausted')
        class Ledger:
            def start(self, *_): raise AssertionError('No provider call was made')
        with model_usage_scope(Reject(), 'bounded', 'attempt'), usage_scope(ledger=Ledger()):
            with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
                self.analyze()
        self.assertEqual(self.endpoint.requests, [])

    def test_accounting_context_crosses_worker_and_broker_threads(self):
        from eregion.api.db import create_database
        from eregion.runtime.provider_usage import UsageLedger, usage_scope
        from eregion.runtime.coordination.model_usage import model_usage_scope
        class Meter:
            def __init__(self): self.finished = []
            def reserve_model_call(self, task_id, attempt_id, **kwargs):
                assert (task_id, attempt_id) == ('unique-task', 'attempt')
                return {'index': len(self.finished)}
            def finish_model_call(self, task_id, attempt_id, reservation, **usage):
                self.finished.append(usage)
        db = create_database('sqlite:///'+(Path(self.temp.name)/'usage.db').as_posix(), create_schema_for_tests=True)
        meter, ledger = Meter(), UsageLedger(db.session_factory)
        try:
            with usage_scope(task_id='unique-task', ledger=ledger), model_usage_scope(meter, 'unique-task', 'attempt'):
                result = self.analyze()
            actual = ledger.summary(task_id='unique-task')
            self.assertEqual(actual['calls'], result.model_calls)
            self.assertEqual(actual['input_tokens'], 100*result.model_calls)
            self.assertEqual(len(meter.finished), result.model_calls)
            self.assertTrue(all(u == {'input_tokens':100,'output_tokens':10} for u in meter.finished))
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
