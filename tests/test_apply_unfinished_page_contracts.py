"""F3: application contracts on an unfinished page, through the production agent loop in real Chrome.

The page starts incomplete (empty required fields, a corrupted year). Scripted turns, not a
model's decisions, exercise each contract and assert the targeted batch actually executed:
the Adobe-36 batch shape (read + evidence + independent writes), a dependent read/write pair,
same-record and foreign-record education evidence, a failed write before Next, a stale
observation, and Next re-identified after writes renumber the page's controls. Run once per
driver (JOB_AGENT_BROWSER_DRIVER=playwright|patchright)."""
import json
import os

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from eregion.runtime.auto_apply.agent import HybridApplicationAgent
from eregion.runtime.auto_apply.autofill import autofill
from test_apply_experience_page_browser import PageBrowser
from test_auto_apply_workday import URL

pytestmark = pytest.mark.skipif(os.getenv('JOB_AGENT_BROWSER_TESTS') != '1', reason='Offline Chrome opt-in')

PROFILE = {'experience': [{'id': 'one', 'title': 'Research', 'company': 'University', 'start_date': 'Aug 2026', 'current': 'Present'}],
           'education': [{'id': 'a', 'school': 'School A', 'degree': 'Masters', 'field_of_study': 'Systems Engineering'},
                         {'id': 'b', 'school': 'School B', 'degree': 'Bachelors', 'field_of_study': 'Computer Science'}]}
WORK = {'jobTitle': 'title', 'companyName': 'company', 'startDate-dateSectionMonth-input': 'start_month',
        'startDate-dateSectionYear-input': 'start_year'}


def key(snap, part, n=1):
    return next(f['key'] for f in snap['fields'] + snap.get('field_index', []) if part in f['key'] and f['key'].endswith(f':{n}'))


def control(snap, label):
    return next(c for c in snap['page']['controls'] if c['label'].startswith(label))


