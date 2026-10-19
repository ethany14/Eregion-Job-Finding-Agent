"""Scenario premise checks for scoring experiments (spec 11.2 F2).

Replay B was meant to test the scoring budget but its jobs were already scored in another
conversation: an empty task index (scored_job_evidence) was read as "nothing scored". This
reports the four separate facts per job so a scenario can assert its premise first:

  persisted_score  the workspace holds a match score (any conversation may have produced it)
  reusable         a saved analysis matches the current inputs (posting snapshot, resume,
                   candidate facts, scorer signature) and would be returned without paying
  task_index       this task's scored_job_evidence lists the job (rebuildable, not authority)
  recommended      this task saved a recommendation for it (a score alone never is one)
"""


def scoring_state(harness, task_id, application_ids):
    state = harness.repository.get(task_id)
    _, resume_text = harness.candidate()
    facts = harness.fit_facts()
    reusable = getattr(harness.analysis, 'reusable', None)
    index = state.get('scored_job_evidence') or {}
    recommended = {r.get('application_id') for r in state.get('recommendations', [])}
    rows = {}
    for application_id in application_ids:
        summary = harness.job_summary(application_id)
        rows[application_id] = {
            'persisted_score': summary.get('match_score') is not None,
            'reusable': bool(callable(reusable) and resume_text and reusable(application_id, resume_text=resume_text, candidate_facts=facts)),
            'task_index': application_id in index,
            'recommended': application_id in recommended,
            'version': harness.runtime.workspace.get_application(application_id).version,
        }
    return {'jobs': rows,
            'counts': {key: sum(1 for row in rows.values() if row[key]) for key in ('persisted_score', 'reusable', 'task_index', 'recommended')},
            'scorer_signature': harness.analysis.cache_signature()}
