"""Replay Adobe-21's saved "Application Questions 2 of 2" page through the real application agent.

No website is opened, nothing is uploaded or submitted. The browser is a replay of the saved
page: the recorded fields (labels, kinds, required flags and the option lists the real Workday
dropdowns returned). Writes stay in memory and read back as written; this checks the model's
decisions and the tool chain, not Workday's widget behaviour (reported as a limitation). An
interaction the saved run never recorded (a search, Back) is refused, never invented.

The user's new statement is appended as a user message in their own words; it is evidence
for the model to cite, not a preset answer.

Usage:
  replay_questionnaire.py --source .codex-runs/apply-test-21 --out <dir> [--precheck|--dry]
          [--max-attempts N --max-input-tokens N --max-seconds S]
"""
import argparse
import copy
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'tests'), str(ROOT / 'tools/scripts')]

# The user's confirmation for this run, verbatim (2026-10-17).
USER_STATEMENT = '没有其他 offer 的答复截止日期（competing offer deadlines）。'


class QuestionnaireReplay:
    headless = True

    def __init__(self, fields, page_text, trace):
        self.fields, self.page_text, self.trace = copy.deepcopy(fields), page_text, trace
        self.ended, self.unsupported, self.next_clicks = False, [], 0

    def is_open(self): return True
    def observe(self): return copy.deepcopy(self.fields)
    def barrier(self): return None
    def field(self, key): return next(f for f in self.fields if f['key'] == key)
    def enrich_options(self, fields, active=None): return fields
    def pump(self): pass
    def verification_barrier(self): return False
    def page_state(self, fields=None):
        return {'url': 'https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced/job/San-Jose/R172083/apply/autofillWithResume',
                'heading': ' / 2027 University Graduate - Software Engineer', 'text': self.page_text, 'can_submit': False,
                'authentication_required': False,
                'controls': [{'id': 'back', 'label': 'Back', 'kind': 'back', 'disabled': False},
                             {'id': 'next', 'label': 'Next', 'kind': 'next', 'disabled': False}]}

    def _log(self, action, **detail):
        self.trace.append({'at': time.strftime('%H:%M:%S'), 'browser': action, **detail})

    def _unsupported(self, what, **detail):
        from eregion.runtime.auto_apply.browser import BrowserBlocked
        self.unsupported.append({'action': what, **detail})
        self._log('replay_unsupported', action=what, **detail)
        raise BrowserBlocked(f'Replay: no recorded website result for {what}. This replay cannot answer it; '
                             'use the recorded options or another action.')

    def inspect_options(self, key, query=None):
        if query is not None:
            self._unsupported('a search', field_key=key, query=query)
        options = self.field(key).get('options') or []
        self._log('inspect_options', field_key=key, results=len(options))
        return options

    def current_options(self, key): return self.field(key).get('options') or []
    def explore_option(self, key, label): self._unsupported('exploring an option', field_key=key, label=label)

    def choose_option(self, key, label):
        field = self.field(key)
        if not any(o['label'] == label for o in field.get('options') or []):
            from eregion.runtime.auto_apply.browser import BrowserBlocked
            raise BrowserBlocked('The option changed or is ambiguous. Inspect the field again.', code='options_changed')
        field['value'] = label
        self._log('choose_option', field_key=key, label=label)

    def fill(self, field, value):
        self.field(field['key'])['value'] = value
        self._log('fill', field_key=field['key'], value=value)
    fill_plain = fill

    def type_field(self, key, value):
        self.field(key)['value'] = value
        self._log('type_field', field_key=key, value=value)

    def click_field(self, key): self._log('click_field', field_key=key)
    def press_key(self, key, value): self._log('press_key', field_key=key, key=value)

    def click_page_action(self, control):
        if control['label'] != 'Next':
            self._unsupported(f"the {control['label']} control")
        self.next_clicks += 1
        self.ended = True  # Leaving the page ends this replay; the next page was never recorded here.
        self._log('click_page_action', label='Next')

    def close(self): pass


