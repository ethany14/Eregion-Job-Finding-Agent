"""Free request-size baseline for the career (main agent) loop: production TaskLoop, scripted model.

A temporary database, the production CareerService/TaskLoop/harness and the default source order
(API first) run one search conversation over frozen public postings: search_jobs, verify_and_score_jobs
for 12 cards, score_job for each imported job, three recommendations, finish. Discovery, qualification and fit scoring are offline fakes
from the test suite; only the model is replaced, and it records the request the production code built.
Nothing reaches a provider or a real site.

Reports characters and UTF-8 bytes only (no token estimate). Outputs hold sizes and hashes, never text.

Usage: token_baseline_career.py --out FILE [--label NAME]
"""
import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'tests'), str(ROOT / 'tools' / 'scripts')]
from token_baseline import SNAPSHOTS, duplicate_bytes, no_network, sha, size  # noqa: E402

JOBS = 12
REQUEST = 'Find junior data analyst jobs in New York, NY and recommend the best fits for my resume.'


def frozen_cards():
    data = json.loads((SNAPSHOTS / 'snapshot-A.json').read_text(encoding='utf-8'))
    rows = [r for r in data['queries'][0]['sources']['boards']['rows'] if len(r.get('description') or '') > 500]
    return [{'url': r['url'], 'title': r.get('title') or '', 'company': r.get('company') or '', 'location': r.get('location') or '',
             'description': r['description']} for r in rows[:JOBS]]


class Discovery:
    def __init__(self, cards):
        self.cards = {c['url']: c for c in cards}

    def search_directory(self, query, location='', limit=10, *, cancelled=None, **_):
        jobs = [{k: c[k] for k in ('url', 'title', 'company', 'location')} for c in self.cards.values()]
        return {'jobs': jobs, 'errors': [], 'sources_checked': [], 'skipped_sources': [], 'board_outcomes': []}

    def search(self, *a, **k):
        return {'jobs': [], 'errors': [], 'sources_checked': [], 'skipped_sources': []}

    def job(self, url):
        return dict(self.cards[url])

    def close(self):
        pass


