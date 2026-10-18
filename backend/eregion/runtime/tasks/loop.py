"""A domain-independent task loop. A harness supplies observations and capabilities.

There is no career stage ordering here: the model plans, acts, asks, waits or finishes.
The domain harness owns evidence, external effects and approval boundaries.
"""
import logging
import time
from datetime import UTC, datetime
from threading import Event, RLock, Thread
from uuid import uuid4

from eregion.runtime.core.engine import AgentLoop
from eregion.runtime.tasks.host import TaskHost
from eregion.runtime.core.model import ModelInterrupted
from eregion.runtime.model_runtime import model_timeout
from eregion.runtime.model_runtime import MODEL_PAYMENT_MESSAGE, model_failure
from eregion.runtime.telemetry import timing_update, model_metrics
from eregion.runtime.tasks.control import requests_resume, requests_pause, resume_command_only
from eregion.runtime.provider_usage import track_run, usage_scope, TaskProviderBudgetExceeded


logger = logging.getLogger(__name__)


def task_failure(error, stage):
    """Public diagnostics contain classifications, never provider bodies or prompts."""
    failure = model_failure(error)
    code = failure.code if stage in {'model_setup', 'model_request'} else 'task_error'
    if stage == 'model_setup':
        message = 'The model could not be initialized. Check the model configuration in the backend.'
    elif stage != 'model_request':
        message = 'The agent could not read or update its task context. Your work is saved; try again.'
    elif failure.status in {401, 403}:
        message = 'The model provider rejected authentication or access. Check the configured API key and model permissions.'
    elif failure.code == 'model_payment_required':
        message = MODEL_PAYMENT_MESSAGE
    elif failure.status == 503:
        message = 'The model service is temporarily overloaded (HTTP 503). Automatic retries did not succeed. Please try again shortly.'
    elif failure.code == 'model_timeout':
        message = 'The model did not respond before the request timed out. Automatic retries did not succeed. Please try again.'
    elif failure.code == 'model_rate_limited':
        message = 'The model provider is rate limiting requests (HTTP 429). Please try again later or check the provider quota.'
    elif failure.code == 'model_unavailable':
        message = 'The model service could not be reached or is temporarily unavailable. Automatic retries did not succeed. Please try again shortly.'
    elif failure.status:
        message = f'The model provider rejected the request (HTTP {failure.status}). Check the configured model and request compatibility.'
    else:
        message = 'The model request failed or returned an unusable response. Your work is saved; retry or check the backend model configuration.'
    return {'stage':stage, 'code':code, 'error_type':failure.error_type,
            'status':failure.status, 'retryable':failure.retryable if stage == 'model_request' else False,
            'message':message}


def now():
    return datetime.now(UTC).isoformat()


def function(name, description, properties=None, required=None):
    properties = properties or {}
    return {'type':'function','function':{'name':name,'description':description,
        'parameters':{'type':'object','properties':properties,
            'required':list(properties) if required is None else required,'additionalProperties':False}}}


TEXT = {'type':'string'}
CONTROL_TOOLS = [
    function('update_plan','Save a plan only when useful. steps must be plain strings, e.g. ["Verify candidates", "Score matches"], never title/description objects. Steps are intentions, not evidence of progress.',
        {'steps':{'type':'array','items':{'type':'string','maxLength':300},'maxItems':10}}),
    function('say','Send a concise progress message to the user.',{'text':TEXT}),
    function('ask_user','Ask a focused question in chat. Keep working if independent work remains.',
        {'text':TEXT,'wait_for_reply':{'type':'boolean'}}),
    function('wait_for_user','Wait for the pending question. Do not repeat it.'),
    function('pause_task','Honor a request to pause this task and its running capabilities. Preserve all work.',{'text':TEXT}),
    function('wait_for_result','Sleep until a running capability needs input or completes.',
        {'kind':TEXT,'id':TEXT}),
    function('finish_task','Explicitly finish the latest request with its answer or observed outcome and evidenced limitations. Also use directly for greetings and ordinary questions. Do not conclude with a promise of future work; the conversation can continue.',
             {'text':TEXT,'stop_reason':{'type':'string','maxLength':400,'description':'When finishing a job search: one short '
              'sentence on why searching stops now (what was covered and why a further step is not worth it).'}},['text']),
]


