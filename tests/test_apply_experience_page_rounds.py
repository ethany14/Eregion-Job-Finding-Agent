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


# --- Boundaries checked before the second real-model page test (51edc8a) ---
import json
from langchain_core.messages import AIMessage, HumanMessage
from test_auto_apply import FakeBrowser as _Fake


class NextPage(_Fake):
    """A fake page with a Next control that counts clicks."""
    def __init__(self, fields):
        super().__init__('')
        self.fields, self.next_clicks = fields, 0

    def page_state(self, fields=None):
        return {'url': 'https://example.test/apply', 'heading': 'My Experience', 'text': '',
                'controls': [{'id': 'next', 'kind': 'next', 'label': 'Next', 'disabled': False}]}

    def click_page_action(self, control):
        self.next_clicks += 1


def run_one_batch(browser, state, calls):
    from eregion.runtime.auto_apply.agent import HybridApplicationAgent
    from eregion.runtime.auto_apply.autofill import autofill
    turns = []
    class Model:
        def invoke(self, messages, tools):
            snap = json.loads(next(m.content for m in reversed(messages) if isinstance(m, HumanMessage) and isinstance(m.content, str)))
            turns.append(snap)
            batch = [(n, {**a, 'observation_id': snap['observation_id']} if n != 'autofill' else a) for n, a in calls]
            return AIMessage(content='', tool_calls=[{'id': f'c{i}', 'name': n, 'args': a} for i, (n, a) in enumerate(batch)])
    HybridApplicationAgent(model_factory=Model).run(browser, state, autofill=lambda: autofill(browser, state, lambda: b''),
        active=lambda: len(turns) < 2, reserve=lambda kind: True, report=lambda *a: None)
    return turns


def test_next_closing_a_batch_is_not_executed_after_a_failed_write():
    note = field('note', required=False)
    browser = NextPage([note])
    state = {'profile': {}, 'answers': {}, 'automation_mode': 'hybrid', 'status': 'running', 'chat_revision': 1, 'messages': []}
    turns = run_one_batch(browser, state, [
        ('resolve_field', {'field_key': 'note', 'mode': 'format', 'value': 'x', 'source_refs': ['profile:missing'], 'reason': 'r'}),
        ('click_page_action', {'control_id': 'next'})])
    clicks = [h for h in state['decision_history'] if h['tool'] == 'click_page_action']
    assert clicks and all(h['failed'] and 'Not executed' in json.dumps(h['result']) for h in clicks)


def test_next_closing_a_successful_batch_runs():
    note = field('note', required=False)
    browser = NextPage([note])
    state = {'profile': {}, 'answers': {'note': 'hello'}, 'automation_mode': 'hybrid', 'status': 'running', 'chat_revision': 1, 'messages': []}
    run_one_batch(browser, state, [('type_from_source', {'field_key': 'note', 'source_ref': 'answer:note'}),
                                   ('click_page_action', {'control_id': 'next'})])
    clicks = [h for h in state['decision_history'] if h['tool'] == 'click_page_action']
    assert clicks and not clicks[0]['failed']


def test_page_action_clears_only_the_detail_scope_and_old_observations_go_stale():
    browser, tools = session(); bind(tools)
    decide_current(tools, ['experience:one:current'])
    tools.state.update(field_attempts={'x': {'count': 2}}, autofill_failures={'y': {'code': 'z'}},
                       observation_scope=['current'], observation_scope_page=['u', 'h'])
    keep = {k: json.dumps(tools.state.get(k), sort_keys=True, default=str) for k in
            ('experience_bindings', 'field_decisions', 'field_attempts', 'autofill_failures', 'education_bindings')}
    browser.page_state = lambda fields=None: {'url': 'u', 'heading': 'h', 'text': '',
        'controls': [{'id': 'add', 'kind': 'action', 'label': 'Add Another', 'disabled': False}]}
    browser.click_page_action = lambda control: None
    tools.observe(); old = tools.observation_id
    tools.execute('click_page_action', {'observation_id': old, 'control_id': 'add'})
    assert tools.state['observation_scope'] is None
    assert {k: json.dumps(tools.state.get(k), sort_keys=True, default=str) for k in keep} == keep
    tools.observe()
    with pytest.raises(BrowserBlocked, match='Stale observation'):
        tools.execute('resolve_field', {'observation_id': old, 'field_key': 'current', 'mode': 'equivalent', 'value': 'true',
                                        'source_refs': ['experience:one:current'], 'reason': 'r'})


def test_record_status_uses_the_page_value_against_the_saved_decision():
    browser, tools = session(); bind(tools)
    decide_current(tools, ['experience:one:start_date'])   # same-record evidence only
    browser.fields[1]['value'] = False                     # the page no longer shows the decision
    tools.observe()
    work = next(r for r in record_status(tools.fields, tools.state) if r['record'] == 'work:one')
    assert 'current' in work['pending'] and not work['all_bound_fields_verified']
    browser.fields[1]['value'] = True
    tools.observe()
    work = next(r for r in record_status(tools.fields, tools.state) if r['record'] == 'work:one')
    assert 'current' in work['verified']
