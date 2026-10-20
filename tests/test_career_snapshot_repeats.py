"""Career observations state shared guidance once; nothing the model needs leaves the request."""
from eregion.runtime.career.harness import DECISION_NOTE, SCORED_DETAIL, shared_job_guidance
from eregion.runtime.career.prompt import RECOMMENDATION_RULE, SYSTEM
from test_career_flow import career  # noqa: F401


def row(n):
    return {'application_id': f'a{n}', 'title': f'Job {n}', 'match_score': 70 + n, 'detail': SCORED_DETAIL,
            'decision_brief': {'counts': {'required:matched': n}, 'note': DECISION_NOTE}}


def test_guidance_repeated_on_every_job_is_stated_once():
    rows, shared = shared_job_guidance([row(1), row(2)])
    assert shared == {'decision_brief_note': DECISION_NOTE, 'detail': SCORED_DETAIL}
    assert rows == [{'application_id': f'a{n}', 'title': f'Job {n}', 'match_score': 70 + n,
                     'decision_brief': {'counts': {'required:matched': n}}} for n in (1, 2)]
    # One job keeps its guidance inline: moving it would not save anything.
    assert shared_job_guidance([row(1)]) == ([row(1)], {})


def test_the_tier_rule_is_referenced_only_when_the_system_prompt_states_it(career):
    c = career
    assert RECOMMENDATION_RULE in SYSTEM and c.harness.prompt == SYSTEM
    instruction = c.harness.observe(c.repository.get(c.state['task_id']))['known_results']['instruction']
    assert RECOMMENDATION_RULE not in instruction and 'recommendation rule (tier definitions)' in instruction
    c.harness.prompt = 'A custom prompt without the rule.'
    assert RECOMMENDATION_RULE in c.harness.observe(c.repository.get(c.state['task_id']))['known_results']['instruction']
