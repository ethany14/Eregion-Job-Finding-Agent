"""Application capability host for the shared decision kernel."""
import base64
import logging
import time

from langchain_core.messages import HumanMessage

from eregion.runtime.core.context import compact, trace_value  # compatibility imports
from eregion.runtime.core.engine import AgentLoop, LoopStop, ToolArgumentsError, Turn
from eregion.runtime.core.model import ModelInterrupted, ModelRequests
from eregion.runtime.model_runtime import model_failure
from eregion.runtime.telemetry import outcome_fingerprint, safe_traceback, timing_update, model_metrics
from .browser import BrowserBlocked
from .errors import failure_detail, failure as action_failure
from .prompt import SYSTEM

# Veeva pilot: ~23 tries on one optional school type-ahead. Past this many unsuccessful
# actions on a field (reset by each user message), optional fields are left empty and
# required ones go back to the user.
FIELD_ATTEMPT_LIMIT = 4
# Adobe Workday spent 523 model calls, most on one page, because the step budget resets on
# every message. One page gets this many calls per user message, then the user is asked.
PAGE_MODEL_CALL_LIMIT = 30
NON_FIELD_ACTIONS = {'observe', 'view_browser', 'say', 'ask_user', 'request_input', 'read_context', 'record_answers',
    'update_plan', 'computer_wait', 'draft_answer'}

logger = logging.getLogger(__name__)


