"""P1: explicit, correctly bound facts are written by code without a new model decision.

A bound current-employment checkbox takes current_status, which exists only for an explicit
current marker or a past end date. Missing, future, conflicting or unconfirmed facts stay with
the model or the user, and a textual 'current' fact is still never read as a boolean.
"""
from datetime import datetime, timezone

import pytest

from eregion.runtime.auto_apply.autofill import desired_answer, matches
from eregion.runtime.auto_apply.experience import current_status, facts
from eregion.runtime.auto_apply.record_coverage import coverage
from test_auto_apply import field

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize('record, expected', [
    ({'current': 'Present'}, (True, 'current_marker')),
    ({'end_date': 'Present'}, (True, 'current_marker')),
    ({'end_date': 'Aug 2022'}, (False, 'past_end_date')),
    ({'end_date': '2024'}, (False, 'past_end_date')),
    ({'end_date': 'Sep 2026'}, (False, 'past_end_date')),
    ({}, (None, None)),                                   # a missing end date is not "current"
    ({'end_date': 'Dec 2026'}, (None, None)),             # future: not yet ended, not stated as current
    ({'end_date': '2026'}, (None, None)),                 # this year, month unknown
    ({'end_date': 'May 2027 (expected)'}, (None, None)),
    ({'end_date': 'Summer 2023'}, (False, 'past_end_date')),  # no month, but the year has passed
    ({'current': 'Present', 'end_date': 'Aug 2022'}, (None, None)),  # conflict
    ({'current': 'part-time'}, (None, None)),             # text the code cannot read as yes/no
])
def test_current_status_needs_an_explicit_marker_or_a_past_end_date(record, expected):
    assert current_status(record, now=NOW) == expected


def work_state(**changes):
    profile = {'experience': [
        {'id': 'one', 'title': 'Research', 'company': 'University', 'start_date': 'Aug 2026', 'current': 'Present'},
        {'id': 'two', 'title': 'Intern', 'company': 'Employer', 'start_date': 'Jul 2022', 'end_date': 'Aug 2022'},
        {'id': 'three', 'title': 'Analyst', 'company': 'Lab', 'start_date': 'Jan 2025'}], 'education': []}
    return {'profile': profile, 'answers': {}, **changes}


def checkbox(n, value=False):
    return {**field(f'checkbox:workExperience-{n}--current', 'checkbox', 'I currently work here', required=False),
            'dom_id': f'workExperience-{n}--currentlyWorkHere', 'value': value}


def bind(state, key, record, component):
    item = next(f for f in state['_fields'] if f['key'] == key)
    state.setdefault('experience_bindings', {})[key] = {
        'field': {k: item.get(k) for k in ('key', 'label', 'kind', 'group')}, 'experience_id': record,
        'component': component, 'source_ref': f'experience:{record}:{component}'}


def test_current_status_is_a_fact_only_when_determinable(monkeypatch):
    import eregion.runtime.auto_apply.experience as experience
    monkeypatch.setattr(experience, '_now', lambda profile: NOW)
    values = facts(work_state()['profile'])
    assert values['experience:one:current_status'] is True
    assert values['experience:two:current_status'] is False
    assert 'experience:three:current_status' not in values
    assert values['experience:one:current'] == 'Present'  # the stated text stays as written


def test_a_bound_checkbox_is_filled_from_current_status_and_counts_as_verified(monkeypatch):
    import eregion.runtime.auto_apply.experience as experience
    monkeypatch.setattr(experience, '_now', lambda profile: NOW)
    state = work_state()
    one, two, three = checkbox(1), checkbox(2, value=True), checkbox(3)
    state['_fields'] = [one, two, three]
    bind(state, one['key'], 'one', 'current_status')
    bind(state, two['key'], 'two', 'current_status')
    bind(state, three['key'], 'three', 'current')  # a textual component: still a model decision
    assert desired_answer(one, state) is True and desired_answer(two, state) is False
    assert desired_answer(three, state) is None
    assert not matches(one, True) and not matches(two, False)  # the page does not hold them yet
    rows = {row['record']: row for row in coverage([{**one, 'value': True}, {**two, 'value': False}, three], state)}
    assert one['key'] in rows['work:one']['verified'] and two['key'] in rows['work:two']['verified']
    assert any(item['key'] == three['key'] for item in rows['work:three']['pending'])


def test_a_user_override_changes_current_status_and_the_old_page_value_is_rechecked(monkeypatch):
    import eregion.runtime.auto_apply.experience as experience
    monkeypatch.setattr(experience, '_now', lambda profile: NOW)
    state = work_state(experience_answer_overrides={'two': {'current': {'value': 'Yes'}}})
    two = checkbox(2, value=False)
    state['_fields'] = [two]
    bind(state, two['key'], 'two', 'current_status')
    # "Yes, still there" against a past end date is a conflict: back to the model or the user.
    assert desired_answer(two, state) is None
    rows = {row['record']: row for row in coverage([two], state)}
    assert any(item['key'] == two['key'] for item in rows['work:two']['pending'])


def test_completion_evidence_names_the_bound_fact_and_conversion_and_saves_no_decision(monkeypatch):
    import eregion.runtime.auto_apply.experience as experience
    from eregion.runtime.auto_apply.record_coverage import completion_evidence
    monkeypatch.setattr(experience, '_now', lambda profile: NOW)
    state = work_state()
    title = {**field('text:workExperience-2--jobTitle', label='Job Title'), 'dom_id': 'workExperience-2--jobTitle',
             'value': 'Intern'}
    month = {**field('text:workExperience-2--startDate-dateSectionMonth-input', label='Month'),
             'dom_id': 'workExperience-2--startDate-dateSectionMonth-input', 'value': '7'}
    two = checkbox(2, value=False)
    state['_fields'] = [title, month, two]
    bind(state, title['key'], 'two', 'title')
    bind(state, month['key'], 'two', 'start_month')
    bind(state, two['key'], 'two', 'current_status')
    evidence = completion_evidence([title, month, two], state)['work:two']
    assert set(evidence['verified_fields']) == {title['key'], month['key'], two['key']}
    assert evidence['direct_facts'][two['key']] == {'source_ref': 'experience:two:current_status',
                                                    'source_value': False, 'conversion': 'boolean:past_end_date'}
    assert evidence['direct_facts'][month['key']]['conversion'] == 'date_part'
    assert evidence['direct_facts'][title['key']]['conversion'] == 'format_only'
    assert not state.get('field_decisions')  # direct writes are not recorded as model decisions
