"""Field classification and usage labels for metering: descriptive only, never a decision."""
from eregion.runtime.auto_apply.agent import HybridApplicationAgent
from eregion.runtime.auto_apply.autofill import autofill
from eregion.runtime.auto_apply.field_work import classify, turn_purpose
from eregion.runtime.provider_usage import current_scope
from test_auto_apply import field
from test_auto_apply_agent import RepairBrowser, ScriptModel, state


def profile_state(**changes):
    return {'profile': {'first_name': 'Test', 'last_name': 'Candidate', 'email': 'test@example.com',
                        'experience': [], 'education': []}, 'answers': {}, **changes}


def test_fields_are_classified_by_what_they_still_need():
    email = {**field('Email', 'email'), 'value': 'test@example.com'}
    first = field('First name')
    hear = field('How did you hear about us?')
    note = field('Anything else?', required=False)
    invalid = {**field('Last name'), 'invalid': True, 'value': 'x' * 300}
    flagged_empty = {**field('Phone'), 'invalid': True}
    current = field('workExperience-1--current', 'checkbox', 'I currently work here', required=False)
    title = {**field('workExperience-1--jobTitle', label='Job Title'), 'dom_id': 'workExperience-1--jobTitle'}
    work = classify([email, first, hear, note, invalid, flagged_empty, current, title], profile_state())
    assert work['done'] == ['Email'] and work['direct'] == ['First name']
    assert work['unsourced'] == ['How did you hear about us?', 'Phone'] and work['optional'] == ['Anything else?']
    assert work['failed'] == ['Last name']
    assert set(work['model']) == {'workExperience-1--current', 'workExperience-1--jobTitle'}


def test_turn_purpose_names_the_remaining_work():
    title = {**field('workExperience-1--jobTitle', label='Job Title'), 'dom_id': 'workExperience-1--jobTitle'}
    visa = field('Will you require visa sponsorship?')
    why = field('Why this company?', 'textarea')
    assert turn_purpose({'recovery': {'reason': 'x'}}, [title], profile_state()) == 'recovery'
    assert turn_purpose({}, [title, visa], profile_state()) == 'record_binding'
    assert turn_purpose({}, [why, visa], profile_state()) == 'open_question'
    assert turn_purpose({}, [visa], profile_state()) == 'eligibility'
    assert turn_purpose({}, [{**field('Email', 'email'), 'value': 'test@example.com'}], profile_state()) == 'coordination'


def test_application_turns_record_a_purpose_label_that_is_never_the_agent_reserve_exemption():
    seen = []

    class LabelledModel(ScriptModel):
        def invoke(self, messages, tools):
            seen.append(current_scope().get('purpose'))
            return super().invoke(messages, tools)
    browser, saved = RepairBrowser(), state()
    model = LabelledModel([('request_input', {'field_keys': ['Country']})])
    HybridApplicationAgent(model_factory=lambda: model).run(browser, saved,
        autofill=lambda: autofill(browser, saved, lambda: b''), active=lambda: True,
        reserve=lambda kind: True, report=lambda *values: None)
    assert seen and all(p.startswith('apply_turn:') and p != 'agent' for p in seen)
