"""E3 bounded real-model replay of task-memory recovery (user-approved 2026-10-17).

Only the career agent calls the real model. Jobs, scoring, materials and qualification use the
same free fakes as tests/test_career_flow.py; the application runs in autofill mode against a
fake browser, so no application website is contacted and nothing can be submitted for real.
Every run starts from a byte copy of one fixed base database. Caps are enforced by the
provider layer through the experiment ledger (all runs) and the task ledger (each run).

Usage: python tools/scripts/task_memory_replay_pilot.py OUT_DIR [--dry]
--dry uses a scripted model (no provider calls) to prove only each scenario task is driven.

Run 01 (2026-10-17) left the setup conversations queued in the base snapshot; every run's TaskLoop
drove them with the real model and they used 20 of the 30 attempts. The base now settles them and
each run refuses to start if any other conversation is queued or running.
"""
import json, os, shutil, sys, time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(sys.argv[1]).resolve()
DRY = '--dry' in sys.argv
OUT.mkdir(parents=True, exist_ok=True)
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'tests')]
os.chdir(ROOT)
os.environ.update(JOB_AGENT_JOB_API_FIRST='0', TYPESAFE_ENABLED='false', JOB_AGENT_ANALYSIS_WARMUP='0')
if not DRY:
    os.environ.update(
        JOB_AGENT_EXPERIMENT_ID=os.getenv('PILOT_EXPERIMENT_ID', 'task-memory-replay-20261017'),
        JOB_AGENT_EXPERIMENT_LEDGER=str(OUT / 'experiment.sqlite3'),
        JOB_AGENT_EXPERIMENT_MAX_PROVIDER_ATTEMPTS=os.getenv('PILOT_MAX_ATTEMPTS', '30'),
        JOB_AGENT_EXPERIMENT_MAX_INPUT_TOKENS=os.getenv('PILOT_MAX_INPUT_TOKENS', '600000'),
        JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS='8', JOB_AGENT_TASK_MAX_INPUT_TOKENS='160000')

from eregion.api.db import create_database, upgrade_database
from eregion.api.services.fit_analysis_service import FitAnalysisService
from eregion.domain.workflow.handlers import JobAgentStepHandler
from eregion.domain.pdf_rendering import render_letter_pdf
from eregion.runtime.tasks.loop import TaskLoop
from eregion.runtime.career.repository import TaskRepository
from eregion.runtime.career.harness import CareerHarness
from eregion.runtime.auto_apply.repository import ApplyRepository
from eregion.runtime.auto_apply.loop import ApplicationLoop
from eregion.runtime.application_pack.repository import PackRepository
from eregion.runtime.application_pack.workflow import ApplicationPackWorkflow
from eregion.runtime.context.repository import ContextSnapshotRepository
from eregion.runtime.evidence.repository import CareerEvidenceRepository
from eregion.runtime.memory.repository import MemoryRepository
from eregion.runtime.memory.policy import LocalOwnerResolver
from eregion.runtime.memory.access import MemoryAccessResolver
from eregion.runtime.mock_interview.controller import MockInterviewController
from eregion.runtime.mock_interview.repository import MockInterviewRepository
from eregion.runtime.sessions.repository import SessionRepository
from eregion.runtime.workspace.repository import JobWorkspaceRepository
from eregion.runtime.task_memory.service import TaskMemoryService
from eregion.runtime.task_memory.types import TaskMemoryConfig
from eregion.runtime.provider_usage import UsageLedger, experiment_summary
from eregion.runtime.career import qualification
from fake_search_qualification import judge
from test_application_pack import FakePackModel
from test_mock_interview import ScriptedModel as InterviewModel
from test_auto_apply import FakeBrowser, wait
from test_career_flow import Discovery, fake_analyze, prepare, approve, execute

qualification.judge = judge