def prepare(source):
    saved = json.loads((Path(source) / 'latest-application.json').read_text(encoding='utf-8'))
    state = copy.deepcopy(saved.get('state', saved))
    page_text = (Path(source) / 'current-page-text.txt').read_text(encoding='utf-8')
    keys = {f['key'] for f in state['fields']}
    revision = int(state.get('chat_revision') or 0) + 1
    message = {'id': str(uuid4()), 'role': 'user', 'text': USER_STATEMENT, 'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
               'revision': revision}
    # Resume the stopped run on this page: run-level stop markers are cleared; facts, answers,
    # bindings, decisions and failure memory are kept exactly as saved.
    state.update(status='running', agent_reason=None, input_request=None, pending_fields=[], cancel_requested=False,
                 pause_requested=False, message=None, retry_at=None, host_inflight=None, chat_revision=revision,
                 messages=[*state.get('messages', []), message])
    # Arrive at the page under the current code: Adobe-21's actions on this page ran on an older
    # tool (its refusal text sent the model to bind the questionnaire to an education row).
    # Earlier pages' history, answers, bindings and decisions are kept.
    history = state.get('decision_history') or []
    entry = max((i for i, h in enumerate(history) if h.get('tool') == 'host:auto_advance'), default=len(history) - 1)
    state['decision_history'] = history[:entry + 1]
    state['field_attempts'] = {k: v for k, v in (state.get('field_attempts') or {}).items() if k not in keys}
    state['last_tool_result'] = None
    # This test gets its own model allowance for the page (the saved run had spent 5 there).
    state['page_calls'] = {}
    if isinstance(state.get('step_budgets'), dict):
        state['step_budgets'] = {**state['step_budgets'], 'counts': {**state['step_budgets'].get('counts', {}), 'step:5': 0}}
    return state, page_text, message, keys


