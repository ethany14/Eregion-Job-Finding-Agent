"""F1: tool contracts checked through the advertised schema and the real TaskLoop dispatch."""
from eregion.runtime.core.engine import validate_call
from eregion.runtime.tasks.loop import TaskLoop
from test_career_flow import career, prepare, ToolModel, settled  # noqa: F401  (fixture)


def test_inspect_job_without_requirement_ids_passes_the_advertised_schema_and_runs(career):
    c = career
    job, _ = prepare(c)
    # The schema the model sees and the handler agree: requirement_ids only narrows the read.
    validate_call({'name': 'inspect_job', 'args': {'application_id': job['application_id']}}, c.harness.tools)
    steps = iter([('inspect_job', {'application_id': job['application_id']}),
                  ('finish_task', {'text': 'Read the saved job.'})])
    loop = TaskLoop(c.repository, c.harness, model_factory=lambda: ToolModel(lambda snapshot: next(steps)))
    try:
        loop.chat(c.state['task_id'], 'Show me the saved job.', 'inspect-whole-job')
        state = settled(c.repository, c.state['task_id'], statuses=('completed',))
    finally:
        loop.close()
    inspected = [h for h in state['history'] if h['tool'] == 'inspect_job']
    assert inspected and not (inspected[-1].get('result') or {}).get('error_code')
    assert inspected[-1]['result']['application_id'] == job['application_id']
