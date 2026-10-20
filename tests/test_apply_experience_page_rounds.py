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
    return host


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
    [('resolve_field', {'field_key': 'a'}), ('bind_experience', {'fields': [{'field_key': 'a'}]}),
     ('click_page_action', {'control_id': 'add'})],                                             # bind after write, then leave
    [('autofill', {'field_keys': ['a']}), ('resolve_field', {'field_key': 'b'})],               # autofill not last
    [('resolve_field', {'field_key': 'b'}), ('autofill', {})],                                  # unscoped in a batch
    [('resolve_field', {'field_key': 'new'})],                                                  # unobserved field
])
def test_unsafe_batches_are_rejected(calls):
    from eregion.runtime.core.engine import ToolArgumentsError
    with pytest.raises(ToolArgumentsError):
        batch(*calls)


def test_an_autofill_overlapping_earlier_writes_runs_on_the_other_keys_only():
    # Adobe 2026-10-20: these batches were refused whole (5-6 actions each).
    from eregion.runtime.auto_apply.runner import ApplicationHost
    host = Host([field('a'), field('b'), field('c', kind='checkbox')])
    calls = [{'id': '1', 'name': 'resolve_field', 'args': {'field_key': 'a'}},
             {'id': '2', 'name': 'autofill', 'args': {'field_keys': ['a', 'b']}}]
    ApplicationHost.validate_batch(host, calls, None)
    assert host.autofill_trims == {'2': (['b'], ['a'])}
    assert calls[1]['args'] == {'field_keys': ['a', 'b']}  # the model's call as sent stays in its history
    only = [{'id': '3', 'name': 'resolve_field', 'args': {'field_key': 'a'}},
            {'id': '4', 'name': 'autofill', 'args': {'field_keys': ['a']}}]
    ApplicationHost.validate_batch(host, only, None)
    assert host.autofill_trims['4'] == ([], ['a'])


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


# --- Adobe re-test 08: page budget per Workday step; no host auto-advance on record pages ---
from eregion.runtime.auto_apply.runner import step_marker, record_page


def test_workday_steps_sharing_url_and_heading_are_different_budget_pages():
    experience = {'url': 'u', 'heading': ' / Job', 'text': 'completed step 2 of 7 My Information current step 3 of 7 My Experience'}
    questions = {'url': 'u', 'heading': ' / Job', 'text': 'completed step 3 of 7 current step 4 of 7 Application Questions 1 of 2'}
    assert step_marker(experience) == '3/7' and step_marker(questions) == '4/7'
    assert step_marker({'url': 'u', 'heading': 'h', 'text': 'no steps here'}) is None


def test_record_rows_stop_the_host_from_advancing_but_plain_pages_do_not():
    state = {'profile': {'experience': [], 'education': []}, 'answers': {}}
    row = {**field('text:jobTitle:1'), 'dom_id': 'workExperience-4--jobTitle'}
    assert record_page([row], state, {'controls': []})
    assert record_page([field('x')], state, {'controls': [{'kind': 'add_entry', 'label': 'Add Another'}]})
    assert not record_page([field('legal-age')], state, {'controls': [{'kind': 'next', 'label': 'Next'}]})
    browser, tools = session(); bind(tools)
    assert record_page(tools.fields, tools.state, {'controls': []})      # model-bound work field, no DOM id


def test_an_empty_optional_questionnaire_question_waits_for_the_models_decision():
    from eregion.runtime.auto_apply.runner import undecided_optional_question
    phd = {**field('textarea:phd:1', kind='textarea', required=False), 'dom_id': 'secondaryQuestionnaire--phd'}
    state = {'profile': {}, 'answers': {}}
    assert undecided_optional_question([phd], state)
    state['field_decisions'] = {phd['key']: {'field': {k: phd.get(k) for k in ('key', 'label', 'kind', 'group')}, 'value': None,
                                             'mode': 'leave_empty', 'sources': {}, 'original_answer': None, 'reason': 'Not a PhD candidate.'}}
    from eregion.runtime.auto_apply.decisions import signature
    state['field_decisions'][phd['key']]['field'] = signature(phd)
    assert not undecided_optional_question([phd], state)
    assert not undecided_optional_question([{**field('ext', required=False), 'dom_id': 'phoneNumber--extension'}], {'profile': {}, 'answers': {}})
    assert not undecided_optional_question([{**phd, 'required': True}], {'profile': {}, 'answers': {}})


