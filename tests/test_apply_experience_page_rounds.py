"""Experience-page model test findings (2026-10-16): evidence vs literal source, batching, record status."""
import pytest
from test_apply_experience_context import session, bind
from test_auto_apply import field
from eregion.runtime.auto_apply.agent import record_status
from eregion.runtime.auto_apply.browser import BrowserBlocked
from eregion.runtime.auto_apply.runner import attempt_record, attempt_target, ATTEMPT_TARGET_VERSION


def decide_current(tools, refs):
    return tools.execute('resolve_field', {'observation_id': tools.observation_id, 'field_key': 'current', 'mode': 'equivalent',
        'value': 'true', 'source_refs': refs, 'reason': 'The record says Present and has no end date.'})


def test_same_record_facts_may_support_a_semantic_decision():
    browser, tools = session(); bind(tools)
    result = decide_current(tools, ['experience:one:current', 'experience:one:start_date'])
    assert result['verified'] and browser.fields[1]['value'] is True
    decision = tools.state['field_decisions']['current']
    assert set(decision['sources']) == {'experience:one:current', 'experience:one:start_date'}


def test_another_records_or_a_missing_fact_is_still_refused():
    browser, tools = session(); bind(tools)
    with pytest.raises(BrowserBlocked, match='bound work-experience'):
        decide_current(tools, ['experience:one:current', 'experience:two:end_date'])
    with pytest.raises(BrowserBlocked, match='unavailable'):
        decide_current(tools, ['experience:one:current', 'experience:one:end_date'])   # record one has no end date
    assert browser.fields[1]['value'] is False and 'current' not in tools.state.get('field_decisions', {})


def test_literal_fill_still_uses_only_the_bound_component():
    browser, tools = session(); bind(tools)
    with pytest.raises(BrowserBlocked, match='bound work-experience'):
        tools.source(tools.fields[0], 'experience:one:start_year')


def test_unticked_default_is_not_a_decision_in_record_status():
    browser, tools = session(); bind(tools)
    work = next(r for r in record_status(tools.fields, tools.state) if r['record'] == 'work:one')
    assert work['checkbox_without_decision'] == ['current'] and not work['all_bound_fields_verified']
    decide_current(tools, ['experience:one:current'])
    tools.observe()
    work = next(r for r in record_status(tools.fields, tools.state) if r['record'] == 'work:one')
    assert work['checkbox_without_decision'] == [] and 'current' in work['verified']
    other = next(r for r in record_status(tools.fields, tools.state) if r['record'] == 'work:two')
    assert other['bound'] == 0 and not other['all_bound_fields_verified']


class Host:
    """Only what validate_batch reads."""
    def __init__(self, fields):
        self.session = type('S', (), {'fields': fields})()


def batch(*calls):
    from eregion.runtime.auto_apply.runner import ApplicationHost
    host = Host([field('a'), field('b'), field('c', kind='checkbox')])
    ApplicationHost.validate_batch(host, [{'name': n, 'args': a} for n, a in calls], None)


def test_bind_decide_and_scoped_fill_fit_one_turn():
    batch(('bind_experience', {'fields': [{'field_key': 'a'}, {'field_key': 'c'}]}),
          ('resolve_field', {'field_key': 'c'}),
          ('autofill', {'field_keys': ['a', 'b']}))


def test_writes_then_one_page_action_fit_one_turn():
    batch(('bind_experience', {'fields': [{'field_key': 'a'}]}), ('resolve_field', {'field_key': 'c'}),
          ('autofill', {'field_keys': ['a']}), ('click_page_action', {'control_id': 'add'}))
    batch(('click_page_action', {'control_id': 'next'}),)


@pytest.mark.parametrize('calls', [
    [('click_page_action', {'control_id': 'add'}), ('resolve_field', {'field_key': 'a'})],     # page action not last
    [('click_page_action', {'control_id': 'add'}), ('click_page_action', {'control_id': 'next'})],
    [('autofill', {'field_keys': ['a']}), ('resolve_field', {'field_key': 'b'}), ('click_page_action', {'control_id': 'x'})],
    [('resolve_field', {'field_key': 'a'}), ('type_from_source', {'field_key': 'a'})],          # two writes
    [('resolve_field', {'field_key': 'a'}), ('bind_experience', {'fields': [{'field_key': 'a'}]})],  # bind after write
    [('autofill', {'field_keys': ['a']}), ('resolve_field', {'field_key': 'b'})],               # autofill not last
    [('resolve_field', {'field_key': 'a'}), ('autofill', {'field_keys': ['a']})],                # overlapping write
    [('resolve_field', {'field_key': 'b'}), ('autofill', {})],                                  # unscoped in a batch
    [('resolve_field', {'field_key': 'new'})],                                                  # unobserved field
])
def test_unsafe_batches_are_rejected(calls):
    from eregion.runtime.core.engine import ToolArgumentsError
    with pytest.raises(ToolArgumentsError):
        batch(*calls)


def test_attempt_count_survives_search_results_and_unrelated_changes():
    picker = {'key': 'text:education--fieldOfStudy:1', 'kind': 'text', 'label': 'Field of Study', 'value': '', 'choice_control': True,
              'options': [{'label': 'Accounting'}]}
    state = {'profile': {}, 'answers': {picker['key']: 'Systems Engineering'}, 'other': 1}
    state['field_attempts'] = {picker['key']: {'v': ATTEMPT_TARGET_VERSION, 'count': 3, 'target': attempt_target(state, picker)}}
    searched = {**picker, 'options': [{'label': 'Computer Systems Analysis'}, {'label': 'Systems Science'}]}
    state['answers']['text:unrelated:1'] = 'changed'
    assert attempt_record(state, searched) == (3, 'current')