def scripted_model():
    """--dry: exercises the harness with no provider call (decisions here are NOT a model's)."""
    from langchain_core.messages import AIMessage, HumanMessage
    class Model:
        timeout = 30
        def invoke(self, messages, tools):
            snap = json.loads(next(m.content for m in reversed(messages) if isinstance(m, HumanMessage)))
            return AIMessage(content='', tool_calls=[{'name': 'click_page_action', 'id': str(uuid4()),
                'args': {'observation_id': snap['observation_id'], 'control_id': 'next'}}])
        invoke_turn = lambda self, messages, tools, snapshot: self.invoke(messages, tools)
    return Model


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--precheck', action='store_true')
    parser.add_argument('--dry', action='store_true')
    parser.add_argument('--max-attempts', type=int, default=8)
    parser.add_argument('--max-input-tokens', type=int, default=150000)
    parser.add_argument('--max-seconds', type=int, default=300)
    parser.add_argument('--ledger', help="continue an earlier attempt's ledger (same experiment id and caps)")
    parser.add_argument('--experiment-id')
    args = parser.parse_args()
    out = Path(args.out).resolve(); out.mkdir(parents=True, exist_ok=True)
    mode = 'precheck' if args.precheck else 'dry' if args.dry else 'paid'
    experiment_id = args.experiment_id or 'questionnaire-replay-' + out.name
    ledger_path = str(Path(args.ledger).resolve()) if args.ledger else str(out / 'ledger.sqlite3')
    os.environ.update(JOB_AGENT_EXPERIMENT_ID=experiment_id,
                      JOB_AGENT_EXPERIMENT_LEDGER=ledger_path,
                      JOB_AGENT_EXPERIMENT_MAX_PROVIDER_ATTEMPTS=str(args.max_attempts),
                      JOB_AGENT_EXPERIMENT_MAX_INPUT_TOKENS=str(args.max_input_tokens))
    if mode == 'paid':
        with (out / 'paid-started.lock').open('x') as lock:  # one paid execution per evidence folder
            lock.write(time.strftime('%Y-%m-%dT%H:%M:%S'))
    from eregion.runtime.auto_apply.agent import HybridApplicationAgent
    from eregion.runtime.auto_apply.autofill import autofill
    from eregion.runtime.provider_usage import capture_provider_attempts, usage_scope, TaskProviderBudgetExceeded, experiment_summary
    from replay_apply_field import precheck_model
    state, page_text, message, keys = prepare(args.source)
    (out / 'input.json').write_text(json.dumps({'fields': state['fields'], 'user_statement': message,
        'caps': {'provider_attempts': args.max_attempts, 'input_tokens_incl_reservation': args.max_input_tokens, 'seconds': args.max_seconds},
        'saved_decisions_on_page': {k: v for k, v in state.get('field_decisions', {}).items() if k in keys},
        'saved_attempts_on_page': {k: v for k, v in state.get('field_attempts', {}).items() if k in keys}},
        default=str, indent=1, ensure_ascii=False), encoding='utf-8')
    trace, probe = [], {}
    browser = QuestionnaireReplay(state['fields'], page_text, trace)
    started = time.monotonic()
    def checkpoint(**changes):
        state.update(changes)
    if mode == 'precheck':
        agent = HybridApplicationAgent(model_factory=precheck_model(probe))
    elif mode == 'dry':
        agent = HybridApplicationAgent(model_factory=scripted_model())
    else:
        agent = HybridApplicationAgent()
    outcome = None
    with capture_provider_attempts() as attempts:
        try:
            with usage_scope(purpose='replay_questionnaire', run_id='questionnaire-' + out.name):
              # The application loop continues after a yield; so does this replay, within its caps.
              while True:
                outcome = agent.run(browser, state, autofill=lambda: autofill(browser, state, lambda: b''),
                    active=lambda: not browser.ended and time.monotonic() - started < args.max_seconds,
                    reserve=lambda kind: True,
                    report=lambda fields, tool, key, result: trace.append({'at': time.strftime('%H:%M:%S'), 'tool': tool, 'field_key': key, 'outcome': result}),
                    communicate=lambda text: trace.append({'at': time.strftime('%H:%M:%S'), 'say': text}),
                    checkpoint=checkpoint,
                    publish_question=lambda text, k, fields, revision: trace.append({'at': time.strftime('%H:%M:%S'), 'ask_user': text, 'field_keys': k}),
                    bind_answers=lambda items, revision: trace.append({'at': time.strftime('%H:%M:%S'), 'bind_answers': items}))
                if mode != 'paid' or browser.ended or (outcome or {}).get('reason') != 'yielded' or time.monotonic() - started >= args.max_seconds:
                    break
                trace.append({'at': time.strftime('%H:%M:%S'), 'slice': 'yielded; continuing'})
        except TaskProviderBudgetExceeded as exc:
            outcome = {'reason': 'experiment_budget', 'detail': exc.reason}
        except Exception as exc:
            outcome = {'reason': 'exception', 'type': type(exc).__name__, 'detail': str(exc)[:800]}
    if mode == 'precheck':
        # The probe builds the real client inside the worker call and may finish after the loop
        # gave up waiting; it never sends, so wait for its record.
        for _ in range(120):
            if probe.get('reservation'):
                break
            time.sleep(0.5)
        (out / 'precheck.json').write_text(json.dumps(probe, default=str, indent=1), encoding='utf-8')
        print(json.dumps({'reservation': probe.get('reservation'), 'model': probe.get('model'), 'attempts': len(attempts), 'outcome': outcome}, default=str))
        return
    result = {'mode': mode, 'outcome': outcome, 'seconds': round(time.monotonic() - started, 2),
              'next_clicked': browser.next_clicks, 'verification_limited': browser.unsupported,
              'fields': {f['key']: {'label': f['label'], 'value': f.get('value'), 'required': f.get('required')} for f in browser.fields},
              'decisions': {k: v for k, v in state.get('field_decisions', {}).items() if k in keys},
              'answers': {k: v for k, v in state.get('answers', {}).items() if k in keys},
              'answer_sources': {k: v for k, v in state.get('answer_sources', {}).items() if k in keys},
              'input_request': state.get('input_request'),
              'history': [h for h in state.get('decision_history', [])],
              'attempts': [{k: a.get(k) for k in ('model', 'purpose', 'status', 'attempt', 'input_tokens', 'output_tokens',
                           'reserved_input_tokens', 'duration_seconds')} for a in attempts],
              'usage': experiment_summary(ledger_path, experiment_id)}
    (out / 'trace.json').write_text(json.dumps(trace, default=str, indent=1, ensure_ascii=False), encoding='utf-8')
    (out / 'result.json').write_text(json.dumps(result, default=str, indent=1, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('outcome', 'seconds', 'next_clicked')}, default=str))
    print('usage', json.dumps({k: result['usage'].get(k) for k in ('calls', 'input_tokens', 'output_tokens', 'unknown_usage_calls', 'retries', 'refused_before_send')}))


if __name__ == '__main__':
    main()
