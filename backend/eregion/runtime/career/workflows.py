"""Optional orchestration only; the granular handlers own all business effects."""
from eregion.runtime.tasks.loop import TEXT, function
from eregion.runtime.provider_usage import TaskProviderBudgetExceeded
from .alerts import job_key

TOOL = function('verify_and_score',
    'Read or reuse one saved posting, qualify, import and score it. Never recommends. '
    'On failure inspect completed_steps and use the indicated granular fallback.',
    {k: TEXT for k in ('url', 'region_ref', 'observation_id', 'message_id',
                      'job_text', 'title', 'company', 'location')}, ['url'])
RULE = ('\nFor a promising observed posting use verify_and_score to read, import and score in one call; '
        'do not first page through its complete archived JD. Decide recommend_job separately from its actual score '
        'and qualification evidence. For a posting already read, use verify_and_score to reuse it, or '
        'dismiss_candidate with an exact source quote and reason if unsuitable. Finish started candidates before '
        'searching again. A progress message or plan is not completed candidate work.\n')
RULE += ('For multi-job requests prefer verify_and_score_jobs with up to 20 observed URLs per call: it scores only as many as the '
         'remaining budget can finish (others return as unscored_for_budget, never matches) and defers '
         'unverified role/location/seniority without scoring and shares the scorer across qualified candidates. '
         'Do not page through JD context first. After deciding scored results, continue a different candidate '
         'batch or query; never keep proposing the same deferred URLs. Use decision_brief for each scored job: '
         'it exposes strengths, gaps and known ineligibility. Read a precise match_report field only if a necessary '
         'fact is absent. You can issue decisions for multiple independent scored jobs in one response: at most six per '
         'response, with reason and caveats under 30 words each, so the reply fits the output limit.\n')
RULE += ('Large entry-level searches expand equivalent role queries automatically, preserving location and provider level filters. '
         'Do not repeat that search with presentation-only changes. Card ordering is advisory, not a qualification. '
         'Expanded city searches default to work_type=onsite_or_hybrid; pass work_type=remote explicitly to look at remote postings. '
         'Choose which cards to read: the source budget is finite, so read cards whose title and location plausibly fit '
         'before cards whose title states a different level or whose location is another city or only a country. '
         'The qualification model already reviews the complete saved JD and gives verified source quotes. '
         'If it remains unknown, only new source evidence for the unresolved field helps; rereading the same JD does not. '
         'Finish honestly with the saved recommendations and the shortfall when further searching is not worth it.\n')
FALLBACK = 'Use read_job, import_job, then score_job individually; inspect saved progress first.'