# --- Adobe-22: closed dropdown options read once at page entry; record then choose in one turn ---
def test_host_reads_closed_dropdown_options_once_without_choosing():
    from eregion.runtime.auto_apply.agent import AgentTools
    from eregion.runtime.auto_apply.runner import ApplicationHost
    reads, closes = [], []
    button = {'placeholder': None, 'autocomplete': None}
    q1 = {**field('q1', kind='combobox'), 'options': [], 'constraints': button}
    q2 = {**field('q2', kind='combobox'), 'options': [], 'constraints': button}
    filled = {**field('lang', kind='combobox'), 'options': [], 'value': 'English', 'constraints': button}
    gone = {**field('q3', kind='combobox'), 'options': [], 'constraints': button}
    search = {**field('school', kind='combobox'), 'options': [], 'constraints': {'placeholder': 'Search', 'autocomplete': 'off'}}
    unknown = {**field('other', kind='combobox'), 'options': []}
    browser = NextPage([q1, q2, filled, gone, search, unknown])
    browser.inspect_options = lambda key, query=None: reads.append(key) or [{'label': 'Yes', 'value': '1'}, {'label': 'No', 'value': '2'}]
    browser.press_key = lambda key, value: closes.append((key, value))
    state = {'profile': {}, 'answers': {}, 'abandoned_fields': ['q3']}
    tools = AgentTools(browser, state, autofill=lambda: {}, active=lambda: True); tools.observe()
    host = ApplicationHost(tools, None, active=lambda: True, reserve=lambda kind: True, report=lambda *a: None)
    host._read_closed_options(tools, limit=1)
    assert reads == ['q1'] and closes == [('q1', 'Escape')]
    host._read_closed_options(tools)
    assert reads == ['q1', 'q2']   # already read, filled, abandoned, searchable and unknown shapes skipped
    assert all(f['value'] in ('', 'English') for f in browser.fields)   # nothing chosen
    shown = next(f for f in tools.observe()['fields'] if f['key'] == 'q2')
    assert [o['label'] for o in shown['options']] == ['Yes', 'No']


def test_record_an_answer_then_choose_it_in_one_turn():
    batch(('record_answers', {'answers': [{'field_key': 'a'}, {'field_key': 'b'}]}),
          ('choose_option', {'field_key': 'a'}), ('choose_option', {'field_key': 'b'}),
          ('click_page_action', {'control_id': 'next'}))
    # Evidence after a write of its field waits for the next turn (Adobe run 40); with a closing
    # page action the field would be gone, so that batch is still refused.
    with pytest.raises(__import__('eregion.runtime.core.engine', fromlist=['x']).ToolArgumentsError):
        batch(('choose_option', {'field_key': 'a'}), ('record_answers', {'answers': [{'field_key': 'a'}]}),
              ('click_page_action', {'control_id': 'next'}))


def test_evidence_after_a_search_of_its_field_is_held_and_the_rest_of_the_batch_runs():
    # Adobe run 40: two major searches and two school bindings were all refused because one
    # binding named a searched field.
    calls = [('search_options', {'field_key': 'a'}), ('search_options', {'field_key': 'b'}),
             ('bind_education', {'fields': [{'field_key': 'a'}]}),
             ('bind_education', {'fields': [{'field_key': 'c'}, {'field_key': 'b'}]})]
    from eregion.runtime.auto_apply.runner import ApplicationHost
    host = Host([field('a'), field('b'), field('c', kind='checkbox')])
    ApplicationHost.validate_batch(host, [{'id': str(i), 'name': n, 'args': a} for i, (n, a) in enumerate(calls)], None)
    assert host.evidence_deferrals == {'2': ['a'], '3': ['b']}
    host = batch(('choose_option', {'field_key': 'a'}), ('record_answers', {'answers': [{'field_key': 'a'}]}))
    assert list(host.evidence_deferrals.values()) == [['a']]