def test_contracts_on_an_unfinished_page_through_the_loop(monkeypatch):
    monkeypatch.setattr('eregion.runtime.auto_apply.fast_options.fill_options', lambda *a, **k: [])
    b = PageBrowser(URL)
    # Like Workday's row Delete: writing Field of Study (only in the final batch) reveals a button before
    # Next, renumbering the controls after the model chose its Next id.
    b.page.evaluate('''() => {
        document.addEventListener('input', e => {
            if (!e.target.id.includes('fieldOfStudy') || document.getElementById('revealed')) return;
            const button = document.createElement('button'); button.type = 'button'; button.id = 'revealed'; button.textContent = 'Delete';
            document.getElementById('addSchool').after(button);
        }, true);
        document.addEventListener('submit', () => { window.revealedBeforeNext = !!document.getElementById('revealed'); }, true);
    }''')
    state = {'automation_mode': 'hybrid', 'answers': {}, 'status': 'running', 'chat_revision': 1, 'messages': [], 'profile': PROFILE}
    seen, results, first = [], [], {}

    def adobe36(s):
        first.update(observation_id=s['observation_id'], next=control(s, 'Next'))
        work = [{'field_key': key(s, p), 'component': c} for p, c in WORK.items()]
        return [('inspect_field', {'field_key': key(s, 'degree')}),
                ('bind_education', {'education_id': 'a', 'fields': [{'field_key': key(s, 'schoolName'), 'component': 'school'},
                    {'field_key': key(s, 'degree'), 'component': 'degree'}, {'field_key': key(s, 'fieldOfStudy'), 'component': 'field_of_study'}]}),
                ('bind_experience', {'experience_id': 'one', 'fields': work}),
                ('resolve_field', {'field_key': key(s, 'current'), 'mode': 'equivalent', 'value': 'true',
                                   'source_refs': ['experience:one:current'], 'reason': 'Scripted.'}),
                ('type_from_source', {'field_key': key(s, 'schoolName'), 'source_ref': 'education:a:school'})]

    def dependent(s):
        return [('inspect_field', {'field_key': key(s, 'degree')}),
                ('resolve_field', {'field_key': key(s, 'degree'), 'mode': 'equivalent', 'value': 'Masters',
                                   'source_refs': ['education:a:degree'], 'reason': 'Scripted.'})]

    def evidence(s):
        return [('resolve_field', {'field_key': key(s, 'degree'), 'mode': 'equivalent', 'value': 'Masters',
                                   'source_refs': ['education:a:degree', 'education:a:school'], 'reason': 'Same record: degree and school.'}),
                ('resolve_field', {'field_key': key(s, 'fieldOfStudy'), 'mode': 'equivalent', 'value': 'Computer Science',
                                   'source_refs': ['education:b:field_of_study'], 'reason': 'Scripted foreign-record citation.'})]

    def failed_write_then_next(s):
        return [('resolve_field', {'field_key': key(s, 'degree'), 'mode': 'equivalent', 'value': 'Doctorate',
                                   'source_refs': ['education:a:degree'], 'reason': 'Scripted: not an option on this page.'}),
                ('click_page_action', {'control_id': control(s, 'Next')['id']})]

    def stale(s):
        return [('click_page_action', {'control_id': first['next']['id'], 'observation_id': first['observation_id']})]

    def finish(s):
        # School B exists only to test foreign-record evidence; it is omitted explicitly, with a cited reason.
        return [('plan_records', {'records': [{'record': 'education:b', 'status': 'omit', 'source_refs': ['education:b:school'],
                                               'reason': 'Scripted: this page asks for one school.'}]}),
                ('resolve_field', {'field_key': key(s, 'fieldOfStudy'), 'mode': 'equivalent', 'value': 'Systems Engineering',
                                   'source_refs': ['education:a:field_of_study'], 'reason': 'Bound record fact.'}),
                ('autofill', {'field_keys': [key(s, p) for p in WORK]}),
                # The id observed before this batch's writes; the Field of Study write reveals Delete first.
                ('click_page_action', {'control_id': control(s, 'Next')['id']})]

    turns = iter([adobe36, dependent, evidence, failed_write_then_next, stale, finish])

    class Model:
        def invoke(self, messages, tools):
            snap = json.loads(next(m.content for m in reversed(messages) if isinstance(m, HumanMessage) and isinstance(m.content, str)))
            seen.append(snap)
            results.append(snap.get('last_tool_result'))
            try:
                calls = next(turns)(snap)
            except StopIteration:
                return AIMessage(content='', tool_calls=[{'id': 'end', 'name': 'ask_user', 'args': {'text': 'Scripted test ended.', 'field_keys': []}}])
            for name, args in calls:
                if name != 'autofill':
                    args.setdefault('observation_id', snap['observation_id'])
            return AIMessage(content='', tool_calls=[{'id': f'{len(seen)}-{i}', 'name': n, 'args': a} for i, (n, a) in enumerate(calls)])

    done = lambda: b.page.evaluate('window.nextCount > 0')
    try:
        HybridApplicationAgent(model_factory=lambda: Model()).run(
            b, state, autofill=lambda: autofill(b, state, lambda: b''), active=lambda: not done(),
            reserve=lambda kind: True, report=lambda *a: None)
        history = state.get('decision_history', [])
        tools_run = [h['tool'] for h in history]
        result = lambda h: h.get('result') or {}
        if os.getenv('F3_DEBUG'):
            for h in history: print('H', h['tool'], json.dumps(h.get('result'), default=str)[:300])
        rejected = [i for i, h in enumerate(history) if 'Batch rejected' in str(result(h).get('error', ''))]
        # 1. Adobe-36 shape: read + evidence + independent writes all ran, before any rejection, without errors.
        opening = history[:rejected[0]]
        for tool in ('inspect_field', 'bind_education', 'bind_experience', 'resolve_field', 'type_from_source'):
            assert any(h['tool'] == tool and not result(h).get('error') for h in opening), (tool, tools_run)
        # 2. A write chosen before its read returned is refused as a whole batch, naming the split.
        assert len(rejected) == 2 and all('send the read alone' in result(history[i])['error'] for i in rejected)
        # 3. Same-record facts support a decision; another record's fact is refused.
        degree = state['field_decisions']['select:education--degree:1']
        assert degree['value'] == 'Masters' and {'education:a:degree', 'education:a:school'} <= set(degree['sources'])
        assert any(result(h).get('error_code') == 'education_source_mismatch' for h in history)
        # 4. A failed write in a batch keeps its closing Next from running.
        failed = next(i for i, h in enumerate(history) if h['tool'] == 'resolve_field' and result(h).get('error_code') == 'invalid_action')
        assert history[failed + 1]['tool'] == 'click_page_action' and result(history[failed + 1])['error_code'] == 'batch_incomplete'
        # 5. An old observation is refused.
        assert any(h['tool'] == 'click_page_action' and result(h).get('error_code') == 'stale_observation' for h in history)
        # 6. Next re-identified after the batch's own write renumbered the controls; exactly one Next, no submit.
        assert b.page.evaluate('window.nextCount===1 && window.submitCount===0 && window.revealedBeforeNext===true'), tools_run[-8:]
        assert len([h for h in history if h['tool'] == 'click_page_action' and result(h).get('navigated')]) == 1
    finally:
        b.close()