def environment(db_path, mode, app=None):
    url = f"sqlite:///{db_path.as_posix()}"
    upgrade_database(url)
    db = create_database(url)
    workspace = JobWorkspaceRepository(db.session_factory)
    evidence = CareerEvidenceRepository(db.session_factory)
    packs = PackRepository(db.session_factory)
    memories = MemoryRepository(db.session_factory)
    workflow = ApplicationPackWorkflow(packs=packs, workspace=workspace, evidence=evidence, memories=memories, model=FakePackModel())
    interviews = MockInterviewController(interviews=MockInterviewRepository(db.session_factory), sessions=SessionRepository(db.session_factory),
        workspace=workspace, packs=packs, evidence=evidence, context_snapshots=ContextSnapshotRepository(db.session_factory), model=InterviewModel())
    owner = LocalOwnerResolver()
    runtime = SimpleNamespace(database=db, workspace=workspace, evidence=evidence, packs=packs, pack_workflow=workflow,
        mock_interviewer=interviews, owner_resolver=owner, memory_access=MemoryAccessResolver(owner), memories=memories)
    service = TaskMemoryService(runtime, TaskMemoryConfig(mode, frozenset({app}) if app else frozenset()))
    runtime.task_memory = service
    repository = TaskRepository(db.session_factory, task_memory=service.repository)
    browsers = []
    def browser(url):
        instance = FakeBrowser(url); browsers.append(instance); return instance
    applications = ApplicationLoop(ApplyRepository(db.session_factory), browser_factory=browser)
    real_start = applications.start
    applications.start = lambda request, **kw: real_start(request.model_copy(update={'automation_mode': 'autofill'}), **kw)
    harness = CareerHarness(runtime, applications, repository, discovery=Discovery(),
        fit_service=FitAnalysisService(JobAgentStepHandler(analyzer=fake_analyze)))
    return SimpleNamespace(db=db, runtime=runtime, repository=repository, applications=applications, harness=harness, browsers=browsers)


def build_base(path):
    """Fixed saved state: one approved job, its original conversation and a run at Review."""
    env = environment(path, 'on')
    original = render_letter_pdf('Synthetic Candidate\nsynthetic@example.com\nEXPERIENCE\nBuilt Python APIs.')
    env.applications.repository.save_candidate({'first_name': 'Synthetic', 'last_name': 'Candidate', 'email': 'synthetic@example.com',
        'phone': '607-555-0100'}, resume=original, name='original.pdf', extracted=True)
    seed, _ = env.repository.create('Find Python jobs and prepare materials.', 'seed')
    env.state = seed
    job, pack = prepare(env); approve(env, pack)
    app = job['application_id']
    env.runtime.task_memory = TaskMemoryService(env.runtime, TaskMemoryConfig('on', frozenset({app})))
    env.repository = TaskRepository(env.db.session_factory, task_memory=env.runtime.task_memory.repository)
    env.harness.repository = env.repository
    owner_task, _ = env.repository.create('Apply to this Python job with my tailored resume and stop at review. '
        'Keep my resume headline as "Backend Engineer".', 'original', context={'application_id': app})
    env.state = owner_task
    run = execute(env, 'start_application', application_id=app, pack_id=pack['pack_id'])
    run = wait(env.applications, run['run_id'])
    assert run['status'] == 'awaiting_review', run['status']
    # Setup conversations must not be driven by a later TaskLoop (run 01's defect).
    for task_id in (seed['task_id'], owner_task['task_id']):
        env.repository.change(task_id, status='completed', wait=None, input_request=None)
    env.applications.close(); env.db.close()
    return {'application_id': app, 'owner_task': owner_task['task_id'], 'run_id': run['run_id']}


SCENARIOS = {
    'new_conversation': ('new', 'Continue my application for this job.'),
    'correction': ('owner', 'Correction: my phone number is 607-555-0199, not 607-555-0100. Update the application with it and continue.'),
    'budget_exhausted': ('owner', 'What is the status of my application?'),
}