def test_the_held_evidence_reports_itself_and_the_other_calls_run(monkeypatch):
    from langchain_core.messages import AIMessage
    from eregion.runtime.auto_apply.agent import AgentTools, HybridApplicationAgent
    from eregion.runtime.auto_apply.autofill import autofill
    from test_auto_apply_agent import RepairBrowser, state
    recorded, original = [], AgentTools.execute

    def execute(self, name, args):
        if name == 'record_answers':
            recorded.append([item['field_key'] for item in args['answers']])
            return {'recorded': recorded[-1]}
        return original(self, name, args)
    monkeypatch.setattr(AgentTools, 'execute', execute)
    answer = lambda key: {'field_key': key, 'message_id': 'm', 'quote': 'x', 'value': 'x'}
    turns = iter([
        lambda snap: [{'id': 'w', 'name': 'type_from_source', 'args': {'observation_id': snap['observation_id'],
                       'field_key': 'First name', 'source_ref': 'profile:first_name'}},
                      {'id': 'all', 'name': 'record_answers', 'args': {'observation_id': snap['observation_id'],
                       'answers': [answer('First name')]}},
                      {'id': 'part', 'name': 'record_answers', 'args': {'observation_id': snap['observation_id'],
                       'answers': [answer('First name'), answer('Country')]}}],
        lambda snap: [{'id': 'end', 'name': 'request_input', 'args': {'field_keys': ['Country']}}]])

    class Model:
        def invoke(self, messages, tools):
            import json
            from langchain_core.messages import HumanMessage
            snap = json.loads(next(m.content for m in reversed(messages) if isinstance(m, HumanMessage) and isinstance(m.content, str)))
            return AIMessage(content='', tool_calls=next(turns)(snap))
    browser, saved = RepairBrowser(), state()
    HybridApplicationAgent(model_factory=Model).run(browser, saved, autofill=lambda: autofill(browser, saved, lambda: b''),
        active=lambda: True, reserve=lambda kind: True, report=lambda *a: None)
    results = {h['call_id']: h['result'] for h in saved['decision_history'] if h.get('call_id') in {'w', 'all', 'part'}}
    assert 'error' not in results['w'] and browser.observe()[0]['value'] == 'Test'
    assert results['all']['error_code'] == 'evidence_deferred' and results['all']['deferred_evidence'] == ['First name']
    assert recorded == [['Country']] and results['part']['deferred_evidence'] == ['First name']


def test_a_third_reopen_of_an_unchanged_page_is_refused_until_the_user_writes(monkeypatch):
    # Adobe-23: the model reopened a Workday maintenance redirect seven times.
    from eregion.runtime.auto_apply.agent import AgentTools
    opened = []
    monkeypatch.setattr('eregion.runtime.browser.tools.execute',
        lambda session, name, args: opened.append(args['url']) or {'page_evidence_hash': 'same', 'title': 'Unavailable'})
    browser = NextPage([field('x', required=False)])
    state = {'profile': {}, 'answers': {}, 'chat_revision': 1}
    tools = AgentTools(browser, state, autofill=lambda: {}, active=lambda: True); tools.observe()
    tools.browsing = object()
    url = {'url': 'https://example.test/job'}
    tools.execute('browser_open', dict(url)); tools.execute('browser_open', dict(url))
    with pytest.raises(BrowserBlocked, match='same page'):
        tools.execute('browser_open', dict(url))
    assert len(opened) == 2
    rebuilt = AgentTools(browser, state, autofill=lambda: {}, active=lambda: True); rebuilt.observe(); rebuilt.browsing = object()
    with pytest.raises(BrowserBlocked, match='same page'):      # survives a new slice
        rebuilt.execute('browser_open', dict(url))
    state['chat_revision'] = 2                                  # the user wrote again
    rebuilt.execute('browser_open', dict(url))
    assert len(opened) == 3


