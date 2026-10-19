"""Final reply agrees with saved recommendation state; list_jobs finds saved jobs by every word.

Layer-2 B (2026-10-18) listed five unscored jobs under "Priority match" in a reply to an
"assess my saved jobs" request; the tier check ran only for search requests and only checked
that saved recommendations were present. Structure only: whether a job belongs in a tier stays
the model's judgment, recorded with recommend_job."""
import pytest

from eregion.runtime.tasks.loop import TaskLoop
from test_career_flow import career, prepare, execute, ToolModel, settled  # noqa: F401  (fixture)

SAVED = 'https://jobs.lever.co/example/saved'
OTHER = 'https://jobs.lever.co/example/unscored'


def state_with(c, recommendations):
    c.repository.change(c.state['task_id'], goal='Assess my saved jobs and recommend the best matches.', recommendations=recommendations)
    return c.repository.get(c.state['task_id'])


@pytest.mark.parametrize('text,message', [
    # Layer-2 B shape: an unscored job under a tier label.
    (f'### Priority match (1)\n1. Data Engineer — Example\n{OTHER}\nScore: not scored.', 'Only saved recommendations'),
    # A saved recommendation under a different tier than the one saved (any request, not only searches).
    (f'### Needs your confirmation (1)\n1. Engineer — Example\n{SAVED}\nScore 61.0', 'misplaced'),
])
def test_a_job_under_a_tier_label_must_be_saved_with_that_tier(career, text, message):
    state = state_with(career, [{'url': SAVED, 'tier': 'priority', 'match_score': 61.0}])
    with pytest.raises(ValueError, match=message):
        career.harness.validate_completion(state, text)


@pytest.mark.parametrize('text', [
    f'### Priority match (1)\n**1. Engineer — Example**\n{SAVED}\nScore 61.0\n\n### Not scored (1)\n- Data Engineer — Example\n{OTHER}',
    f'**Priority match (1)**\n**1. Engineer — Example**\n{SAVED}\nScore 61.0\n\n**Not scored**\n- {OTHER} (not recommended)',
    f'No recommendation was saved.\n\nNot scored:\n- {OTHER}',
    # A plain tier label ends at the next heading.
    f'Priority match\n\nEngineer\n{SAVED}\nScore: 61.0/100\n### Not recommended\n\nData Engineer\n{OTHER}',
])
def test_saved_tiers_and_separately_listed_unscored_jobs_are_accepted(career, text):
    recommendations = [{'url': SAVED, 'tier': 'priority', 'match_score': 61.0}] if SAVED in text else []
    career.harness.validate_completion(state_with(career, recommendations), text)


def test_the_finish_tool_refuses_unscored_tiers_and_accepts_the_corrected_reply(career):
    c = career
    state_with(c, [])
    replies = iter([('finish_task', {'text': f'### Priority match (1)\n1. Data Engineer — Example\n{OTHER}\nScore: not scored.'}),
                    ('finish_task', {'text': f'No job was scored, so nothing is recommended.\n\n### Not scored (1)\n- {OTHER}'})])
    loop = TaskLoop(c.repository, c.harness, model_factory=lambda: ToolModel(lambda snapshot: next(replies)))
    try:
        loop.chat(c.state['task_id'], 'Assess my saved jobs.', 'assess-saved')
        state = settled(c.repository, c.state['task_id'], statuses=('completed',))
    finally:
        loop.close()
    finishes = [h for h in state['history'] if h['tool'] == 'finish_task']
    assert 'Only saved recommendations' in (finishes[0].get('result') or {}).get('error', '')
    assert not (finishes[-1].get('result') or {}).get('error') and 'Not scored' in state['messages'][-1]['text']


def test_list_jobs_matches_every_word_across_title_company_and_location(career):
    c = career
    job, _ = prepare(c)  # 'Python Engineer' at 'Example', location 'Remote'
    for query in ('Python Engineer Remote', 'example python', 'PYTHON'):
        found = execute(c, 'list_jobs', query=query)
        assert [j['application_id'] for j in found['jobs']] == [job['application_id']], query
    missing = execute(c, 'list_jobs', query='Python Engineer New York')
    assert missing['jobs'] == [] and 'without query' in missing['note']
    assert len(execute(c, 'list_jobs')['jobs']) == 1
