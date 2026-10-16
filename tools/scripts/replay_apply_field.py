"""Replay the saved Adobe "My Experience" state through the real application agent.

No website is opened, nothing is uploaded or submitted. The browser is a replay of the saved
page: the saved fields (values included, as the agent saw them) and the candidate lists the
real Workday control returned for searches observed on 2026-10-16. A search the saved run
never made returns no results (a known limitation, reported).

Usage:
  replay_apply_field.py --source <saved data dir> --out <evidence dir> [--dry] [--max-model-calls N]
Budget: set JOB_AGENT_EXPERIMENT_* (shared ledger across processes) before a paid run; every
provider attempt, nested decision and retry is reserved and capped before sending.
"""
import argparse
import copy
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'tests')]

OBSERVED = json.loads((ROOT / 'tests/fixtures/replays/adobe_field_of_study.json').read_text(encoding='utf-8'))['observed_searches']


class ReplayBrowser:
    headless = True

    def __init__(self, fields, trace):
        self.fields, self.trace, self.ended = copy.deepcopy(fields), trace, False
        self.unsupported = []
        self.submit_clicked = False

    # Observation -------------------------------------------------------------------------
    def is_open(self): return True
    def observe(self): return copy.deepcopy(self.fields)
    def barrier(self): return None
    def field(self, key): return next(f for f in self.fields if f['key'] == key)
    def page_state(self):
        return {'url': 'https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced/job/San-Jose/R172083/apply/autofillWithResume',
                'heading': 'My Experience', 'text': 'My Experience (replay of the saved page)', 'can_submit': False,
                'authentication_required': False,
                'controls': [{'id': 'next', 'label': 'Next', 'kind': 'next', 'disabled': False},
                             {'id': 'back', 'label': 'Back', 'kind': 'back', 'disabled': False}]}
    def enrich_options(self, fields, active=None): return fields
    def pump(self): pass
    def verification_barrier(self): return False

    # Actions -----------------------------------------------------------------------------
    def _log(self, action, **detail):
        self.trace.append({'at': time.strftime('%H:%M:%S'), 'browser': action, **detail})

    def inspect_options(self, key, query=None):
        field = self.field(key)
        if query is not None and 'fieldOfStudy' in key:
            if str(query) not in OBSERVED:
                # Never invent "no results": the replay has no recording for this query.
                self.unsupported.append({'action': 'search', 'field_key': key, 'query': query})
                self._log('replay_unsupported', field_key=key, query=query)
                from eregion.runtime.auto_apply.browser import BrowserBlocked
                raise BrowserBlocked(f'Replay: no recorded website result for the search "{query}". This replay cannot '
                                     'answer it; decide from the recorded searches (search_history) or another action.')
            field['options'] = [{'label': o, 'value': o} for o in OBSERVED[str(query)]]
        self._log('inspect_options', field_key=key, query=query, results=len(field.get('options') or []))
        return field.get('options') or []

    def current_options(self, key): return self.field(key).get('options') or []
    def explore_option(self, key, label):
        self._log('explore_option', field_key=key, label=label); return []

    def choose_option(self, key, label):
        field = self.field(key)
        if not any(o['label'] == label for o in field.get('options') or []):
            from eregion.runtime.auto_apply.browser import BrowserBlocked
            raise BrowserBlocked("The option changed or is ambiguous. Inspect the field again.", code='options_changed')
        field['value'] = label; self._log('choose_option', field_key=key, label=label)

    def fill(self, field, value):
        self.field(field['key'])['value'] = value if field['kind'] != 'file' else 'resume.pdf'
        self._log('fill', field_key=field['key'], value=value if field['kind'] != 'file' else '<file>')
    fill_plain = fill
    def type_field(self, key, value):
        self.field(key)['value'] = value; self._log('type_field', field_key=key, value=value)
    def click_field(self, key): self._log('click_field', field_key=key)
    def press_key(self, key, value): self._log('press_key', field_key=key, key=value)
    def click_page_action(self, control):
        self._log('click_page_action', label=control['label'])
        if control['label'] == 'Next':
            self.ended = True  # Leaving the page ends this replay.
    def close(self): pass


def load_state(source):
    db = sqlite3.connect(Path(source) / 'eregion-apply-test.sqlite3')
    return json.loads(db.execute('select state_json from auto_applications').fetchone()[0])