def run_one(base, info, scenario, mode):
    path = OUT / f'{scenario}-{mode}.sqlite3'
    shutil.copyfile(base, path)
    env = environment(path, mode, info['application_id'])
    app, run_id = info['application_id'], info['run_id']
    if scenario == 'budget_exhausted':
        current = env.applications.repository.get(run_id)
        env.applications.repository.save(current, status='needs_input', agent_reason='application_budget',
            message='This application reached its provider budget. Completed fields are saved; allow more to continue.')
    run_before = env.applications.repository.get(run_id)
    busy = [t for t in env.repository.list() if t['status'] in {'queued', 'running', 'waiting'}]
    if busy:
        raise RuntimeError(f'Other conversations are active ({len(busy)}); refusing to start a paid run.')
    if DRY:
        from test_career_flow import ToolModel
        loop = TaskLoop(env.repository, env.harness, model_factory=lambda: ToolModel(
            lambda snapshot: ('finish_task', {'text': 'Dry run: the application is saved; nothing was submitted.'})))
    else:
        loop = TaskLoop(env.repository, env.harness)
    started = time.monotonic()
    try:
        where, text = SCENARIOS[scenario]
        if where == 'new':
            task = loop.start(text, f'{scenario}-{mode}', context={'application_id': app})
        else:
            loop.chat(info['owner_task'], text, f'{scenario}-{mode}-msg', context={'application_id': app})
            task = env.repository.get(info['owner_task'])
        task_id = task['task_id']
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            state = env.repository.get(task_id)
            child = env.applications.repository.get(run_id)
            if state['status'] in {'completed', 'needs_input'} and child['status'] not in {'queued', 'running'}:
                break
            if state['status'] == 'waiting' and child['status'] not in {'queued', 'running'} and time.monotonic() - started > 30:
                break
            time.sleep(.5)
        elapsed = time.monotonic() - started
        state = env.repository.get(task_id)
        child = env.applications.repository.get(run_id)
        usage = UsageLedger(env.repository.sessions).summary(task_id=task_id)
        from sqlalchemy import text as sql
        with env.db.session_factory() as session:
            runs = session.execute(sql('select count(*) from auto_applications')).scalar()
        new_messages = [m for m in state['messages'] if m['role'] == 'assistant'][-3:]
        history = [h['tool'] for h in state.get('history', []) if h.get('chat_revision', 0) >= 0][-14:]
        result = {
            'scenario': scenario, 'mode': mode, 'task_status': state['status'], 'elapsed_s': round(elapsed, 1),
            'provider_attempts': usage['calls'], 'input_tokens': usage.get('input_tokens'), 'output_tokens': usage.get('output_tokens'),
            'budget_input_tokens': usage.get('budget_input_tokens'), 'unknown_usage': usage.get('unknown_usage_calls'),
            'tools': history, 'final_messages': [m['text'][:700] for m in new_messages],
            'run_status_before': run_before['status'], 'run_status_after': child['status'],
            'run_version_changed': child['version'] != run_before['version'], 'run_count': runs,
            'submissions': sum(b.submissions for b in env.browsers), 'browsers_opened': len(env.browsers),
            'correction_delivered': any('607-555-0199' in (m.get('text') or '') for m in child.get('messages', [])),
            'task_memory_injected': mode == 'on',
        }
    finally:
        loop.close(); env.applications.close(); env.db.close()
    (OUT / f'{scenario}-{mode}.json').write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding='utf-8')
    return result


if __name__ == '__main__':
    base = OUT / 'base.sqlite3'
    if base.exists(): raise SystemExit('Fresh output directory required.')
    info = build_base(base)
    (OUT / 'base.json').write_text(json.dumps(info, indent=1), encoding='utf-8')
    results = []
    for scenario in SCENARIOS:
        for mode in ('off', 'on'):
            try:
                results.append(run_one(base, info, scenario, mode))
            except BaseException as exc:  # a cap stop or failure is reported, not retried
                results.append({'scenario': scenario, 'mode': mode, 'error': f'{type(exc).__name__}: {str(exc)[:300]}'})
            print(json.dumps(results[-1], ensure_ascii=False)[:1500], flush=True)
    total = None if DRY else experiment_summary(os.environ['JOB_AGENT_EXPERIMENT_LEDGER'], os.environ['JOB_AGENT_EXPERIMENT_ID'])
    if not DRY:
        # Every attempt must belong to a scenario task; anything else invalidates the comparison.
        scenario_calls = sum(r.get('provider_attempts', 0) for r in results)
        total = {**total, 'scenario_task_attempts': scenario_calls, 'unattributed_attempts': total['calls'] - scenario_calls}
    (OUT / 'summary.json').write_text(json.dumps({'runs': results, 'experiment': total}, indent=1, ensure_ascii=False), encoding='utf-8')
    print('EXPERIMENT', json.dumps(total))