def run(harness, task_id, args, call_id):
    repo = harness.repository
    state = repo.get(task_id)
    key = harness.key(state, 'verify_and_score', {**args, 'url': job_key(args['url'])})
    record = dict(state.get('verification_workflows', {}).get(key, {}))
    completed = list(record.get('completed_steps', []))
    initial_analyses = state.get('analyses', {}).copy()

    def save(**changes):
        record.update(changes, completed_steps=list(completed))
        repo.change(task_id, lambda s: {'verification_workflows': {
            **s.get('verification_workflows', {}), key: dict(record)}})

    def step(name, arguments):
        order = ('read_job', 'import_job', 'score_job')
        # A revalidation can fail after inputs change. Do not describe a prior
        # score as completed for the current attempt; domain caches own reuse.
        completed[:] = [s for s in completed if order.index(s) < order.index(name)]
        save(pending_step=name)  # Intent before the existing handler's effects.
        from eregion.runtime.provider_usage import usage_scope
        with usage_scope(purpose=name):
            result = harness.execute(task_id, name, arguments, call_id=f'{call_id}:{name}')
        if result.get('error'):
            detail = {'completed_steps': list(completed), 'failed_step': name}
            if result.get('error_code') not in {'candidate_rejected', 'source_blocked'}:
                detail['fallback'] = FALLBACK
            save(failure=detail)
            result = {**result, **detail}
            return result
        if name not in completed:
            completed.append(name)
        save(pending_step=None, **({'retrieved_job': result} if name == 'read_job' else {}))
        return result

    current = 'read_job'
    try:
        saved = next((j for j in state.get('retrieved_jobs', {}).values()
                      if j.get('url') and job_key(j['url']) == job_key(args['url'])
                      and j.get('description')), None)
        if not saved and record.get('retrieved_job'):
            saved = record['retrieved_job']
            repo.change(task_id, lambda s: {'retrieved_jobs': dict(list({
                **s.get('retrieved_jobs', {}), saved['url']: saved}.items())[-100:])})
        if not saved and not args.get('job_text'):
            saved = step('read_job', {k: v for k, v in args.items()
                                    if k in ('url', 'region_ref', 'observation_id')})
            if saved.get('error'):
                return {**saved, 'completed_steps': completed, 'failed_step': current}
        elif saved and 'read_job' not in completed:
            completed.append('read_job')
            save(pending_step=None, retrieved_job=saved)
        if saved:
            from .candidate_health import validate_and_record
            validate_and_record(repo,task_id,saved)
            from .qualification import record as qualify
            qualification=qualify(repo,task_id,saved)
            issues=[c['reason'] for c in qualification['checks'].values() if c['status']!='met']
            if not qualification['qualified']:
                from .posting_brief import with_brief
                detail={'error':'Verify requested constraints before spending on fit scoring.',
                        'error_code':'qualification_needed','issues':issues,
                        'completed_steps':list(completed),'failed_step':'qualification',
                        'url':saved['url'],'title':saved.get('title'),'location':saved.get('location'),
                        'triage_evidence':with_brief(saved)['triage_evidence'],
                        'no_new_evidence':record.get('qualification_issues')==issues,
                        'next':'Use the source evidence to dismiss_candidate if unsuitable. If more source evidence genuinely establishes eligibility, use the granular import/score tools; do not repeatedly retry verify_and_score or assume remote location coverage.'}
                save(qualification_issues=issues,pending_step=None)
                return detail
        # Import re-runs the shared candidate preflight and posting qualification,
        # even on resume. Workspace source/snapshot identity deduplicates effects.
        current = 'import_job'
        imported = step(current, {k: v for k, v in args.items()
                                 if k not in ('region_ref', 'observation_id')})
        if imported.get('error'):
            return {**imported, 'completed_steps': completed, 'failed_step': current}
        save(application_id=imported['application_id'])
        current = 'score_job'
        before_score = harness.job_summary(imported['application_id'], detail=True)
        result = step(current, {'application_id': imported['application_id']})
        if result.get('error'):
            return {**result, 'completed_steps': completed, 'failed_step': current}
        from .qualification import assess
        result = {**result, 'completed_steps': completed,
                  'qualification':assess(repo.get(task_id),harness.job_summary(imported['application_id'],detail=True))}
        if (saved and imported['application_id'] in state.get('jobs', [])
                and initial_analyses == repo.get(task_id).get('analyses', {})
                and all(result.get(k) == before_score.get(k) for k in before_score if k != 'result_at')):
            result['no_new_evidence'] = True
        save(finished=True, failure=None)
        return result
    except TaskProviderBudgetExceeded as exc:
        detail = {'completed_steps': completed, 'failed_step': current}
        save(failure={**detail, 'reason': exc.reason})
        exc.workflow_detail = detail
        raise
    except Exception as exc:
        from .posting_qualification import PostingRejected
        from .source_health import SourceBlocked
        detail = {'completed_steps': completed, 'failed_step': current}
        if not isinstance(exc, (PostingRejected, SourceBlocked)):
            detail['fallback'] = FALLBACK
        save(failure={**detail, 'type': type(exc).__name__})
        exc.workflow_detail = detail
        raise