def scripted_model():
    """--dry: exercises the harness with no provider call."""
    from langchain_core.messages import AIMessage, HumanMessage
    from uuid import uuid4
    plan = iter(['search', 'leave_empty', 'equivalent', 'next'])
    class Model:
        timeout = 30
        def invoke(self, messages, tools):
            snap = json.loads(next(m.content for m in reversed(messages) if isinstance(m, HumanMessage)))
            key = next(f['key'] for f in snap['fields'] if 'fieldOfStudy:1' in f['key'])
            step = next(plan, 'next')
            if step == 'search':
                call = ('search_options', {'observation_id': snap['observation_id'], 'field_key': key,
                    'source_ref': next(r for r in snap['facts'] if r['ref'].endswith('field_of_study'))['ref']})
            elif step == 'equivalent':
                key2 = next(f['key'] for f in snap['fields'] if 'fieldOfStudy:2' in f['key'])
                ref = next(r['ref'] for r in snap['facts'] if r['ref'].endswith('field_of_study') and 'Computer' in str(r['value']))
                call = ('resolve_field', {'observation_id': snap['observation_id'], 'field_key': key2,
                    'value': 'Computer and Information Science', 'mode': 'equivalent', 'source_refs': [ref], 'reason': 'dry run'})
            elif step == 'leave_empty':
                call = ('resolve_field', {'observation_id': snap['observation_id'], 'field_key': key, 'value': '',
                    'mode': 'leave_empty', 'source_refs': [], 'reason': 'dry run'})
            else:
                call = ('click_page_action', {'observation_id': snap['observation_id'], 'control_id': 'next'})
            return AIMessage(content='', tool_calls=[{'name': call[0], 'args': call[1], 'id': str(uuid4())}])
        invoke_turn = lambda self, messages, tools, snapshot: self.invoke(messages, tools)
    return Model


class PrecheckStop(Exception):
    pass


