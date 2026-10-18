"""Free regressions for apply-test-08: missing records and shared-URL budgets."""
from copy import deepcopy
import pytest
from test_auto_apply import field
from test_apply_experience_context import session, bind
from eregion.runtime.auto_apply.record_coverage import coverage, gaps, plan_records
from eregion.runtime.auto_apply.steps import budget_record
from eregion.runtime.auto_apply.observation import field_delta, apply_field_delta
from eregion.runtime.auto_apply.browser import BrowserBlocked


def test_missing_whole_record_is_not_complete_even_when_visible_fields_are_filled():
    browser, tools = session(); bind(tools)
    for f in browser.fields:
        f['value'] = True if f['kind'] == 'checkbox' else '08'
    rows = coverage(browser.fields, tools.state)
    assert not next(r for r in rows if r['record'] == 'work:two')['complete']
    assert any('work:two' in message for message in gaps(browser.fields, tools.state, {}))
    # Even current=true is not completion until a model decision has been saved.
    assert next(r for r in rows if r['record'] == 'work:one')['pending']


def test_bound_record_with_unbound_peer_is_incomplete():
    browser, tools = session(); bind(tools)
    first = tools.fields[0]
    extra = {**field('company', required=False), 'group': first.get('group'), 'value': 'Prefilled'}
    # Explicit structural identity links an unbound peer, not text similarity.
    first['dom_id'] = 'workExperience-1--startDate-dateSectionMonth-input'
    extra['dom_id'] = 'workExperience-1--companyName'
    rows = coverage([first, extra], tools.state)
    assert any(p['key'] == 'company' for p in rows[0]['pending'])


def test_model_next_cannot_skip_an_uncreated_record():
    browser, tools = session(); bind(tools)
    browser.fields[0]['value'] = '08'
    tools.execute('resolve_field', {'observation_id': tools.observation_id, 'field_key': 'current',
        'mode': 'equivalent', 'value': 'true', 'source_refs': ['experience:one:current'], 'reason': 'Present.'})
    browser.page_state = lambda: {'url': 'u', 'heading': 'My Experience', 'text': '',
        'controls': [{'id': 'next', 'kind': 'next', 'label': 'Next', 'disabled': False}]}
    browser.click_page_action = lambda control: pytest.fail('Incomplete coverage must not click')
    tools.observe()
    with pytest.raises(BrowserBlocked, match='work:two'):
        tools.execute('click_page_action', {'observation_id': tools.observation_id, 'control_id': 'next'})


def test_add_invalidates_observation_immediately_without_clearing_failures():
    browser, tools = session(); bind(tools)
    browser.page_state = lambda: {'url': 'u', 'heading': 'h', 'text': '',
        'controls': [{'id': 'add', 'kind': 'add_entry', 'label': 'Add Another', 'disabled': False}]}
    browser.click_page_action = lambda control: None
    tools.failed_fields.add('current')
    tools.observe(); old = tools.observation_id
    tools.execute('click_page_action', {'observation_id': old, 'control_id': 'add'})
    assert 'current' in tools.failed_fields
    with pytest.raises(BrowserBlocked, match='Stale'):
        tools.execute('bind_experience', {'observation_id': old, 'experience_id': 'one', 'fields': []})


def test_omission_is_explicit_and_invalidated_by_source_changes():
    browser, tools = session(); bind(tools)
    with pytest.raises(BrowserBlocked):
        plan_records(tools.state, [{'record': 'work:two', 'status': 'omit', 'reason': 'r', 'source_refs': []}])
    tools.state['record_plan'] = plan_records(tools.state, [{'record': 'work:two', 'status': 'omit',
        'reason': 'Model determined it is outside this application scope.', 'source_refs': ['experience:two:end_date']}])
    assert not any('work:two' in m for m in gaps(tools.fields, tools.state, {}))
    tools.state['profile']['experience'][1]['end_date'] = 'Sep 2022'
    assert any('work:two' in m for m in gaps(tools.fields, tools.state, {}))


def consume(state, number=None):
    page = {'url': 'same', 'heading': 'same', 'text': f'current step {number} of 7' if number else ''}
    budgets, bucket = budget_record(state, page)
    budgets['counts'][budgets['active']] += 1
    state.update(step_budgets=budgets, page_calls={**bucket, 'count': bucket['count'] + 1})
    return bucket['count']


def test_step_budget_survives_reply_revisit_missing_marker_and_restart():
    state = {}
    assert consume(state, 3) == 0
    state['chat_revision'] = 99
    assert consume(state, 3) == 1
    assert consume(state, 4) == 0
    assert consume(state, 3) == 2
    assert consume(state) == 3
    state = deepcopy(state)  # persisted/reloaded application
    assert consume(state, 3) == 4