def test_adobe36_inspect_then_evidence_and_independent_writes_fit_one_turn():
    # Adobe-36: inspect_field(source) then record_answers(…source…) plus four independent field
    # actions was rejected as a whole. Evidence does not depend on the read; it now fits one turn.
    from eregion.runtime.auto_apply.runner import ApplicationHost
    keys = ['text:source--source:1', 'radio:candidateIsPreviousWorker:1', 'combobox:countryRegion:1',
            'text:city:1', 'combobox:phoneType:1', 'text:phoneNumber:1']
    host = Host([field(k) for k in keys])
    ApplicationHost.validate_batch(host, [{'name': n, 'args': a} for n, a in [
        ('inspect_field', {'field_key': keys[0]}),
        ('record_answers', {'answers': [{'field_key': keys[1]}, {'field_key': keys[2]}, {'field_key': keys[0]}]}),
        ('resolve_field', {'field_key': keys[3]}), ('resolve_field', {'field_key': keys[4]}),
        ('type_from_source', {'field_key': keys[5]})]], None)


@pytest.mark.parametrize('calls,split', [
    ([('inspect_field', {'field_key': 'a'}), ('resolve_field', {'field_key': 'a'})], 'send the read alone'),
    ([('search_options', {'field_key': 'a'}), ('choose_option', {'field_key': 'a'})], 'send the search alone'),
    ([('search_options', {'field_key': 'a'}), ('search_options', {'field_key': 'a'})], 'extra_queries'),
])
def test_writes_that_depend_on_an_unseen_read_are_refused_with_a_split(calls, split):
    from eregion.runtime.auto_apply.runner import ApplicationHost, BatchConflict
    with pytest.raises(BatchConflict) as error:
        batch(*calls)
    assert error.value.conflict['field_key'] == 'a' and split in error.value.conflict['split']
    outcome = ApplicationHost.action_error(None, error.value)
    assert outcome['error_code'] == 'batch_conflict' and outcome['conflict']['tool'] == calls[1][0]


def test_readback_after_a_write_is_allowed():
    batch(('resolve_field', {'field_key': 'a'}), ('read_field', {'field_key': 'a'}))


def test_the_trimmed_batch_runs_through_the_loop_and_reports_the_dropped_keys():
    from langchain_core.messages import AIMessage
    from eregion.runtime.auto_apply.agent import HybridApplicationAgent
    from eregion.runtime.auto_apply.autofill import autofill
    from test_auto_apply_agent import RepairBrowser, state
    turns = iter([
        lambda snap: [{'id': 'w', 'name': 'type_from_source', 'args': {'observation_id': snap['observation_id'],
                       'field_key': 'First name', 'source_ref': 'profile:first_name'}},
                      {'id': 'f', 'name': 'autofill', 'args': {'field_keys': ['First name']}}],
        lambda snap: [{'id': 'end', 'name': 'request_input', 'args': {'field_keys': ['Country']}}]])

    class Model:
        def invoke(self, messages, tools):
            import json
            from langchain_core.messages import HumanMessage
            snap = json.loads(next(m.content for m in reversed(messages) if isinstance(m, HumanMessage) and isinstance(m.content, str)))
            return AIMessage(content='', tool_calls=next(turns)(snap))
    browser, saved = RepairBrowser(), state()
    HybridApplicationAgent(model_factory=Model).run(browser, saved, autofill=lambda: autofill(browser, saved, lambda: b''),
        active=lambda: True, reserve=lambda kind: True, report=lambda *a: None)
    results = {h['call_id']: h['result'] for h in saved['decision_history'] if h.get('call_id') in {'w', 'f'}}
    assert 'error' not in results['w'] and browser.observe()[0]['value'] == 'Test'
    assert results['f']['skipped'] and results['f']['dropped_overlap'] == ['First name']
