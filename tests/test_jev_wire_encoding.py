"""Jev wire encoding: repeated evidence values sent once per request, cited by reference, lossless."""
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from eregion.domain.jev_fit import core, transport
from eregion.domain.jev_fit.service import JevFitService
from eregion.domain.jev_fit.settings import Settings
from eregion.domain.jev_fit.transport import decode_wire, encode_wire

RESUME = ('Avery Example\nEXPERIENCE\nData Analyst | Example Co | 2023-01 | 2026-01\n'
          'Built SQL dashboards and Python pipelines for operations reporting.\nSKILLS\nPython, SQL, Tableau\n')
JD = ('Data Analyst\nRequirements:\n- Two or more years of experience writing production SQL for analytics teams.\n'
      '- Experience with Python data pipelines or a similar scripting language for reporting.\n'
      '- Bachelor degree in statistics, economics, computer science or a related quantitative field.\n'
      'Preferred:\n- Familiarity with Tableau or another business intelligence tool used by stakeholders.\n'
      '- Authorization to work in the United States without employer sponsorship now or later.\n'
      'Benefits:\n- Health insurance and a generous paid time off policy for every employee.\n')


class Endpoint:
    """Answers each complete question from a hash of its sentence and evidence; records wire payloads."""

    def __init__(self):
        self.payloads = []

    def __call__(self, request):
        payload = json.loads(request.content)
        self.payloads.append(payload)
        state, questions = decode_wire(payload['state'], payload['questions'])
        answers = {}
        for key, q in questions.items():
            ins = dict(q['instructions'])
            ins.pop('context_case', None)
            ins.pop('context_rule', None)
            digest = int(hashlib.sha256(json.dumps([ins, q.get('criteria')], sort_keys=True).encode()).hexdigest(), 16)
            answers[key] = {'type': 'noul', 'noul': (.96, .04, .5)[digest % 3]}
        return httpx.Response(200, json={'model': payload['model'], 'answers': answers,
                                         'usage': {'prompt_tokens': 1, 'completion_tokens': 1}})


def refs(value, out):
    if isinstance(value, dict):
        if set(value) == {'$ref'}:
            out.add(value['$ref'])
        for v in value.values():
            refs(v, out)
    elif isinstance(value, list):
        for v in value:
            refs(v, out)
    return out


def analyze(tmp_path, jobs, name='decisions.sqlite3', endpoint=None):
    endpoint = endpoint or Endpoint()
    service = JevFitService(Settings(key='test-only', cache_path=tmp_path / name), transport=httpx.MockTransport(endpoint))
    results = service.analyze_many(resume_text=RESUME, jobs=jobs)
    keep = {k: (v['skill_match'].model_dump(mode='json'), v['job_analysis'].model_dump(mode='json')) for k, v in results.items()}
    for match, _ in keep.values():
        match.pop('valid_until', None)
        match.pop('analysis_id', None)
    return keep, endpoint


def extraction_questions(jd):
    captured = {}

    class Client:
        task_id = 'local'

        def evaluate(self, state, questions, purpose):
            captured.setdefault(purpose, (state, questions))
            return {}, ['stop']
    core.JevFit(Client()).extract(jd)
    return captured['extract_requirements']


def test_encoding_is_lossless_and_shares_only_repeated_evidence():
    state, questions = extraction_questions(JD)
    wire_state, wire_questions = encode_wire(state, questions)
    assert decode_wire(wire_state, wire_questions) == (state, questions)
    shared = wire_state['shared_values']
    cited = refs(wire_questions, set())
    assert cited == set(shared)  # every reference resolves in this request, and nothing unused is carried
    # Each span is asked twice (requirement, preference): sent once. The question sentence stays inline.
    for key, q in wire_questions.items():
        assert isinstance(q['instructions']['question'], str)
        if len(questions[key]['instructions']['span']) >= transport.SHARED_MINIMUM:
            assert q['instructions']['span'] == {'$ref': q['instructions']['span']['$ref']}
    assert len(json.dumps([wire_state, wire_questions])) < len(json.dumps([state, questions]))
    # A value that occurs once is not moved.
    single_state, single = encode_wire({'policy': 'p'}, {'q0': core.yesno('One?', requirement='x' * 60)})
    assert single == {'q0': core.yesno('One?', requirement='x' * 60)} and 'shared_values' not in single_state


def test_split_batches_are_self_contained_and_decide_the_same(tmp_path, monkeypatch):
    jobs = [{'id': 'a', 'job_description': JD}, {'id': 'b', 'job_description': JD.replace('Data Analyst', 'Reporting Analyst', 1)}]
    plain_transport = lambda s, q: (s, q)  # the earlier inline protocol
    monkeypatch.setattr(transport, 'encode_wire', plain_transport)
    inline, _ = analyze(tmp_path, jobs, 'inline.sqlite3')
    monkeypatch.undo()
    monkeypatch.setattr(transport, 'MAX_QUESTIONS', 5)  # force many requests per stage
    encoded, endpoint = analyze(tmp_path, jobs, 'encoded.sqlite3')
    assert encoded == inline
    assert len(endpoint.payloads) > 7
    for payload in endpoint.payloads:
        assert refs(payload['questions'], set()) <= set(payload['state'].get('shared_values', {}))
        # Questions of several jobs keep their own case; shared values are question text, not case facts.
        for q in payload['questions'].values():
            if 'cases' in payload['state']:
                assert q['instructions']['context_case'] in payload['state']['cases']


def test_cache_key_versions_the_wire_protocol(tmp_path, monkeypatch):
    jobs = [{'id': 'a', 'job_description': JD}]
    first, endpoint = analyze(tmp_path, jobs)
    assert endpoint.payloads
    warm, endpoint = analyze(tmp_path, jobs)
    assert warm == first and endpoint.payloads == []  # same protocol: every decision reused
    monkeypatch.setattr(transport, 'WIRE_PROTOCOL', 'jev-wire-earlier')
    _, endpoint = analyze(tmp_path, jobs)
    assert endpoint.payloads  # another protocol never hits these entries


def test_payload_limit_applies_to_the_encoded_request(monkeypatch):
    state, questions = extraction_questions(JD)
    wire_state, wire_questions = encode_wire(state, questions)
    encoded = len(json.dumps({'model': 'jev-1.13.0', 'state': wire_state, 'questions': wire_questions}, ensure_ascii=False).encode())
    plain = len(json.dumps({'model': 'jev-1.13.0', 'state': state, 'questions': questions}, ensure_ascii=False).encode())
    assert encoded < plain
    broker = transport.Broker.__new__(transport.Broker)
    broker.config, broker.records, broker.max_requests = Settings(key='test-only'), [], 10
    monkeypatch.setattr(transport, 'MAX_PAYLOAD_BYTES', encoded - 1)
    assert broker._request(state, questions, 'extract_requirements', None)[1] == 'payload_limit'
