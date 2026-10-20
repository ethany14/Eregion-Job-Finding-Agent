"""Real-model tailored resume and cover letter on synthetic inputs (user-approved 2026-10-20). One paid run per folder.

Inputs: the synthetic resume `avery.txt` with its saved analysis, and the frozen public JD `B0-jobspy_indeed`
with the job analysis and skill match Jev produced for this pair in T6 (inline arm, the current request
encoding). The production ApplicationPackWorkflow and SharedPackModel run on a new database in --out:
the tailored resume first, then the cover letter. Model settings come from --env (values never printed
or saved).

Caps, enforced before every send by the experiment ledger and the guard: 8 provider attempts, 150,000
input-token units (first run 2026-10-20: 6 / 80,000; the retest after the fixes was approved at 8 / 150,000) (pending requests hold their reservation, UTF-8 bytes + 1,024), 600 seconds. Reasoning
escalation and rejection retries are off; nothing is retried.

--only limits the run to one item; --max-attempts / --max-input lower the caps so several runs stay
inside one approved total.

Usage: material_live_check.py precheck|run --out DIR --env PATH_TO_.env [--only ITEM] [--max-attempts N] [--max-input N]
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'tools' / 'scripts')]

CAPS = {'provider_attempts': 8, 'input_tokens': 150_000, 'seconds': 600}
RESUME = ROOT / 'tests' / 'fixtures' / 'resume_source_regressions' / 'avery.txt'
ANALYSIS = ROOT / 'tests' / 'fixtures' / 'resume_source_regressions' / 'avery-analysis.json'
SCORED = ROOT / '.codex-runs' / 'token-efficiency' / 't6-scoring' / 'arm-inline.json'
JOB_ID, TITLE, COMPANY = 'B0-jobspy_indeed', 'Founding Engineer', 'David Joseph & Company'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['precheck', 'run'])
    parser.add_argument('--out', required=True)
    parser.add_argument('--env', required=True)
    parser.add_argument('--only', choices=['tailored_resume', 'cover_letter'])
    parser.add_argument('--max-attempts', type=int, default=CAPS['provider_attempts'])
    parser.add_argument('--max-input', type=int, default=CAPS['input_tokens'])
    args = parser.parse_args()
    if args.max_attempts > CAPS['provider_attempts'] or args.max_input > CAPS['input_tokens']:
        parser.error('caps can only be lowered')
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    from dotenv import dotenv_values
    os.environ.update({k: v for k, v in dotenv_values(args.env).items() if v is not None})
    ledger = out / 'experiment-ledger.sqlite3'
    experiment = f'material-check-{args.command}'
    os.environ.update(JOB_AGENT_REASONING_ESCALATION='0', JOB_AGENT_REASONING_REJECTION_RETRY='0',
                      JOB_AGENT_EXPERIMENT_ID=experiment, JOB_AGENT_EXPERIMENT_LEDGER=str(ledger),
                      # precheck: a zero-attempt cap refuses the first call before it is sent.
                      JOB_AGENT_EXPERIMENT_MAX_PROVIDER_ATTEMPTS=str(args.max_attempts if args.command == 'run' else 0),
                      JOB_AGENT_EXPERIMENT_MAX_INPUT_TOKENS=str(args.max_input))
    from run_guard import guarded, require
    with guarded(out / f'exit-{args.command}', deadline_s=CAPS['seconds']) as run:
        if args.command == 'run':
            with (out / 'paid-started.lock').open('x') as lock:  # one paid run per folder
                lock.write(time.strftime('%Y-%m-%dT%H:%M:%S'))
        from token_baseline import SNAPSHOTS, frozen_jobs
        from eregion.api.db import create_database, upgrade_database
        from eregion.domain.schemas import JobAnalysis, ResumeAnalysis, SkillMatch
        from eregion.runtime.application_pack.repository import PackRepository
        from eregion.runtime.application_pack.workflow import ApplicationPackWorkflow
        from eregion.runtime.evidence.repository import CareerEvidenceRepository
        from eregion.runtime.memory.repository import MemoryRepository
        from eregion.runtime.provider_usage import TaskProviderBudgetExceeded, experiment_summary, usage_scope
        from eregion.runtime.resumes.repository import ResumeDocumentRepository
        from eregion.runtime.workspace.repository import JobWorkspaceRepository
        import hashlib
        db_path = out / f'{args.command}.sqlite3'
        db_path.unlink(missing_ok=True)
        url = f'sqlite:///{db_path.as_posix()}'
        upgrade_database(url)
        db = create_database(url)
        jd = next(j['job_description'] for j in frozen_jobs(SNAPSHOTS) if j['id'] == JOB_ID)
        resume_text = RESUME.read_text(encoding='utf-8')
        scored = json.loads(SCORED.read_text(encoding='utf-8'))['results'][JOB_ID]
        workspace = JobWorkspaceRepository(db.session_factory)
        saved = workspace.save_workspace(cleaned_job_description=jd, title=TITLE, company=COMPANY)
        resumes = ResumeDocumentRepository(db.session_factory)
        document = resumes.create(owner_id='local-user', filename='avery.txt', display_name='Avery (synthetic)',
                                  content_sha256=hashlib.sha256(resume_text.encode()).hexdigest(),
                                  extracted_text=resume_text, page_count=1)
        application, _ = workspace.project_fit_analysis(
            saved.application.application_id, snapshot_id=saved.snapshot.snapshot_id, analysis_id='t6-inline',
            resume=ResumeAnalysis.model_validate_json(ANALYSIS.read_text(encoding='utf-8')),
            job=JobAnalysis.model_validate(scored['job']), match=SkillMatch.model_validate(scored['match']),
            expected_version=saved.application.version, resume_text=resume_text, resume_id=document.resume_id)
        packs = PackRepository(db.session_factory)
        workflow = ApplicationPackWorkflow(packs=packs, workspace=workspace,
                                           evidence=CareerEvidenceRepository(db.session_factory),
                                           memories=MemoryRepository(db.session_factory), resumes=resumes)
        pack = workflow.create(application.application_id, expected_version=application.version,
                               idempotency_key='material-check')
        snapshot = packs.snapshot(pack.pack_id)
        started = time.monotonic()
        items, stopped = {}, None
        for artifact in [args.only] if args.only else ('tailored_resume', 'cover_letter'):
            if stopped:
                break
            t0 = time.monotonic()
            error = None
            try:
                with usage_scope(purpose=f'material_check:{artifact}'):
                    workflow.generate_many(pack.pack_id, [artifact], idempotency_key=f'material-check:{artifact}')
            except (Exception, TaskProviderBudgetExceeded) as exc:  # a cap stop or error is recorded, not raised
                error = stopped = f'{type(exc).__name__}: {str(exc)[:300]}'
            item = next((i for i in packs.items(pack.pack_id) if i.artifact_type == artifact), None)
            items[artifact] = {
                'seconds': round(time.monotonic() - t0, 1), 'error': error,
                'status': str(item.status) if item else None,
                'revision_count': item.revision_count if item else None,
                'verification': item.verification.model_dump(mode='json') if item and item.verification else None,
                'versions': packs.versions(pack.pack_id, item.pack_item_id) if item else [],
                'artifact': packs.artifact(pack.pack_id, item.pack_item_id) if item else None}
        usage = experiment_summary(str(ledger), experiment) if ledger.exists() else {}
        (out / f'materials-{args.command}.json').write_text(json.dumps(
            {'evidence': [e.model_dump(mode='json') for e in snapshot.items], 'items': items}, indent=1, default=str),
            encoding='utf-8')
        run['result'] = {
            'seconds': round(time.monotonic() - started, 1), 'evidence_items': len(snapshot.items),
            'items': {k: {f: v[f] for f in ('seconds', 'error', 'status', 'revision_count')} | {
                'issues': len((v['verification'] or {}).get('issues', []))} for k, v in items.items()},
            'usage': {k: usage.get(k) for k in ('calls', 'input_tokens', 'output_tokens', 'cached_input_tokens',
                                                'unknown_usage_calls', 'refused_before_send', 'budget_input_tokens')},
            'usage_by_purpose': [{k: g.get(k) for k in ('purpose', 'calls', 'input_tokens', 'output_tokens')}
                                 for g in usage.get('groups', [])]}
        if args.command == 'precheck':
            require(not usage.get('calls') and usage.get('refused_before_send'), f'precheck sent a request: {run["result"]["usage"]}')


if __name__ == '__main__':
    main()