def measure(messages, tools):
    from langchain_core.messages import HumanMessage, SystemMessage
    live_index = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage) and isinstance(m.content, str))
    live = json.loads(messages[live_index].content)
    system = ''.join(m.content for m in messages if isinstance(m, SystemMessage) and isinstance(m.content, str))
    history = [m.model_dump() for i, m in enumerate(messages) if i != live_index and not isinstance(m, SystemMessage)]
    request = {'messages': [m.model_dump() for m in messages], 'tools': tools}
    return {'request': size(request), 'system': size(system), 'tools': size(tools), 'tool_count': len(tools),
            'history': size(history), 'history_messages': len(history), 'live_snapshot': size(live),
            'snapshot_fields_bytes': {k: size(v)['bytes'] for k, v in live.items()},
            'duplicate_bytes': duplicate_bytes(request)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True)
    parser.add_argument('--label', default='current')
    args = parser.parse_args()
    no_network()
    os.environ['JOB_AGENT_JOB_API_FIRST'] = '1'
    os.environ['TYPESAFE_ENABLED'] = 'false'
    os.environ.pop('MCP_CONFIG_PATH', None)
    os.environ['MCP_CONFIG_PATH'] = ''
    from langchain_core.messages import AIMessage, HumanMessage
    from eregion.api.db import create_database, upgrade_database
    from eregion.api.services.fit_analysis_service import FitAnalysisService
    from eregion.domain.pdf_rendering import render_letter_pdf
    from eregion.domain.workflow.handlers import JobAgentStepHandler
    from eregion.runtime.application_pack.repository import PackRepository
    from eregion.runtime.application_pack.workflow import ApplicationPackWorkflow
    from eregion.runtime.auto_apply.loop import ApplicationLoop
    from eregion.runtime.auto_apply.repository import ApplyRepository
    from eregion.runtime.career import qualification
    from eregion.runtime.career.harness import CareerHarness
    from eregion.runtime.career.repository import TaskRepository
    from eregion.runtime.career.service import CareerService
    from eregion.runtime.context.repository import ContextSnapshotRepository
    from eregion.runtime.evidence.repository import CareerEvidenceRepository
    from eregion.runtime.memory.repository import MemoryRepository
    from eregion.runtime.mock_interview.controller import MockInterviewController
    from eregion.runtime.mock_interview.repository import MockInterviewRepository
    from eregion.runtime.sessions.repository import SessionRepository
    from eregion.runtime.workspace.repository import JobWorkspaceRepository
    from fake_search_qualification import judge
    from test_application_pack import FakePackModel
    from test_auto_apply import FakeBrowser
    from test_career_flow import fake_analyze, settled
    from test_mock_interview import ScriptedModel as InterviewModel
    qualification.judge = judge
    cards = frozen_cards()
    # The offline fit analyzer of the test suite cites this sentence, so it must occur in the resume.
    resume = ((ROOT / 'tests' / 'fixtures' / 'resume_source_regressions' / 'avery.txt').read_text(encoding='utf-8')
              + '\nEXPERIENCE\nBuilt Python APIs.\n')
    requests, scored_ids, tried = [], set(), set()

    def decide(snap):
        history = [h['tool'] for h in snap.get('history', [])]
        jobs = snap.get('jobs') or []
        recommended = {r.get('application_id') for r in snap.get('recommended_jobs') or []}
        if 'search_jobs' not in history and not jobs:
            return [('search_jobs', {'query': 'junior data analyst', 'location': 'New York, NY'})]
        if 'verify_and_score_jobs' not in history and not jobs:
            return [('verify_and_score_jobs', {'urls': [c['url'] for c in cards]})]
        # The script remembers what it scored; compact job rows need not repeat every score.
        unscored = [j['application_id'] for j in jobs if j['application_id'] not in scored_ids]
        if unscored:
            scored_ids.add(unscored[0])
            return [('score_job', {'application_id': unscored[0]})]
        scored = [j for j in jobs if j['application_id'] not in recommended and j['application_id'] not in tried]
        if len(tried) < 3 and scored:
            job = scored[0]
            tried.add(job['application_id'])
            return [('recommend_job', {'application_id': job['application_id'], 'tier': 'to_confirm',
                                       'reason': 'The resume shows SQL analytics and Python pipelines.', 'evidence': [job['title']],
                                       'gaps': [], 'to_confirm': ['Seniority and location fit the request.'],
                                       'next_steps': ['Read the posting and apply with the saved resume.'], 'caveats': 'Confirm level.'})]
        # The final reply follows the saved recommendations, as the reply check requires.
        saved = snap.get('recommended_jobs') or []
        lines = '\n'.join(f"- {r.get('title')}: {r.get('url')} Score {r.get('match_score')}. To confirm: seniority and location."
                          for r in saved)
        return [('finish_task', {'text': f'Needs your confirmation ({len(saved)})\n' + lines,
                                 'stop_reason': 'The observed candidates were scored; the saved ones are recommended for confirmation.'})]

    class Model:
        def invoke(self, messages, tools):
            requests.append(measure(messages, tools))
            snap = json.loads(next(m.content for m in reversed(messages) if isinstance(m, HumanMessage) and isinstance(m.content, str)))
            calls = decide(snap)
            return AIMessage(content='', tool_calls=[{'id': str(uuid4()), 'name': n, 'args': a} for n, a in calls])

    with tempfile.TemporaryDirectory() as temp:
        url = f"sqlite:///{(Path(temp) / 'career.db').as_posix()}"
        upgrade_database(url)
        db = create_database(url)
        workspace = JobWorkspaceRepository(db.session_factory)
        evidence = CareerEvidenceRepository(db.session_factory)
        packs = PackRepository(db.session_factory)
        workflow = ApplicationPackWorkflow(packs=packs, workspace=workspace, evidence=evidence,
                                           memories=MemoryRepository(db.session_factory), model=FakePackModel())
        interviews = MockInterviewController(interviews=MockInterviewRepository(db.session_factory),
            sessions=SessionRepository(db.session_factory), workspace=workspace, packs=packs, evidence=evidence,
            context_snapshots=ContextSnapshotRepository(db.session_factory), model=InterviewModel())
        runtime = SimpleNamespace(database=db, workspace=workspace, evidence=evidence, packs=packs,
                                  pack_workflow=workflow, mock_interviewer=interviews)
        repository = TaskRepository(db.session_factory)
        applications = ApplicationLoop(ApplyRepository(db.session_factory), browser_factory=lambda u: FakeBrowser(u))
        applications.repository.save_candidate({'first_name': 'Avery', 'last_name': 'Lin', 'email': 'avery.lin.qa@example.com'},
                                               resume=render_letter_pdf(resume), name='resume.pdf', extracted=True)
        harness = CareerHarness(runtime, applications, repository, discovery=Discovery(cards),
                                fit_service=FitAnalysisService(JobAgentStepHandler(analyzer=fake_analyze)))
        service = CareerService(runtime, applications, harness=harness, model_factory=lambda: Model(), start_alert_scheduler=False)
        try:
            task = service.start(REQUEST, str(uuid4()))
            state = settled(service.repository, task['run_id'], statuses=('needs_input', 'completed', 'waiting', 'failed'))
            deadline = time.monotonic() + 60
            while state['status'] not in ('completed', 'failed', 'needs_input') and time.monotonic() < deadline:
                time.sleep(.2)
                state = service.repository.get(task['run_id'])
            view = service.view(task['run_id'])
            outcome = {'status': state['status'], 'tools': [a['tool'] for a in state.get('actions', [])],
                       'recommendations': len(view.get('recommendations') or []), 'jobs': len(view.get('jobs') or [])}
        finally:
            service.close()
            applications.close()
            db.close()
    report = {'label': args.label, 'kind': 'career', 'units': 'characters and UTF-8 bytes; no token estimate',
              'inputs': {'cards': len(cards), 'card_urls_sha256': sha(json.dumps([c['url'] for c in cards])),
                         'resume_sha256': sha(resume)},
              'summary': {'requests': len(requests), 'total_request_bytes': sum(r['request']['bytes'] for r in requests),
                          'outcome': outcome},
              'requests': requests}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(report['summary'], indent=2))
    for i, r in enumerate(requests):
        top = sorted(r['snapshot_fields_bytes'].items(), key=lambda x: -x[1])[:6]
        print(i, r['request']['bytes'], 'sys', r['system']['bytes'], 'tools', r['tools']['bytes'], r['tool_count'],
              'hist', r['history']['bytes'], 'live', r['live_snapshot']['bytes'], top)


if __name__ == '__main__':
    main()