class TaskModel:
    def __init__(self):
        from eregion.domain.model import create_model
        self.model = create_model()
        self.timeout = model_timeout(self.model)

    def invoke(self, messages, tools):
        # Per-reply output bound. A complete 10-15 job final report (URL, score and
        # risk per job) plus reasoning was cut off at 2400 in live runs.
        # Raised from 4000: a reply deciding several jobs with tiered gap lists was cut off
        # twice in a row (tier-check-01 student replay); only generated tokens are billed.
        return self.model.bind_tools(tools).invoke(messages, timeout=self.timeout, max_tokens=8000)


class TaskLoop:
    def __init__(self, repository, harness, *, model_factory=TaskModel, budget=100, retry_delays=(1.0, 3.0),
                 execution=None):
        self.repository, self.harness = repository,harness
        self.model_factory, self.budget = model_factory,budget
        self.retry_delays = tuple(retry_delays)
        self.lock, self.stop = RLock(),Event()
        # The process ExecutionService owns the worker pool and startup recovery;
        # a private one is created only when none is supplied (tests, CLI).
        from eregion.runtime.execution import ExecutionService
        self._owns_execution = execution is None
        self.execution = execution or ExecutionService()
        self.execution.register_kind('career_task', max_workers=2, recover=self._recover)
        self.running = set()
        self.execution.recover('career_task')
        self.monitor = Thread(target=self._monitor,daemon=True,name='task-events')
        self.execution.register_resource('task-events', 'Wakes waiting career tasks; enqueue only.')
        self.monitor.start()

    def _recover(self):
        """Startup recovery: re-queue unfinished tasks once per process."""
        for state in self.repository.list():
            if state['status'] in {'running','queued'} and not state.get('pause_requested'):
                self.repository.change(state['task_id'],status='queued')
                self.schedule(state['task_id'])

    def schedule(self, task_id):
        with self.lock:
            if self.stop.is_set() or task_id in self.running:return
            self.running.add(task_id)
            # TaskLoop coordinates its own re-runs (see _drive's tail), so no key.
            self.execution.submit('career_task',None,self._drive,task_id)

    def start(self, text, key, *, visual_fallback=False, context=None):
        state, created = self.repository.create(text,key,visual_fallback=visual_fallback,context=context)
        if created:
            state = self.repository.change(state['task_id'], model_budget=self._budget(state))
            self.schedule(state['task_id'])
        return state

    def chat(self, task_id, text, message_id, *, visual_fallback=None, context=None):
        with self.lock:
            state = self.repository.get(task_id)
            if any(m['id']==message_id for m in state['messages']):return state
            control = text.strip().casefold()
            pause = requests_pause(text)
            resume = requests_resume(text)
            hold = pause or (state.get('pause_requested') and not resume)
            reply = (state.get('input_request') or {}).get('id')
            message = {'id':message_id,'role':'user','text':text,'at':now(),'in_reply_to':reply}
            if context:message['context']=context
            state = self.repository.change(task_id,lambda s:{'messages':[*s['messages'],message][-150:],
                'chat_revision':s['chat_revision']+1,'budget_base':s['model_calls']},
                status='needs_input' if hold else 'queued',pause_requested=hold,
                input_request=state.get('input_request') if pause or (resume and resume_command_only(text)) else None,wait=None,
                error=None,model_activity=None,
                visual_fallback=state['visual_fallback'] if visual_fallback is None else visual_fallback)
            self.harness.control(state,'pause' if pause else 'resume' if resume else 'feedback')
            if hold and not pause:
                # A message during a pause (e.g. while the user controls the browser) is kept
                # for the resumed agent; say so instead of answering with silence.
                self.repository.append(task_id,role='assistant',text='Paused, so I have not acted on this yet. Your message '
                    'is saved and I will read it when you resume: send "continue" or use Resume when you are done.')
            route=getattr(self.harness,'route_user_control',None)
            if not hold and route:
                try:routed=route(state,message)
                except Exception as exc:
                    failure=task_failure(exc,'decision')
                    logger.warning('Task control failure: task=%s type=%s',task_id,type(exc).__name__)
                    self.repository.append(task_id,role='assistant',text=failure['message'])
                    return self.repository.change(task_id,status='needs_input',error=failure,model_activity=None)
                if routed and routed.get('wait'):
                    # The reply already reached the capability; wait for it without a model turn.
                    wait=routed['wait']
                    return self.repository.change(task_id,lambda s:{'history':[*s['history'],
                        {'tool':routed['tool'],'arguments':{'message_id':message_id},'result':routed['result'],
                         'chat_revision':s['chat_revision'],'at':now()}][-24:]},status='waiting',
                        wait={**wait,'token':self.harness.wait_token(wait)})
                if routed:
                    self.repository.append(task_id,role='assistant',text=routed['text'])
                    result=self.repository.change(task_id,lambda s:{'history':[*s['history'],
                        {'tool':routed['tool'],'arguments':{'message_id':message_id},'result':routed['result'],
                         'chat_revision':s['chat_revision'],'at':now()}][-24:]},status='needs_input' if routed.get('deferred') else 'completed',plan=[])
                    if routed.get('start'):routed['start']()
                    return result
            if not hold:self.schedule(task_id)
            return state

    def _monitor(self):
        while not self.stop.wait(.5):
            try:
                for state in self.repository.list():
                    if state['status']=='waiting' and not state.get('pause_requested') and self.harness.wake(state):
                        with self.lock:
                            latest = self.repository.get(state['task_id'])
                            if latest['status']!='waiting':continue
                            hold = getattr(self.harness,'hold_wait',None)
                            if hold and hold(latest):
                                # The capability asks the user directly; keep waiting without a model turn.
                                self.repository.change(state['task_id'],wait={**latest['wait'],'token':self.harness.wait_token(latest['wait'])})
                                continue
                            self.repository.change(state['task_id'],status='queued',wait=None)
                            self.schedule(state['task_id'])
            except Exception:
                # A temporary database/browser outage is retried; no model spam.
                continue

    def _active(self,state):
        return not self.stop.is_set() and not state.get('pause_requested') and state['status'] not in {'cancelled','completed'}

    def _budget(self,state):return min(self.budget,state.get('model_budget',self.budget))

    def _invoke_model(self, task_id, model, messages, tools, revision, requests):
        # Retry only an unsuccessful model request. Never replay a completed tool.
        def active_revision():
            state = self.repository.get(task_id)
            return self._active(state) and state['chat_revision'] == revision
        for attempt in range(1, len(self.retry_delays)+2):
            with self.lock:
                state = self.repository.get(task_id)
                if not self._active(state) or state['chat_revision'] != revision:return None
                if state['model_calls']-state['budget_base'] >= self._budget(state):
                    self.repository.append(task_id,role='assistant',text='I have saved the work so far. Send continue to allow more steps.')
                    self.repository.change(task_id,status='needs_input',model_activity=None)
                    return None
                self.repository.change(task_id,status='running',model_calls=state['model_calls']+1,
                    model_activity={'phase':'requesting' if attempt == 1 else 'retrying',
                                    'attempt':attempt,'max_attempts':len(self.retry_delays)+1,
                                    'message':'Waiting for the model to respond.' if attempt == 1 else
                                        f'Retrying the model request automatically ({attempt}/{len(self.retry_delays)+1}).'},error=None)
            started = time.monotonic()
            try:
                with usage_scope(attempt=attempt):
                    response = requests.call(lambda: model.invoke(messages, tools),
                        active=active_revision,
                        timeout=getattr(model, 'timeout', 45) + 2)
            except ModelInterrupted:
                return None
            except Exception as exc:
                self._timing(task_id, 'model', started, revision, failed=True, **model_metrics(messages, tools))
                failure = model_failure(exc)
                if not failure.retryable or attempt > len(self.retry_delays):raise
                with self.lock:
                    current = self.repository.get(task_id)
                    if not self._active(current) or current['chat_revision'] != revision:return None
                    reason = f'HTTP {failure.status}' if failure.status else failure.code.replace('_', ' ')
                    self.repository.change(task_id,model_activity={'phase':'retrying','attempt':attempt+1,
                        'max_attempts':len(self.retry_delays)+1,'status':failure.status,
                        'message':f'The model is temporarily unavailable ({reason}). Retrying automatically ({attempt+1}/{len(self.retry_delays)+1}).'})
                deadline = time.monotonic()+self.retry_delays[attempt-1]
                while time.monotonic() < deadline:
                    if self.stop.wait(min(.1, max(0, deadline-time.monotonic()))):return None
                    current = self.repository.get(task_id)
                    if not self._active(current) or current['chat_revision'] != revision:return None
                continue
            self._timing(task_id, 'model', started, revision, **model_metrics(messages, tools, response))
            with self.lock:
                current = self.repository.get(task_id)
                if not self._active(current) or current['chat_revision'] != revision:return None
                self.repository.change(task_id,model_activity=None)
            return response

    def _timing(self, task_id, stage, started, revision, *, failed=False, tool=None, **metadata):
        seconds = time.monotonic()-started
        self.repository.change(task_id,lambda s:timing_update(s,stage,seconds,
            failed=failed,tool=tool,chat_revision=revision,**metadata))

    def _progress(self, task_id, text):
        state = self.repository.get(task_id)
        if hasattr(self.harness,'validate_reply'):self.harness.validate_reply(state,text)
        for message in reversed(state['messages']):
            if message.get('role') == 'user':break
            if message.get('role') == 'assistant' and message.get('text','').strip() == text.strip():return False
        self.repository.append(task_id,role='assistant',text=text)
        return True

    @track_run('task_id', 'agent')
    def _drive(self, task_id):
        host = None
        revision = self.repository.get(task_id)['chat_revision']
        try:
            host = TaskHost(self, task_id, self.model_factory())
            result = AgentLoop(host, self.repository.get(task_id).get('loop_runtime'),
                request_chars=getattr(self.harness,'request_chars',64000)).run()
            if result['reason'] == 'invalid_model_response':
                raise ValueError('The model returned an unusable response.')
            with self.lock:
                state = self.repository.get(task_id)
                if result['reason'] in {'yielded', 'steered'} and self._active(state):
                    self.repository.change(task_id, status='queued')
        except TaskProviderBudgetExceeded as exc:
            with self.lock:
                latest = self.repository.get(task_id)
                if self._active(latest):
                    message = 'I stopped at the provider budget limit. Saved work is preserved; the requested work is not complete.'
                    delivery = getattr(self.harness, 'terminal_delivery', None)
                    lines = delivery(latest) if delivery else []
                    text = message + ('\n\nSaved so far:\n' + '\n'.join(lines) if lines else '')
                    self.repository.append(task_id, role='assistant', text=text, kind='error')
                    self.repository.change(task_id, status='needs_input', model_activity=None, tool_activity=None,
                        error={'code': 'provider_budget', 'reason': exc.reason, 'message': message, 'retryable': False,
                               **getattr(exc, 'workflow_detail', {})})
        except Exception as exc:
            with self.lock:
                latest = self.repository.get(task_id)
                revision = host.last_revision if host else revision
                if self._active(latest) and latest['chat_revision'] == revision:
                    stage = host.stage if host else 'model_setup'
                    failure = task_failure(exc, stage)
                    logger.warning('Task failure: task=%s stage=%s type=%s status=%s',
                        task_id, stage, failure['error_type'], failure['status'])
                    self.repository.append(task_id, role='assistant', text=failure['message'], kind='error')
                    self.repository.change(task_id, status='needs_input', error=failure, model_activity=None, tool_activity=None)
        finally:
            if host:
                host.requests.close()
            with self.lock:
                self.running.discard(task_id)
                if not self.stop.is_set() and self.repository.get(task_id)['status'] == 'queued':
                    self.schedule(task_id)

    def _question(self, task_id, text):
        if not isinstance(text,str) or not 0<len(text)<=4000:raise ValueError('Ask a concise question.')
        state = self.repository.get(task_id)
        if state.get('input_request'):return state['input_request']
        question = {'id':str(uuid4()),'text':text,'field_keys':[],'fields':[]}
        if state.get('active_application_run') and hasattr(self.harness,'wait_token'):
            question.update(application_run_id=state['active_application_run'],
                application_wait_token=self.harness.wait_token({'id':state['active_application_run']}))
        self.repository.append(task_id,id=question['id'],role='assistant',text=text,kind='question')
        self.repository.change(task_id,input_request=question)
        return question

    def _control(self, task_id, name, args, state):
        if name=='update_plan':
            steps = args['steps']
            if not isinstance(steps,list) or len(steps)>10 or not all(isinstance(s,str) and len(s)<=300 for s in steps):raise ValueError('Use up to ten short steps.')
            self.repository.change(task_id,plan=steps)
            return {'saved':True}
        if name=='say':
            if not isinstance(args['text'],str) or not 0<len(args['text'])<=4000:raise ValueError('Use a concise message.')
            sent = self._progress(task_id,args['text'])
            return {'sent':sent, **({} if sent else {'next':'This update repeats prior progress. Continue useful work and report a new result or blocker.'})}
        if name=='ask_user':
            if type(args['wait_for_reply']) is not bool:raise ValueError('wait_for_reply must be a boolean.')
            question = self._question(task_id,args['text'])
            if args['wait_for_reply']:self.repository.change(task_id,status='needs_input')
            return {'question':question}
        if name=='wait_for_user':
            if not self.repository.get(task_id).get('input_request'):raise ValueError('Read the latest reply or ask a new question first.')
            self.repository.change(task_id,status='needs_input')
            return {'waiting':True}
        if name=='pause_task':
            if not isinstance(args['text'],str) or not 0<len(args['text'])<=4000:raise ValueError('Explain the pause briefly.')
            current = self.repository.change(task_id,status='needs_input',pause_requested=True,wait=None)
            self.harness.control(current,'pause')
            self.repository.append(task_id,role='assistant',text=args['text'])
            return {'paused':True}
        if name=='wait_for_result':
            self.harness.validate_wait(state,args)
            # A capability that already reached a final state never changes its
            # token again, so waiting on it would never wake (lost wake-up).
            finished = getattr(self.harness,'capability_finished',None)
            if finished and finished(args):return {'changed':True,'next':'This capability already finished. Read its result.'}
            stopped = getattr(self.harness,'capability_stopped',None)
            if stopped and (result := stopped(args)):return result
            token = self.harness.wait_token(args)
            observed = state.get('capability_versions',{}).get(args['kind'],{}).get(args['id'])
            if observed is not None and observed!=token:return {'changed':True,'next':'Read the newly available result.'}
            self.repository.change(task_id,wait={**args,'token':token})
            return {'waiting':True}
        if name=='finish_task':
            self.harness.validate_finish(self.repository.get(task_id))
            if self.repository.get(task_id).get('input_request'):raise ValueError('A question is still pending.')
            if not isinstance(args['text'],str) or not args['text'].strip():raise ValueError('Summarize the observed outcome.')
            # A tiered search reply with gaps per job needs more room than 4000 characters
            # (acceptance tiered-v3-04 was rejected once for length without being told why).
            if len(args['text'])>8000:raise ValueError(f"The reply has {len(args['text'])} characters; keep it under 8000 "
                                                       "by shortening each job's text, not by dropping jobs, gaps or failures.")
            if hasattr(self.harness,'validate_reply'):self.harness.validate_reply(self.repository.get(task_id),args['text'])
            if hasattr(self.harness,'validate_completion'):self.harness.validate_completion(self.repository.get(task_id),args['text'])
            if hasattr(self.harness,'validate_stop'):self.harness.validate_stop(self.repository.get(task_id),args.get('stop_reason'))
            if args.get('stop_reason'):self.repository.change(task_id,stop_reason=args['stop_reason'])
            self.repository.append(task_id,role='assistant',text=args['text'])
            # Commit completion together with the action history in _drive.
            return {'completed':True}
        return None

    def close(self):
        self.stop.set()
        self.monitor.join(timeout=2)
        # Stop and drain this loop's own work before any caller closes resources
        # the drives use; a shared pool's other kinds keep running.
        self.execution.stop_kind('career_task',timeout=None)
        if self._owns_execution:
            self.execution.close(wait=True)