def test_legacy_and_unidentified_budget_do_not_become_zero():
    state = {'page_calls': {'page': ['u', 'h', 1], 'count': 17}}
    assert consume(state) == 17
    assert consume(state, 3) == 18
    assert consume(state, 4) == 0


def test_seven_same_url_steps_keep_independent_counts_on_revisit():
    state = {}
    for step in range(1, 8):
        assert consume(state, step) == 0
    for step in range(7, 0, -1):
        assert consume(state, step) == 1
    from eregion.runtime.auto_apply.steps import step_key
    assert step_key({'current_step': 'current step 3 of 7 My Experience'}) == 'step:3'
    assert step_key({'current_step': 'My Experience'}) == 'step:My Experience'


@pytest.mark.parametrize('changed_step', [False, True])
def test_next_rechecks_step_and_pending_document_before_click(changed_step):
    browser, tools = session()
    tools.state['profile'] = {}
    browser.fields = [{**field('resume', kind='file', label='Resume'), 'value': 'old.pdf'}]
    tools.state['document_refresh'] = {'resume': True}
    page = {'url': 'u', 'heading': 'same', 'text': 'current step 3 of 7',
            'controls': [{'id': 'next', 'kind': 'next', 'label': 'Next', 'disabled': False}]}
    browser.page_state = lambda: deepcopy(page)
    browser.click_page_action = lambda control: pytest.fail('Do not navigate past stale state or document')
    tools.observe()
    if changed_step: page['text'] = 'current step 4 of 7'
    with pytest.raises(BrowserBlocked, match='business step changed' if changed_step else 'Pending document'):
        tools.execute('click_page_action', {'observation_id': tools.observation_id, 'control_id': 'next'})


def test_delta_reconstructs_values_options_checkbox_removal_and_order():
    original = [{'key': 'a', 'value': False, 'options': []}, {'key': 'b', 'value': 'old'}]
    baseline = apply_field_delta(None, field_delta(None, original, 1))
    changed = [{'key': 'c', 'value': 'new'}, {'key': 'a', 'value': True, 'options': ['X'], 'invalid': True}]
    delta = field_delta(baseline, changed, 2)
    restored = apply_field_delta(baseline, delta)
    assert restored == {'revision': 2, 'fields': changed}
    unchanged = field_delta(restored, changed, 3)
    assert unchanged['kind'] == 'unchanged'
    with pytest.raises(ValueError, match='baseline'):
        apply_field_delta(None, unchanged)


def test_completion_proof_expires_when_facts_change_or_browser_is_replaced():
    from eregion.runtime.auto_apply.record_coverage import record_version, review_gaps
    from eregion.runtime.auto_apply.education import new_browser_bindings
    _, tools = session()
    state = tools.state
    state['record_kinds_seen'] = ['work']
    state['record_completion'] = {f'work:{r["id"]}': {'fact_version': record_version(state, f'work:{r["id"]}')}
                                  for r in state['profile']['experience']}
    assert review_gaps(state) == []
    state['record_plan'] = plan_records(state, [{'record': 'work:one', 'status': 'needs_input',
        'reason': 'Current application scope is unclear.', 'source_refs': []}])
    assert any('still needs' in gap for gap in review_gaps(state))
    state['record_plan'] = {}
    state['experience_answer_overrides'] = {'one': {'start_date': {'value': 'Sep 2026'}}}
    assert any('work:one' in gap for gap in review_gaps(state))
    assert new_browser_bindings(state)['record_completion'] == {}


def test_local_view_retains_record_coverage_and_new_field_forces_full_observation():
    browser, tools = session(); bind(tools)
    tools.execute('focus_fields', {'field_keys': [tools.fields[0]['key']]})
    scoped = tools.observe()
    assert any(r['record'] == 'work:two' and not r['complete'] for r in scoped['record_coverage'])
    browser.fields.append(field('new-record-company'))
    changed = tools.observe()
    assert any(f['key'] == 'new-record-company' for f in changed['fields'])
    assert tools.state['observation_scope'] is None


def test_delta_preserves_small_changes_and_falls_back_for_large_changes():
    fields = [{'key': str(i), 'value': 'long value ' * 40, 'checked': False} for i in range(20)]
    baseline = {'revision': 1, 'fields': fields}
    changed = deepcopy(fields); changed[0]['checked'] = True
    delta = field_delta(baseline, changed, 2)
    assert delta['kind'] == 'delta'
    assert apply_field_delta(baseline, delta)['fields'] == changed
    assert field_delta(baseline, [{'key': 'new', 'value': 'x'}], 2)['kind'] == 'full'
