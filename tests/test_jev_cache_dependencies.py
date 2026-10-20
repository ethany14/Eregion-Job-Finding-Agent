"""Jev decision cache keys follow each stage's real inputs: posting stages survive a resume change."""
import hashlib
import json

import httpx

from eregion.domain.jev_fit import cache as cache_module
from eregion.domain.jev_fit.service import JevFitService
from eregion.domain.jev_fit.settings import Settings

RESUME = ('Avery Example\nEXPERIENCE\nData Analyst | Example Co | 2023-01 | 2026-01\n'
          'Built SQL dashboards and Python pipelines for operations reporting.\nSKILLS\nPython, SQL, Tableau\n')
JD = ('Data Analyst\nRequirements:\n- Two or more years of experience writing production SQL for analytics teams.\n'
      '- Experience with Python data pipelines or a similar scripting language for reporting.\n'
      '- Bachelor degree in statistics, economics, computer science or a related quantitative field.\n'
      'Preferred:\n- Familiarity with Tableau or another business intelligence tool used by stakeholders.\n'
      '- Authorization to work in the United States without employer sponsorship now or later.\n'
      'Benefits:\n- Health insurance and a generous paid time off policy for every employee.\n')
OTHER_RESUME = RESUME.replace('Python pipelines', 'Java services').replace('Tableau', 'Looker')
POSTING_STAGES = {'Does this span state', 'Is this candidate qualification', 'Does this span specify',
                  'Does this qualification explicitly', 'Is `requirement` a condition', 'Does the requirement ask for personal'}


class Endpoint:
    """Answers each question from a hash of its sentence and evidence; records request payloads."""

    def __init__(self):
        self.payloads = []

    def __call__(self, request):
        payload = json.loads(request.content)
        self.payloads.append(payload)
        answers = {}
        for key, q in payload['questions'].items():
            ins = dict(q['instructions'])
            ins.pop('context_case', None)
            ins.pop('context_rule', None)
            digest = int(hashlib.sha256(json.dumps([ins, q.get('criteria')], sort_keys=True).encode()).hexdigest(), 16)
            answers[key] = {'type': 'noul', 'noul': (.96, .04, .5)[digest % 3]}
        return httpx.Response(200, json={'model': payload['model'], 'answers': answers,
                                         'usage': {'prompt_tokens': 1, 'completion_tokens': 1}})



def stages(endpoint):
    """Which judgments each request asked, by question sentence."""
    asked = []
    for payload in endpoint.payloads:
        asked.extend(q['instructions']['question'] for q in payload['questions'].values())
    return asked


def posting(questions):
    return [q for q in questions if q.startswith(tuple(POSTING_STAGES))]


def run(path, resume=RESUME, jd=JD, owner='local', facts=None, **settings):
    endpoint = Endpoint()
    service = JevFitService(Settings(key='test-only', cache_path=path, **settings), owner=owner, transport=httpx.MockTransport(endpoint))
    results = service.analyze_many(resume_text=resume, jobs=[{'id': 'job', 'job_description': jd}], candidate_facts=facts)
    match = results['job']['skill_match'].model_dump(mode='json')
    for key in ('valid_until', 'analysis_id'):
        match.pop(key, None)
    return json.dumps([match, results['job']['job_analysis'].model_dump(mode='json')], sort_keys=True), stages(endpoint)


def test_a_resume_change_reuses_posting_decisions_and_asks_every_matching_one_again(tmp_path):
    cold, asked = run(tmp_path / 'warm.sqlite3')
    assert posting(asked) and len(asked) > len(posting(asked))
    warm_b, asked_b = run(tmp_path / 'warm.sqlite3', resume=OTHER_RESUME)
    assert posting(asked_b) == [] and asked_b  # matching stages are asked again for the new resume
    fresh_b, _ = run(tmp_path / 'fresh.sqlite3', resume=OTHER_RESUME)
    assert warm_b == fresh_b  # reuse changes nothing about the result
    assert run(tmp_path / 'warm.sqlite3')[1] == []  # the first resume is fully cached


def test_posting_changes_owners_and_facts_invalidate_what_they_should(tmp_path):
    path = tmp_path / 'decisions.sqlite3'
    run(path)
    _, other_jd = run(path, jd=JD.replace('production SQL', 'production SQL and dbt'))
    assert posting(other_jd)  # a changed posting is extracted again
    _, other_owner = run(path, owner='someone-else')
    assert posting(other_owner) and len(other_owner) == len(run(tmp_path / 'cold.sqlite3')[1])  # nothing shared across owners
    _, other_facts = run(path, facts={'work_authorization': 'US citizen'})
    assert posting(other_facts) == [] and other_facts  # facts change matching, not the posting's requirements


def test_unknown_decisions_expire_on_their_own_ttl(tmp_path, monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(cache_module.time, 'time', lambda: now[0])
    path = tmp_path / 'decisions.sqlite3'
    _, first = run(path, unknown_ttl=60)
    now[0] += 120  # past the unknown TTL, well inside the decided TTL
    _, again = run(path, resume=OTHER_RESUME, unknown_ttl=60)
    assert 0 < len(posting(again)) < len(posting(first))  # only the undecided posting questions are asked again
