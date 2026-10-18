"""Repository-backed conversations hosted by the shared decision kernel."""
import base64
import time
from contextlib import contextmanager

from langchain_core.messages import HumanMessage

from eregion.runtime.core.context import trace_value
from eregion.runtime.core.model import ModelRequests
from eregion.runtime.core.engine import LoopStop, ToolArgumentsError, Turn
from eregion.runtime.telemetry import outcome_fingerprint


class TaskHost:
    def __init__(self, loop, task_id, model):
        self.loop, self.task_id, self.model = loop, task_id, model
        self.stage, self.last_revision = 'observation', self.revision()
        self.started = None
        self.requests = ModelRequests()
        self.context_archive = None

    def state(self):
        return self.loop.repository.get(self.task_id)

    def active(self):
        state = self.state()
        return self.loop._active(state) and state['status'] not in {'needs_input', 'waiting'}

    def revision(self):
        return self.state()['chat_revision']

    def persist(self, runtime):
        self.loop.repository.change(self.task_id, loop_runtime=runtime)

    def observe(self):
        from eregion.runtime.tasks.loop import CONTROL_TOOLS
        self.stage = 'observation'
        state = self.state()
        self.last_revision = state['chat_revision']
        if state['model_calls'] - state['budget_base'] >= self.loop._budget(state):
            self.loop.repository.append(self.task_id, role='assistant', text='I have saved the work so far. Send continue to allow more steps.')
            self.loop.repository.change(self.task_id, status='needs_input', model_activity=None)
            raise LoopStop('agent_limit')
        state = self.loop.repository.change(self.task_id, status='running')
        started = time.monotonic()
        observed = self.loop.harness.observe(state)
        self.loop._timing(self.task_id, 'observation', started, state['chat_revision'])
        state['capability_versions'] = observed.get('capability_versions', {})
        self.observed_state = state
        # Reconstruct from durable messages, including legacy tasks after restart.
        # The original creation goal is identity/history, not the current order.
        latest_user = next((m for m in reversed(state['messages']) if m.get('role') == 'user'), None)
        current_request = {'revision': state['chat_revision'], 'message': latest_user}
        snapshot = {'goal': latest_user['text'] if latest_user else state['goal'],
            'initial_goal': state['goal'], 'current_request': current_request,
            'messages': state['messages'][-60:], 'plan': state['plan'],
            'history': state['history'][-16:], 'pending_question': state.get('input_request'),
            'visual_fallback': state.get('visual_fallback', False),
            'budget_remaining': self.loop._budget(state) - state['model_calls'] + state['budget_base'], **observed}
        harness = self.loop.harness
        tools = harness.available_tools(state) if hasattr(harness, 'available_tools') else harness.tools
        controls = CONTROL_TOOLS
        guard=state.get('progress_guard',{})
        if guard.get('revision')==state['chat_revision'] and guard.get('count',0)>=2:
            snapshot['must_decide']=True
            snapshot['decision_reason']='Repeated reads added no evidence. Use saved results to finish honestly, ask for essential missing input, or take a different action/source. Do not claim unfinished work is complete.'
            reads={'read_context','browser_read','browser_screenshot','list_jobs','inspect_job',
                'inspect_materials','inspect_interview','inspect_application',
                'browser_scroll','browser_wait','screen_jobs'}
            tools=[t for t in tools if t['function']['name'] not in reads]
            controls=[t for t in controls if t['function']['name'] in {'finish_task','ask_user','pause_task'}]
        # The task's provider-attempt cap is shared with tool work (qualification,
        # Jev). Near it, the remaining attempts are for decisions and the reply.
        from eregion.runtime.provider_usage import task_limits, UsageLedger, REPLY_RESERVE
        cap=task_limits().get('provider_attempts')
        if cap and cap>2*REPLY_RESERVE:
            provider_remaining=cap-UsageLedger(self.loop.repository.sessions).summary(task_id=self.task_id)['calls']
            snapshot['provider_attempts_remaining']=provider_remaining
            if provider_remaining<=REPLY_RESERVE:
                snapshot['completion_required']=True
                snapshot['completion_reason']=('The provider budget is nearly spent: no further paid search, reading or scoring. '
                    + ('Record decisions for already scored jobs if needed, then finish. ' if provider_remaining>2 else
                       'Finish now; the remaining call is only for correcting a rejected reply. ')
                    + __import__('eregion.runtime.career.prompt',fromlist=['FINAL_FORMAT']).FINAL_FORMAT)
                decisions=getattr(harness,'batch_decision_tools',set())-{'read_context','dismiss_candidate'}
                from eregion.runtime.career.decisions import pending as undecided
                open_decisions=bool(undecided(state)) if hasattr(harness,'job_summary') else True
                tools=[t for t in tools if provider_remaining>2 and open_decisions and t['function']['name'] in decisions]
                controls=[t for t in CONTROL_TOOLS if t['function']['name']=='finish_task']
        if snapshot['budget_remaining']<=3:
            snapshot['completion_required']=True
            snapshot['completion_reason']='Use the remaining decisions to report the observed results and explicit unfinished work. No additional research or rereading. Do not claim requested constraints were met when they were not. Ask only for genuinely essential missing user input.'
            tools=[]
            controls=[t for t in CONTROL_TOOLS if t['function']['name'] in {'finish_task','ask_user'}]
        from eregion.runtime.request_options import enabled
        prompt = harness.prompt
        if enabled('PROMPT_TRIM'):
            from eregion.runtime.career.prompt import trim_prompt
            prompt = trim_prompt(prompt)
        descriptions = {}
        if hasattr(harness, 'api_first') and harness.api_first():
            # One switch: prompt wording, tool descriptions and snapshot policy agree.
            from eregion.runtime.career import source_mode
            prompt = source_mode.prompt_for(prompt, True)
            descriptions = source_mode.tool_descriptions(True)
        if any(t['function']['name'] == 'verify_and_score' for t in tools):
            from eregion.runtime.career.workflows import RULE
            prompt += RULE
        stable = enabled('STABLE_TOOLS') and hasattr(harness, 'stable_tools')
        return Turn(prompt, snapshot, CONTROL_TOOLS + harness.stable_tools(state) if stable else controls + tools,
                    state['chat_revision'], allowed_tools=controls + tools if stable else None,
                    batchable=frozenset(getattr(harness, 'batchable_tools', ())) | frozenset(getattr(harness, 'batch_decision_tools', ())),
                    tool_descriptions=descriptions)

    def invoke(self, messages, tools, turn):
        self.stage = 'model_request'
        return self.loop._invoke_model(self.task_id, self.model, messages, tools, turn.revision, self.requests)

    def progress(self, text, call, turn):
        with self.loop.lock:
            if not self.active() or self.revision() != turn.revision:
                raise LoopStop('steered')
            if text and not (call['name'] in {'say', 'ask_user', 'finish_task', 'pause_task'} and text == call['args'].get('text')):
                self.loop._progress(self.task_id, text[:4000])

    @contextmanager
    def text_guard(self, turn):
        with self.loop.lock:
            if not self.active() or self.revision() != turn.revision:
                raise LoopStop('steered')
            yield

    def on_text(self, text, turn):
        with self.loop.lock:
            if not self.active() or self.revision() != turn.revision:
                raise LoopStop('steered')
            state = self.state()
            if state.get('input_request'):
                self.loop._progress(self.task_id, text[:4000])
                self.loop.repository.change(self.task_id, status='needs_input')
                return {'reason': 'needs_input'}
            # Completion is an explicit control decision for every revision,
            # including its first model response. A promise cannot become a
            # completed task just because no capability has run yet. Ordinary
            # answers use finish_task directly, without unrelated tool work.
            self.loop._progress(self.task_id, text[:4000])
            if self.no_progress(turn, True):
                return {'reason': 'no_progress'}
            return {'next': 'Your text was delivered as progress, not task completion. '
                'Reconcile current_request with observed outcomes. Take the next useful action, '
                'wait for an active capability, or explicitly call finish_task with your answer '
                'or completed result and evidenced limitations. A promise is not a completed action. '
                'For ordinary conversation or questions requiring no tools, finish_task can directly deliver the answer.',
                'message_delivered': True, 'completion_required': True}

    def no_progress(self, turn, stalled, batched=False):
        """A durable per-request circuit breaker, independent of loop slices."""
        with self.loop.lock:
            return self._no_progress_locked(turn,stalled,batched)

    def _no_progress_locked(self,turn,stalled,batched=False):
        state = self.state()
        if state['chat_revision'] != turn.revision or not self.active():return False
        prior = state.get('progress_guard', {})
        count = prior.get('count', 0) if prior.get('revision') == turn.revision else 0
        # Several independent (batchable) actions of one model reply are one decision:
        # a batch of repeated actions counts once toward the stall limit, not per call.
        reply = getattr(turn, 'reply_id', None)
        if batched and stalled and prior.get('revision') == turn.revision and prior.get('reply') == reply:
            return False
        count = count + 1 if stalled else 0
        self.loop.repository.change(self.task_id, progress_guard={'revision':turn.revision,'count':count,'reply':reply})
        if count < 3:return False
        # Deliver what was actually saved, without another model call.
        delivery = getattr(self.loop.harness, 'terminal_delivery', None)
        lines = delivery(state) if delivery else []
        self.loop.repository.append(self.task_id, role='assistant', text=(
            'I stopped because repeated steps produced no new evidence. Saved results are preserved; '
            'the requested work is not yet complete. Open the saved results or retry with a different source or approach.'
            + (chr(10)*2 + 'Saved so far:' + chr(10) + chr(10).join(lines) if lines else '')))
        self.loop.repository.change(self.task_id, status='needs_input', model_activity=None,
            error={'stage':'decision','code':'no_progress','retryable':True,
                   'message':'Repeated steps produced no new evidence. Saved work is preserved; the request is incomplete.'})
        return True

    def execute(self, call, turn):
        from eregion.runtime.tasks.loop import now
        self.stage = 'decision'
        self.started = time.monotonic()
        with self.loop.lock:
            if not self.active() or self.revision() != turn.revision:
                raise LoopStop('steered')
            self.loop.repository.change(self.task_id, tool_activity={'tool': call['name'], 'started_at': now(),
                'chat_revision': turn.revision, 'message': f'Running {call["name"].replace("_", " ")}.'})
            state = self.state()
            failures = [h for h in state['history'] if h.get('chat_revision') == turn.revision
                        and h.get('tool') == call['name'] and h.get('arguments') == trace_value(call['args'])
                        and h.get('result', {}).get('error_code') in
                        {'model_timeout', 'model_unavailable', 'model_rate_limited', 'evaluation_schema',
                         'evaluation_grounding', 'interview_failed', 'retry_exhausted'}]
            if len(failures) >= 2:
                return {'error': 'The same inputs already failed twice in this user turn.',
                    'error_code': 'retry_exhausted', 'retryable': False,
                    'next': 'Keep saved progress. Continue another task/source or explain the service failure. Retry after new user input or an actual input change; do not ask the user to repeat saved facts.'}
            result = self.loop._control(self.task_id, call['name'], call['args'], self.observed_state)
        if result is None:
            # A fresh observation token must not make an already-stalled click
            # executable again on the same unchanged source. New targets/pages
            # remain available for recovery, as does the bounded read_job tool.
            if call['name'] in {'browser_click','browser_open','browser_type','browser_select','browser_key','browser_back'}:
                browser=state.get('browser') or {}
                for h in state['history']:
                    previous=h.get('result') or {}
                    if (h.get('chat_revision')==turn.revision and h.get('tool')==call['name'] and
                        isinstance(previous,dict) and (previous.get('no_new_evidence') or h.get('no_new_evidence')) and
                        outcome_fingerprint(h.get('arguments',{}))==outcome_fingerprint(call['args']) and
                        (h.get('browser_url') or previous.get('url') or browser.get('url'))==browser.get('url') and
                        (not h.get('browser_source_hash') or h['browser_source_hash']==browser.get('page_evidence_hash'))):
                        return {'error':'This action already produced no new evidence on this unchanged page.',
                            'error_code':'repeated_action','no_new_evidence':True,
                            'next':'Use the current bounded read_job action, another observed target, or a different public source.'}
            guard=state.get('progress_guard',{})
            if guard.get('revision')==turn.revision and guard.get('count',0)>=2 and any(
                    h.get('tool')==call['name'] and h.get('arguments')==trace_value(call['args']) and
                    h.get('chat_revision')==turn.revision for h in state['history']):
                return {'error':'This exact action already ran without resolving the request.',
                    'error_code':'repeated_action','no_new_evidence':True,
                    'next':'Use a different search query/source or finish with the actual limitation. Do not repeat these arguments.'}
            from eregion.runtime.provider_usage import usage_scope, ReplyBudgetReserved
            try:
                with usage_scope(purpose=call['name']):
                    result = self.loop.harness.execute(self.task_id, call['name'], call['args'], call_id=call['id'])
            except ReplyBudgetReserved as exc:
                # Saved progress stays; only new paid tool work is refused.
                # A budget transition, not a stall: the next observation is already
                # restricted to decisions and the reply, so it must not end the loop.
                return {'error':'The remaining provider budget is reserved for your decisions and final reply.',
                        'error_code':'provider_budget_reserved',
                        **getattr(exc,'workflow_detail',{}),
                        'next':'Start no further paid work. Decide any scored jobs (free), then finish with the saved '
                               'recommendations, their URLs and scores, and the honest shortfall.'}
        return result

    def action_error(self, exc):
        return {**self._action_error(exc), **getattr(exc, 'workflow_detail', {})}

    def _action_error(self, exc):
        if isinstance(exc, ToolArgumentsError) and str(exc).startswith('update_plan.'):
            return {'error':str(exc), 'error_code':'tool_arguments',
                    'next':'Pass steps as plain strings, for example {"steps":["Verify candidates","Score matches"]}. Do not use objects with title or description. Or skip the plan and execute the next useful tool.'}
        from eregion.runtime.career.source_health import SourceBlocked
        if isinstance(exc,SourceBlocked):return {'error':str(exc),'error_code':'source_blocked',
            'source':exc.source,'no_new_evidence':True,
            'next':'Use a healthy observed candidate or discover a different public source. This source remains blocked for this user turn.'}
        from eregion.runtime.career.posting_qualification import PostingRejected
        if isinstance(exc,PostingRejected):return exc.outcome()
        from eregion.runtime.model_runtime import model_failure
        failure = model_failure(exc)
        if failure.code != 'model_error':
            return {'error': failure.error_type, 'error_code': failure.code, 'retryable': failure.retryable,
                    'next': 'This is a provider failure, not invalid user data. Preserve inputs; retry at most once, then continue independent work or report the unavailable capability.'}
        safe = isinstance(exc, (ValueError, LookupError)) or type(exc).__module__.startswith('eregion.runtime.')
        return {'error': str(exc)[:1500] if safe else type(exc).__name__,
            'error_code': 'tool_arguments' if isinstance(exc, ToolArgumentsError) else 'tool_error',
            'next': 'Correct the tool arguments and retry.' if isinstance(exc, ToolArgumentsError)
                    else 'Inspect current state and change the approach. Ask only for missing user facts.'}

    def after_action(self, call, outcome, turn):
        from eregion.runtime.tasks.loop import now
        name, args = call['name'], call['args']
        self.loop._timing(self.task_id, 'tool', self.started or time.monotonic(), turn.revision, failed='error' in outcome, tool=name)
        self.started = None
        self.loop.repository.change(self.task_id, tool_activity=None)
        state = self.state()
        progress_updates={};search_subset=False
        previous=state.get('observation_progress_evidence',{})
        seen_observations=previous.get('fingerprints',[]) if previous.get('revision')==turn.revision else []
        if outcome.get('error_code')=='candidate_rejected':
            fingerprint=outcome_fingerprint(['candidate_rejected',outcome.get('candidate_identity'),outcome.get('body_hash')])
        elif name.startswith('browser_'):
            from eregion.runtime.progress import browser_evidence
            fingerprint = outcome_fingerprint(browser_evidence(outcome))
        elif name == 'read_context':
            # Access counters change on every read, but immutable evidence does not.
            fingerprint = outcome_fingerprint([name, args.get('ref'), args.get('path') or [],
                outcome.get('offset', args.get('offset', 0)), outcome.get('content'), outcome.get('error')])
        elif name == 'search_jobs':
            # Query wording is an intention, not new evidence. Distinct actual
            # sources or candidate results still allow discovery to advance.
            from eregion.runtime.progress import search_evidence
            fingerprint = outcome_fingerprint([name,search_evidence(outcome)])
            previous=state.get('search_progress_evidence',{})
            seen_searches=previous.get('fingerprints',[]) if previous.get('revision')==turn.revision else []
            # A reordered, truncated or narrower list of already-observed links is
            # not discovery progress; only a candidate not seen this turn is.
            seen_identities=previous.get('identities',[]) if previous.get('revision')==turn.revision else []
            identities=[row['identity'] for row in search_evidence(outcome)['jobs']]
            search_subset=bool(identities) and set(identities)<=set(seen_identities)
            progress_updates['search_progress_evidence']={'revision':turn.revision,
                'fingerprints':list(dict.fromkeys([*seen_searches,fingerprint]))[-128:],
                'identities':list(dict.fromkeys([*seen_identities,*identities]))[-256:]}
        else:
            fingerprint = outcome_fingerprint([name, args, outcome])
        repeated = sum(h.get('outcome_fingerprint') == fingerprint and h.get('chat_revision') == turn.revision for h in state['history'][-12:])
        if name=='read_context' or name.startswith('browser_'):
            if fingerprint in seen_observations:repeated=max(1,repeated)
            progress_updates['observation_progress_evidence']={'revision':turn.revision,
                'fingerprints':list(dict.fromkeys([*seen_observations,fingerprint]))[-256:]}
        if name=='search_jobs' and (fingerprint in seen_searches or search_subset):repeated=max(1,repeated)
        if outcome.get('no_new_evidence') or outcome.get('error_code') in {'repeated_action','source_blocked'}:
            repeated = max(1, repeated)
        control = name in {'say', 'update_plan', 'ask_user', 'wait_for_user', 'wait_for_result', 'finish_task', 'pause_task'}
        if repeated >= 1 and not control:
            outcome['no_new_evidence']=True
            outcome['progress_feedback'] = ('No new evidence from this action. Change query, route, source or capability. '
                'If alternatives are exhausted, finish with verified results and the limitation.')
            if name=='search_jobs':
                outcome['progress_feedback']=('Public search returned only links already observed in this request. '
                    'Do not repeat it with reworded queries. Read an unread observed posting, or call search_jobs with '
                    'boards (greenhouse/lever/ashby company tokens or a public Workday host/site) for employers that '
                    'hire this role in the requested location. If alternatives are exhausted, finish with verified results and the limitation.')
        stalled = bool(repeated and not control)
        wait_after_read = getattr(self.loop.harness, 'wait_after_repeated_read', None)
        if repeated and not outcome.get('error') and wait_after_read:
            with self.loop.lock:
                if self.active() and self.revision() == turn.revision:
                    wait = wait_after_read(self.state(), name)
                    if wait:
                        stalled = False
                        outcome['background_wait'] = self.loop._control(
                            self.task_id, 'wait_for_result', wait, self.observed_state)
                        outcome['progress_feedback'] = (
                            'This read added no evidence. Waiting for the active capability; '
                            'on wake, use the current request and live outcome. Do not replay old instructions.')
        context_result = self.context_archive.tool_result(outcome) if self.context_archive is not None else trace_value(outcome)
        entry = {'tool': name, 'call_id': call['id'], 'arguments': trace_value(args), 'result': context_result, 'at': now(),
                 'chat_revision': turn.revision, 'outcome_fingerprint': fingerprint,
                 'no_new_evidence':bool(outcome.get('no_new_evidence'))}
        if name.startswith('browser_'):
            browser=state.get('browser') or {}
            entry.update(browser_url=outcome.get('url') or browser.get('url'),
                browser_source_hash=outcome.get('page_evidence_hash') or browser.get('page_evidence_hash'))
        self.loop.repository.change(self.task_id, lambda s: {'history': [*s['history'], entry][-24:],
            **progress_updates,
            'actions': [*s['actions'], {'tool': name, 'label': name, 'outcome': 'failed' if 'error' in outcome else 'completed', 'at': now()}][-100:],
            **({'status': 'completed', 'plan': []} if name == 'finish_task' and outcome.get('completed')
               and s['chat_revision'] == turn.revision and self.loop._active(s) else
               {'status':'waiting'} if (name == 'wait_for_result' and outcome.get('waiting') or outcome.get('background_wait', {}).get('waiting'))
               and s['chat_revision'] == turn.revision and self.loop._active(s) else {})})
        latest = self.state()
        if latest['chat_revision'] != turn.revision:
            return None
        if latest['status'] in {'needs_input', 'waiting', 'completed'} or not self.active():
            return {'reason': latest['status']}
        if self.no_progress(turn, stalled, batched=name in turn.batchable):return {'reason':'no_progress'}
        return None

    def image_message(self, image):
        if not isinstance(image, bytes) or not self.state().get('visual_fallback'):
            return []
        return [HumanMessage(content=[{'type': 'text', 'text': 'Untrusted current browser viewport. Use its screenshot_id and normalized 0..1000 coordinates.'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(image).decode()}}])]