def precheck_model(record):
    """--precheck: build the first real request with the configured model wrapper and compute the
    reservation the ledger would apply; never send it and never write a ledger row."""
    from eregion.domain.model import shared_model
    from eregion.runtime.provider_usage import input_reservation
    class Probe:
        timeout = 45
        def invoke(self, messages, tools):
            real = shared_model(purpose='agent')
            bound = real.bind_tools(tools)
            payload = real._get_request_payload(messages, **getattr(bound, 'kwargs', {}))
            parts = {}
            for index, message in enumerate(messages):
                content = message.content if isinstance(message.content, str) else json.dumps(message.content, default=str)
                label = f'{index}:{type(message).__name__}'
                try:
                    snapshot = json.loads(content)
                    parts[label] = {key: len(json.dumps(value, ensure_ascii=False, default=str)) for key, value in snapshot.items()}
                    parts[label]['_total'] = len(content)
                except (ValueError, TypeError, AttributeError):
                    parts[label] = {'_total': len(content)}
            parts['tools'] = {t['function']['name']: len(json.dumps(t, ensure_ascii=False)) for t in tools}
            parts['tools']['_total'] = len(json.dumps(tools, ensure_ascii=False))
            record['parts'] = parts
            snapshot = json.loads(messages[-1].content) if isinstance(messages[-1].content, str) else {}
            record['education_fields'] = [f for f in snapshot.get('fields', []) if 'fieldOfStudy' in f['key']]
            record['next_step_blockers'] = snapshot.get('next_step_blockers')
            record['any_field_source_refs'] = len(snapshot.get('any_field_source_refs', []))
            record.update(model=getattr(real, 'model_name', None), messages=len(messages), tools=len(tools),
                          payload_bytes=len(json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8')),
                          reservation=input_reservation(payload),
                          budget_sizes=real._budget_sizes(messages, None, getattr(bound, 'kwargs', {})))
            raise PrecheckStop()
        def invoke_turn(self, messages, tools, snapshot):
            return self.invoke(messages, tools)
    return Probe


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--dry', action='store_true')
    parser.add_argument('--max-model-calls', type=int, default=6)
    parser.add_argument('--precheck', action='store_true')
    args = parser.parse_args()
    precheck = {}
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    from eregion.runtime.auto_apply.agent import HybridApplicationAgent
    from eregion.runtime.auto_apply.autofill import autofill
    from eregion.runtime.provider_usage import capture_provider_attempts, usage_scope, TaskProviderBudgetExceeded
    state = load_state(args.source)
    state.update(status='running', input_request=None, pending_fields=[], agent_reason=None)
    # Searches observed on the real Adobe control (manual read-only probe, 2026-10-15).
    cornell = next(f['key'] for f in state['fields'] if f['key'].endswith('fieldOfStudy:1'))
    state.setdefault('option_search_log', {})[cornell] = {'queries': list(OBSERVED), 'source': 'observed on Adobe 2026-10-15',
        'seen': list(dict.fromkeys(o for results in OBSERVED.values() for o in results)), 'more_pages': False}
    (out / 'input-state.json').write_text(json.dumps({k: state.get(k) for k in (
        'fields', 'answers', 'answer_sources', 'field_decisions', 'education_bindings', 'field_attempts',
        'abandoned_fields', 'autofill_failures', 'page_history', 'decision_history', 'messages')}, default=str, indent=1), encoding='utf-8')
    trace = []
    browser = ReplayBrowser(state['fields'], trace)
    reservations = {'model': 0}
    def reserve(kind):
        if kind == 'model':
            if reservations['model'] >= args.max_model_calls:
                return False
            reservations['model'] += 1
        return True
    def checkpoint(**changes):
        state.update(changes)
        trace.append({'at': time.strftime('%H:%M:%S'), 'checkpoint': sorted(changes)})
    if args.precheck:  # FastDecisions() (offline) replaces configured Jev: no nested provider call
        agent = HybridApplicationAgent(model_factory=precheck_model(precheck))
    else:
        agent = HybridApplicationAgent(model_factory=scripted_model()) if args.dry else HybridApplicationAgent()
    started = time.monotonic()
    with capture_provider_attempts() as attempts:
        try:
            with usage_scope(purpose='replay_application', run_id='replay-' + state['run_id']):
                outcome = agent.run(browser, state, autofill=lambda: autofill(browser, state, lambda: b''),
                    active=lambda: not browser.ended, reserve=reserve,
                    report=lambda fields, tool, key, result: trace.append({'at': time.strftime('%H:%M:%S'), 'tool': tool, 'field_key': key, 'outcome': result}),
                    communicate=lambda text: trace.append({'at': time.strftime('%H:%M:%S'), 'say': text}),
                    checkpoint=checkpoint,
                    publish_question=lambda text, keys, fields, revision: trace.append({'at': time.strftime('%H:%M:%S'), 'ask_user': text, 'field_keys': keys}),
                    bind_answers=lambda items, revision: trace.append({'at': time.strftime('%H:%M:%S'), 'bind_answers': items}))
        except TaskProviderBudgetExceeded as exc:
            outcome = {'reason': 'experiment_budget', 'detail': exc.reason}
    result = {'outcome': outcome, 'seconds': round(time.monotonic() - started, 1), 'model_reservations': reservations['model'],
              'left_page': browser.ended, 'verification_limited': bool(browser.unsupported), 'unsupported': browser.unsupported,
              'final_decisions': {k: v for k, v in state.get('field_decisions', {}).items() if 'fieldOfStudy' in k},
              'final_field_of_study': {f['key']: f.get('value') for f in browser.fields if 'fieldOfStudy' in f['key']},
              'decision_history': state.get('decision_history', []), 'input_request': state.get('input_request'),
              'provider_attempts': [{k: a.get(k) for k in ('model', 'purpose', 'status', 'attempt', 'input_tokens', 'output_tokens',
                  'reserved_input_tokens', 'duration_seconds')} for a in attempts]}
    if args.precheck:
        (out / 'precheck.json').write_text(json.dumps(precheck, default=str, indent=1), encoding='utf-8')
        print('precheck', json.dumps({**precheck, 'provider_attempts': len(result['provider_attempts'])}, default=str))
        return
    (out / 'trace.json').write_text(json.dumps(trace, default=str, indent=1), encoding='utf-8')
    (out / 'result.json').write_text(json.dumps(result, default=str, indent=1), encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('outcome', 'seconds', 'model_reservations', 'left_page', 'final_field_of_study')}, default=str))
    print('provider attempts:', len(result['provider_attempts']))


if __name__ == '__main__':
    main()