class ApplicationHost:
    def __init__(self, session, model, *, active, reserve, report, refresh=None, communicate=None, checkpoint=None, decisions=None):
        self.session, self.model, self.browser = session, model, session.browser
        self.is_active, self.reserve, self.report = active, reserve, report
        self.refresh, self.communicate, self.checkpoint = refresh, communicate, checkpoint
        self.requests, self.deadline = ModelRequests(), time.monotonic() + 180
        self.history = list(session.state.get('decision_history', []))
        self.pending, self.started, self.fingerprint = {}, None, None
        self.context_archive = None
        self.text_turns = 0
        self.first_observation = True
        self.host_autofill = None
        self.visual_requested, self.pending_screen = False, None
        session.verify_answers = self.verify_answers
        saved = session.state.get('loop_runtime', {}).get('application', {})
        self.autofill_fingerprint = saved.get('autofill_fingerprint')
        self.advanced_pages = set(saved.get('advanced_pages', []))
        session.autofill_called = saved.get('autofill_called', False)
        session.failed_fields = set(saved.get('failed_fields', []))
        from eregion.runtime.fast_decisions import configured_decisions
        self.fast = configured_decisions() if decisions is None else decisions
        session.fast_option_attempts = set(saved.get('fast_option_attempts', []))
        session.fast_choices = self.fast_choices if self.fast.enabled else None

    def active(self):
        return self.is_active()

    def revision(self):
        if self.refresh and hasattr(self.refresh, 'revision'):
            return self.refresh.revision()
        state = self.refresh() if self.refresh else self.session.state
        return state.get('chat_revision', 0)

    def timed(self, stage, started, **metadata):
        changes = timing_update({**self.session.state, **self.pending}, stage, time.monotonic() - started, **metadata)
        self.session.state.update(changes)
        self.pending.update(changes)

    def persist(self, runtime):
        runtime['application'] = {'autofill_fingerprint': self.autofill_fingerprint,
                                  'advanced_pages': sorted(self.advanced_pages)[-80:],
                                  'autofill_called': self.session.autofill_called,
                                  'failed_fields': sorted(self.session.failed_fields),
                                  'fast_option_attempts': sorted(self.session.fast_option_attempts)[-80:]}
        self.session.state['loop_runtime'] = runtime
        if self.checkpoint:
            self.checkpoint(**self.pending, loop_runtime=runtime)
        self.pending.clear()

    def observe(self):
        from .agent import HIDDEN_TOOLS, TOOLS, VISUAL_TOOLS
        self.fingerprint, self.started = None, None
        snapshot = self._prepare_page()
        check = getattr(self.browser, 'verification_barrier', None)
        if callable(check) and check():
            raise LoopStop('verification_required', chat_revision=snapshot.get('chat_revision', 0))
        if (snapshot.get('page') or {}).get('authentication_required'):
            raise LoopStop('authentication_required', chat_revision=snapshot.get('chat_revision', 0))
        if time.monotonic() >= self.deadline:
            raise LoopStop('yielded', chat_revision=snapshot.get('chat_revision', 0))
        images = []
        snapshot['screen'] = None
        if not self.session.state.get('visual_fallback'):
            self.session.visual = None
            self.visual_requested, self.pending_screen = False, None
        elif self.visual_requested:
            # Only explicit viewing, visual actions, or DOM recovery need pixels.
            # Reuse an explicitly requested capture only while its token is fresh
            # after host autofill/navigation; otherwise capture the current page.
            screen = self.pending_screen
            self.visual_requested, self.pending_screen = False, None
            if screen is not None:
                try:
                    self.session.visual.validate(screen['screen']['screenshot_id'])
                except BrowserBlocked:
                    screen = None
            if screen is None:
                screen = self.session._view_browser()
            images = self.image_message(screen['image'])
            snapshot['screen'] = screen['screen']
        elif self.session.visual is not None:
            # No pixels in this turn means no reusable coordinate authority.
            self.session.visual.invalidate()
        from .autofill import desired_answer, matches, pending_documents
        # Affordances follow observed remaining work, not the model's intent.
        # Legacy direct callers keep execution checks; current models are not
        # invited to repeat a host action which has already filled every fact.
        fill_available=bool(pending_documents(self.session.state,self.session.fields)) or any(
            desired_answer(f,self.session.state) is not None and not matches(f,desired_answer(f,self.session.state))
            for f in self.session.fields if f['kind']!='file')
        snapshot['autofill_available']=fill_available
        tools = [item for item in TOOLS if item['function']['name'] not in HIDDEN_TOOLS
                 and (self.session.state.get('visual_fallback') or item['function']['name'] not in VISUAL_TOOLS)
                 and (fill_available or item['function']['name']!='autofill')]
        if self.host_autofill is not None:
            snapshot['host_autofill'] = self.host_autofill
        return Turn(SYSTEM, snapshot, tools, snapshot.get('chat_revision', 0), images,
            batchable=frozenset({'type_from_source', 'choose_option', 'resolve_field', 'record_answers',
                'use_remembered_answer', 'bind_document', 'bind_education', 'draft_answer', 'say',
                'update_plan', 'search_options', 'inspect_field'}),
            compatibility_tools=[t for t in TOOLS if t['function']['name'] in {'observe', 'use_chat_answer'} | ({'autofill'} if not fill_available else set())])

    def validate_batch(self, calls, turn):
        seen = set()
        observed = {f['key'] for f in self.session.fields}
        for call in calls:
            args = call['args']
            if any(not isinstance(item, dict) for key in ('answers', 'fields') for item in args.get(key) or []):
                raise ToolArgumentsError(f"{call['name']}: each item in answers/fields must be an object with a field_key.")
            if call['name']=='bind_education':
                from .education import combined_bindings
                args = {**args, 'fields':combined_bindings(args.get('fields', []))}
            keys = [args['field_key']] if args.get('field_key') else []
            keys += [item.get('field_key') for key in ('answers', 'fields') for item in args.get(key, [])]
            for key in keys:
                if key not in observed:
                    raise ToolArgumentsError('Observe a newly revealed field before batching an action for it.')
                if key in seen:
                    raise ToolArgumentsError('Batch rejected: use each field only once per turn.')
                seen.add(key)

    def _page_fingerprint(self):
        page = self.session.page_context or {}
        return outcome_fingerprint([page.get('url'), page.get('heading'), sorted(f['key'] for f in self.session.fields)])

    def _host_action(self, name, args, snapshot):
        turn = Turn(SYSTEM, snapshot, [], snapshot.get('chat_revision', 0))
        call = {'id': 'host-' + str(time.monotonic_ns()), 'name': name, 'args': args}
        # Journal before an automatic browser effect just like a model action.
        if not self.active() or self.revision() != turn.revision:
            raise LoopStop('steered', chat_revision=turn.revision)
        if self.checkpoint:
            self.checkpoint(host_inflight={'tool': name, 'page': self._page_fingerprint()})
        try:
            result = self.execute(call, turn)
        except BrowserBlocked as exc:
            result = self.action_error(exc)
        result.pop('unchanged', None)
        self.after_action({**call, 'name': 'host:auto_advance' if name == 'click_page_action' else 'host:autofill'}, result, turn)
        if self.checkpoint:
            self.checkpoint(host_inflight=None)
        return result

    def _scan(self):
        started = time.monotonic()
        snapshot = self.session.observe()
        self.timed('observation', started)
        from .agent import application_history
        snapshot['decision_history'] = application_history(self.history)
        return snapshot

    def _prepare_page(self):
        from .autofill import desired_answer, matches, pending_documents
        from .drafting import visible_drafts
        from .education import gaps
        from .matching import is_consent
        for _ in range(12):
            snapshot = self._scan()
            session, page = self.session, self.session.page_context or {}
            if not self.active() or self.revision() != snapshot.get('chat_revision', 0):
                raise LoopStop('steered', chat_revision=snapshot.get('chat_revision', 0))
            check = getattr(self.browser, 'verification_barrier', None)
            if page.get('authentication_required') or (callable(check) and check()):
                return snapshot
            fingerprint = self._page_fingerprint()
            if self.first_observation or fingerprint != self.autofill_fingerprint:
                self.first_observation = False
                self.autofill_fingerprint = fingerprint
                result = self._host_action('autofill', {}, snapshot)
                self.host_autofill = {k: result.get(k, {} if k == 'failed' else []) for k in ('filled', 'failed')}
                snapshot = self._scan()
                page = session.page_context or {}
            if session.state.get('input_request'):
                return snapshot
            incomplete = any(f.get('invalid') or (f['required'] and not f['value']) or
                (is_consent(f) and session.state.get('consent_policy', 'ask') == 'ask' and f['key'] not in session.state['answers']) or
                (desired_answer(f, session.state) is not None and not matches(f, desired_answer(f, session.state)))
                for f in session.fields if f['kind'] != 'file')
            if incomplete or session._unresolved_paths() or visible_drafts(session.state, session.fields) or gaps(session.state, session.fields, page) or pending_documents(session.state, session.fields):
                return snapshot
            if page.get('can_submit') and any(not f.get('value') and f['kind'] != 'file' for f in session.fields):
                # Let the model decide whether optional unanswered fields can be skipped.
                return snapshot
            if page.get('can_submit'):
                try:
                    session.execute('ready_for_review', {})
                except BrowserBlocked:
                    return snapshot
                raise LoopStop('ready', chat_revision=snapshot.get('chat_revision', 0))
            controls = [c for c in page.get('controls', []) if c['kind'] == 'next' and not c.get('disabled')]
            if len(controls) != 1 or fingerprint in self.advanced_pages:
                return snapshot
            self.advanced_pages.add(fingerprint)
            result = self._host_action('click_page_action', {'observation_id': session.observation_id,
                'control_id': controls[0]['id']}, snapshot)
            if result.get('error'):
                return self._scan()
            fresh = self._scan()
            if self._page_fingerprint() == fingerprint:
                return fresh
            if time.monotonic() >= self.deadline:
                raise LoopStop('yielded', chat_revision=fresh.get('chat_revision', 0))
        return self._scan()

    def invoke(self, messages, tools, turn):
        page = self.session.page_context or {}
        page_id = [page.get('url'), page.get('heading'), turn.revision]
        record = self.session.state.get('page_calls') or {}
        if record.get('page') != page_id:
            record = {'page': page_id, 'count': 0}
        if record['count'] >= PAGE_MODEL_CALL_LIMIT:
            raise LoopStop('page_budget')
        if not self.reserve('model'):
            raise LoopStop('agent_limit')
        record['count'] += 1
        self.session.state['page_calls'] = self.pending['page_calls'] = record
        started = time.monotonic()
        try:
            result = self.requests.call(lambda: self.model.invoke_turn(messages, tools, turn.snapshot) if hasattr(self.model, 'invoke_turn') else self.model.invoke(messages, tools),
                active=lambda: self.active() and self.revision() == turn.revision,
                timeout=min(getattr(self.model, 'timeout', 45) + 2, max(.01, self.deadline - time.monotonic())),
                pump=getattr(self.browser, 'pump', None))
        except ModelInterrupted:
            return None
        except Exception as exc:
            self.timed('model', started, failed=True)
            failure = model_failure(exc)
            self.pending.pop('last_tool_result', None)
            if self.checkpoint:
                self.checkpoint(last_tool_result={'tool': 'model', 'result': {'error': failure.error_type, 'status': failure.status}})
            raise LoopStop(failure.code, **({'retry_after': failure.retry_after} if failure.retry_after else {})) from exc
        self.timed('model', started, **model_metrics(messages, tools, result))
        return result

    def fast_choices(self, state, questions):
        from eregion.runtime.fast_decisions import DecisionBatch
        if not self.reserve('model'):
            return DecisionBatch(status='budget')
        revision, started = self.revision(), time.monotonic()
        result = DecisionBatch(status='interrupted')
        try:
            result = self.requests.call(
                lambda: self.fast.evaluate(state, questions, purpose='form_options'),
                active=lambda: self.active() and self.revision() == revision,
                timeout=min(self.fast.timeout + .5, max(.01, self.deadline - time.monotonic())),
                pump=getattr(self.browser, 'pump', None))
            return result
        except (ModelInterrupted, TimeoutError):
            return result
        finally:
            metadata = result.metadata()
            metadata['provider_seconds'] = metadata.pop('seconds')
            self.timed('fast_options', started, failed=result.status != 'ok', **metadata)

    def verify_answers(self, answers):
        if not hasattr(self.model, 'verify_answers'):
            raise BrowserBlocked('Semantic answer verification is unavailable; use an explicit field reply.', code='verification_unavailable')
        if not self.reserve('model'):
            raise LoopStop('agent_limit', stage='answer_verification')
        started, revision = time.monotonic(), self.revision()
        failed, error_code = True, None
        try:
            result = self.requests.call(lambda: self.model.verify_answers(answers),
                active=lambda: self.active() and self.revision() == revision,
                timeout=min(47, max(.01, self.deadline - time.monotonic())), pump=getattr(self.browser, 'pump', None))
            failed = False
            return result
        except ModelInterrupted as exc:
            error_code = 'stale_observation'
            raise BrowserBlocked('Read the latest user reply before interpreting answers.', code='stale_observation') from exc
        except Exception as exc:
            failure = model_failure(exc)
            error_code = failure.code
            # Use the existing bounded provider recovery path. Returning a tool
            # error here made the model rewrite and resubmit the same true fact.
            raise LoopStop(failure.code, chat_revision=revision,
                stage='answer_verification',
                **({'retry_after': failure.retry_after} if failure.retry_after else {})) from exc
        finally:
            self.timed('answer_verification', started, failed=failed, error_code=error_code)

    def progress(self, text, call, turn):
        if text and self.communicate and text != call['args'].get('text') and self.revision() == turn.revision:
            self.communicate(text[:1500])

    def on_text(self, text, turn):
        if self.communicate and self.revision() == turn.revision:
            self.communicate(text[:1500])
        pending = self.session.state.get('input_request')
        if pending:
            return {'reason': 'needs_input', 'field_keys': pending['field_keys'], 'question': pending['text'], 'chat_revision': turn.revision}
        # Plain replies are visible. The application host verifies completeness;
        # an incomplete page receives feedback instead of fabricating a question.
        try:
            self.session.execute('ready_for_review', {})
            return {'reason': 'ready', 'chat_revision': turn.revision}
        except BrowserBlocked as exc:
            self.text_turns += 1
            if self.text_turns >= 3:
                return {'reason': 'no_progress', 'chat_revision': turn.revision}
            return {'message_delivered': True, 'next': 'Continue with browser/field tools, or ask_user for genuinely missing facts.',
                    'incomplete': str(exc)}

    def execute(self, call, turn):
        name, args = call['name'], call['args']
        if not self.active() or self.revision() != turn.revision:
            raise LoopStop('steered', chat_revision=turn.revision)
        if not self.reserve('tool'):
            raise LoopStop('agent_limit')
        self.started = time.monotonic()
        session = self.session
        self.fingerprint = outcome_fingerprint([name, args,
            [{k: f.get(k) for k in ('key', 'value', 'options', 'errors', 'invalid')} for f in session.fields],
            session.page_context, turn.revision,
            {key: session.state.get(key) for key in ('answers', 'answer_sources', 'field_decisions', 'education_bindings',
                'education_answer_overrides', 'document_bindings', 'document_refresh')}])
        failures = sum(h.get('fingerprint') == self.fingerprint and h.get('failed', False) for h in self.history)
        if failures >= 5:
            raise LoopStop('no_progress', field_keys=[args['field_key']] if args.get('field_key') else [])
        if failures >= 2 and name not in {'observe', 'view_browser', 'say', 'ask_user', 'request_input', 'computer_wait'}:
            return {**action_failure('repeated_action', 'This failed action was not executed again because the page and inputs are unchanged.'), 'repeated_action': True}
        key = args.get('field_key') if isinstance(args.get('field_key'), str) else None
        field = next((f for f in session.fields if f['key'] == key), None) if key else None
        if field is not None and name not in NON_FIELD_ACTIONS and self.attempts(field) >= FIELD_ATTEMPT_LIMIT:
            if field.get('required'):
                raise LoopStop('no_progress', field_keys=[key])
            return {**action_failure('field_attempt_limit', f'{FIELD_ATTEMPT_LIMIT} attempts did not fill this optional field and '
                'nothing relevant has changed. Leave it empty or ask the user for a different answer; it does not block review.'),
                'abandoned': key}
        self.text_turns = 0
        session._cached_fields = None
        outcome = session.execute(name, args)
        if (name == 'view_browser' or name.startswith('computer_')) and isinstance(outcome.get('image'), bytes):
            # Host.observe owns screenshot delivery and freshness. Do not also
            # place this image in the kernel's generic one-turn image queue.
            self.pending_screen = dict(outcome)
            outcome = {key: value for key, value in outcome.items() if key != 'image'}
        return outcome

    def _target(self, field):
        """What a field is being filled towards: the desired answer and its current options.
        A new fact or new choices starts a fresh count; a plain "continue" does not (W1)."""
        from .autofill import desired_answer
        return outcome_fingerprint([desired_answer(field, self.session.state),
                                    [o.get('label') for o in field.get('options') or []]])

    def attempts(self, field):
        entry = (self.session.state.get('field_attempts') or {}).get(field['key'])
        return entry['count'] if isinstance(entry, dict) and entry.get('target') == self._target(field) else 0

    def count_attempt(self, field):
        record = self.session.state.setdefault('field_attempts', {})
        count = self.attempts(field) + 1
        record[field['key']] = {'target': self._target(field), 'count': count}
        self.pending['field_attempts'] = record
        abandoned = [k for k in self.session.state.get('abandoned_fields', []) if k != field['key']]
        if count >= FIELD_ATTEMPT_LIMIT and not field.get('required'):
            # Autofill skips it too; otherwise each round re-reports it as failed (Adobe).
            abandoned.append(field['key'])
        self.session.state['abandoned_fields'] = self.pending['abandoned_fields'] = abandoned

    def action_error(self, exc):
        if isinstance(exc, ToolArgumentsError):
            return action_failure('invalid_action', str(exc))
        return failure_detail(exc)

    def after_action(self, call, outcome, turn):
        name, args = call['name'], call['args']
        failed = ('error' in outcome or bool(outcome.get('failed')) or outcome.get('verified') is False or bool(outcome.get('unchanged')))
        if failed and not outcome.get('error_code') and not outcome.get('failure_details'):
            code = 'repeated_action' if outcome.get('unchanged') else 'value_not_retained' if outcome.get('verified') is False else 'invalid_action'
            outcome.update({**action_failure(code, outcome.get('error') or outcome.get('next') or 'Inspect the failed controls before retrying.'), **outcome})
        visual = outcome.get('visual_recovery') or any(d.get('visual_recovery') for d in outcome.get('failure_details', {}).values())
        if self.session.state.get('visual_fallback') and hasattr(self.browser, 'page'):
            if visual or name == 'view_browser' or name.startswith('computer_'):
                self.visual_requested = True
            # observe consumes this once. A later successful DOM action in the
            # same batch must not erase an earlier control's recovery request.
        self.timed('tool', self.started or time.monotonic(), tool=name, failed=failed)
        self.started = None
        context_result = self.context_archive.tool_result(outcome) if self.context_archive is not None else trace_value(outcome)
        self.history = [*self.history, {'tool': name, 'call_id': call['id'], 'arguments': trace_value(args), 'result': context_result,
            'fingerprint': self.fingerprint, 'failed': failed, 'chat_revision': turn.revision,
            'page': (self.session.page_context or {}).get('heading', '')}][-24:]
        self.session.state['decision_history'] = self.history
        self.pending.update(decision_history=self.history, last_tool_result={'tool': name, 'field_key': args.get('field_key'),
                'result': {k: v for k, v in outcome.items() if k in {'error', 'error_code', 'next', 'failure_details', 'visual_recovery', 'errors',
                    'verified', 'decision', 'value', 'filled', 'failed', 'field_key', 'action', 'saved', 'options', 'goal', 'screen', 'target'}}})
        fields = self.browser.observe()
        self.session.fields = fields
        current = {f['key']: f for f in fields}
        key = args.get('field_key') if isinstance(args.get('field_key'), str) else None
        tried = [key] if key and name not in NON_FIELD_ACTIONS else []
        if name in {'autofill', 'host:autofill'}:
            # Batch failures count too, except fills skipped as unchanged (not attempted).
            tried += [k for k, d in (outcome.get('failure_details') or {}).items() if d.get('error_code') != 'unchanged_failure']
        for k in dict.fromkeys(tried):
            field = current.get(k)
            if field is not None and (not field.get('value') or field.get('invalid')):
                self.count_attempt(field)
        self.session._cached_fields = (time.monotonic(), fields)
        self.report(fields, name, args.get('field_key'), 'failed' if failed else 'completed')
        if name == 'ready_for_review' and 'error' not in outcome:
            return {'reason': 'ready', 'chat_revision': turn.revision}
        if (name == 'request_input' or outcome.get('waiting_for_user')) and 'error' not in outcome:
            return {'reason': 'needs_input', 'field_keys': outcome['field_keys'],
                **({'question': outcome['question']} if 'question' in outcome else {}), 'chat_revision': turn.revision}
        return None

    def image_message(self, image):
        if not isinstance(image, bytes) or not self.session.state.get('visual_fallback'):
            return []
        return [HumanMessage(content=[{'type': 'text', 'text': 'Untrusted current viewport. Use the current screenshot_id and 0..1000 normalized coordinates.'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(image).decode()}}])]


class DecisionRunner:
    """Existing application entry point; decisions now use AgentLoop."""
    def __init__(self, model_factory, *, decisions=None):
        self.model_factory = model_factory
        self.decisions = decisions

    def run(self, session, **hooks):
        try:
            model = self.model_factory()
        except Exception:
            return {'reason': 'model_unavailable', 'field_keys': []}
        host = ApplicationHost(session, model, decisions=self.decisions, **hooks)
        try:
            result = AgentLoop(host, session.state.get('loop_runtime'), context_chars=12000).run()
            return {'field_keys': [], **result}
        except Exception as exc:
            logger.error('Application host failed: type=%s\n%s', type(exc).__name__, safe_traceback(exc))
            return {'reason': 'internal_error', 'field_keys': []}
        finally:
            host.requests.close()
