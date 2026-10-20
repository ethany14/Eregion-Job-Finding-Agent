"""Bounded isolated-state batching. Paid usage is recorded once per HTTP request."""
from concurrent.futures import Future
from contextvars import copy_context
from dataclasses import dataclass
import copy
import json
import queue
import re
import threading
import time
import uuid

import httpx
from eregion.runtime.coordination.model_usage import invoke_model, current_model_call
from eregion.runtime.provider_usage import provider_call, usage_scope, token_counts, input_reservation
from .core import MAX_PAYLOAD_BYTES, MAX_QUESTIONS, boolean, validate_answers
from .settings import canonical, digest


def semantic_value(value):
    """Presentation whitespace is irrelevant to yes/no decisions, not offsets.

    Only provider inputs are normalized. Original documents and source spans
    remain untouched in the analysis and citations.
    """
    if isinstance(value,str):return re.sub(r'\s+',' ',value).strip()
    if isinstance(value,dict):return {k:semantic_value(v) for k,v in value.items()}
    if isinstance(value,list):return [semantic_value(v) for v in value]
    return value


_WAKE = object()

# Stages whose whole input is the posting (requirement extraction, its refinement, and whether a
# requirement is a personal condition). Matching stages read the resume and stay bound to it.
POSTING_ONLY = frozenset({'extract_requirements', 'refine_extraction_yes_no', 'condition_category_yes_no'})


@dataclass
class Pending:
    state: dict
    questions: dict
    purpose: str
    scope: dict
    task_id: str
    context: object
    future: Future
    cacheable: bool = True


class Broker:
    def __init__(self, config, cache, *, transport=None, max_requests=160, batch=True):
        if not config.key:
            raise ValueError('TYPESAFE_ENABLED is true but TYPESAFE_API_KEY is missing.')
        self.config, self.cache, self.batch = config, cache, batch
        self.signature, self.max_requests = config.signature(), max_requests
        self.run_id = str(uuid.uuid4())
        self.records, self.events = [], []
        # A warm cache needs no HTTP client or TLS initialization.
        self.http, self.transport = None, transport
        self.queue = queue.Queue()
        # Workers expected to submit concurrently. A batch waits (bounded) until every
        # live worker has submitted, so one stage of several jobs is one paid request.
        self.active, self.align_seconds = 0, 2.0
        self._active_lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name='jev-fit-broker', daemon=True)
        self.thread.start()

    def close(self):
        self.queue.put(None)
        self.thread.join()
        if self.http is not None:
            self.http.close()

    def expect(self, workers):
        with self._active_lock:
            self.active += workers

    def leave(self):
        with self._active_lock:
            self.active -= 1
        self.queue.put(_WAKE)

    def submit(self, *, state, questions, purpose, scope, task_id):
        future = Future()
        pending = Pending(semantic_value(state), semantic_value(questions), purpose,
                          copy.deepcopy(scope), task_id, copy_context(), future)
        self.queue.put(pending)
        result = future.result()
        if not pending.cacheable:
            from eregion.domain.reasoning import mark_uncacheable
            mark_uncacheable()
        return result

    def _run(self):
        stop = False
        while not stop:
            item = self.queue.get()
            if item is None:
                return
            if item is _WAKE:
                continue
            pending = [item]
            if self.batch:
                deadline = time.monotonic() + .025
                aligned = time.monotonic() + self.align_seconds
                while True:
                    # Local work between stages is CPU-only; the bound keeps a slow
                    # worker from delaying the others indefinitely.
                    limit = aligned if len(pending) < self.active else deadline
                    try:
                        item = self.queue.get(timeout=max(0, limit-time.monotonic()))
                    except queue.Empty:
                        break
                    if item is None:
                        stop = True
                        break
                    if item is _WAKE:
                        continue
                    pending.append(item)
            groups = {}
            for p in pending:
                key = digest([p.purpose, p.scope['owner'], p.scope['resume'], p.scope['facts']])
                if not self.batch:
                    key += p.task_id
                groups.setdefault(key, []).append(p)
            bounded=[]
            for group in groups.values():
                part=[];states={}
                for p in group:
                    trial={**states,digest(p.state):p.state}
                    if part and len(canonical(trial).encode())>MAX_PAYLOAD_BYTES//2:
                        bounded.append(part);part=[];states={}
                    part.append(p);states[digest(p.state)]=p.state
                if part:bounded.append(part)
            for group in bounded:
                try:
                    self._dispatch(group)
                except BaseException as exc:
                    for p in group:
                        if not p.future.done():
                            p.future.set_exception(exc)

    def _key(self, p, q):
        from eregion.domain.reasoning import resolve, escalation_enabled
        # State + question contain the full semantic input. The consumer's job
        # ID/hash must not prevent reuse of the same resume/requirement decision.
        # Posting-only stages read the JD and requirement text, never the resume or the user's facts:
        # their decisions stay valid when only those change, within the same owner. A state that ever
        # carries evidence keeps the resume and facts in its key.
        posting_only = p.purpose in POSTING_ONLY and 'evidence' not in p.state
        scope={k:v for k,v in p.scope.items() if k!='job' and not (posting_only and k in ('resume','facts'))}
        return digest([self.signature, scope, p.purpose, p.state, q,
                       resolve(self.config.reasoning, p.purpose), escalation_enabled()])

    def _request(self, state, questions, purpose, context, *, retry=False, plain=False):
        if len(self.records) >= self.max_requests:
            return {}, 'request_budget', None
        payload = {'model': self.config.model, 'state': state, 'questions': questions}
        from eregion.domain.reasoning import resolve, escalated, escalation_enabled
        level = resolve(self.config.reasoning, purpose)
        original_level = level
        if retry and escalation_enabled():
            level = escalated(level)
        if plain:
            level = None
        if level is not None:
            payload['reasoning_effort'] = level
        if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_PAYLOAD_BYTES:
            return {}, 'payload_limit', None
        rec = {'call_id': str(uuid.uuid4()), 'model': self.config.model, 'purpose': purpose,
               'reasoning_escalation': level != original_level and not plain, 'reasoning_fallback': plain,
               'status': 'started', 'input_tokens': None, 'output_tokens': None,
               'question_count': len(questions)}
        self.records.append(rec)
        started = time.monotonic()

        def call():
            with usage_scope(purpose='jev:fit:' + purpose, attempt=2 if retry or plain else 1,
                             effective_reasoning=level, reasoning_escalation=level != original_level and not plain,
                             reasoning_fallback=plain):
                def paid_request(request_payload):
                    with provider_call(self.config.model, context_chars=len(canonical(request_payload)),
                                       input_token_reservation=input_reservation(request_payload)) as usage:
                        if self.http is None:
                            self.http = httpx.Client(timeout=self.config.timeout, transport=self.transport,
                                follow_redirects=False, headers={'Authorization': 'Bearer ' + self.config.key})
                        response = self.http.post(self.config.base_url + '/v1/systemone', json=request_payload)
                        if response.status_code != 200:
                            usage.update(status='failed', http_status=response.status_code)
                            rec['status'] = 'http_' + str(response.status_code)
                            rec['reasoning_rejected'] = bool(level and response.status_code in {400,422}
                                and 'reasoning' in response.text.lower())
                            return {}
                        data = response.json()
                        counts = token_counts(data.get('usage'))
                        usage.update(counts)
                        rec.update(counts)
                        reservation = current_model_call()
                        if reservation is not None and all(k in counts for k in ('input_tokens','output_tokens')):
                            reservation['usage'] = {k: counts[k] for k in ('input_tokens','output_tokens')}
                        if data.get('model') != self.config.model:
                            raise ValueError('Unexpected Jev release')
                        result = validate_answers(data, questions)
                        rec['status'] = 'ok'
                        return result
                return invoke_model(paid_request, payload)
        try:
            answers = context.run(call)
        except httpx.TimeoutException:
            answers, rec['status'] = {}, 'timeout'
        except httpx.HTTPError:
            answers, rec['status'] = {}, 'connection_error'
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            answers, rec['status'] = {}, 'invalid_response'
        finally:
            rec['seconds'] = round(time.monotonic() - started, 4)
        if not retry and not plain:
            from eregion.domain.reasoning import rejection_retry_enabled
            if rejection_retry_enabled() and rec.get('reasoning_rejected'):
                return self._request(state, questions, purpose, context, plain=True)
            if rec['status'] == 'invalid_response' and escalation_enabled():
                return self._request(state, questions, purpose, context, retry=True)
        return answers, rec['status'], rec

    def _dispatch(self, pending):
        answers = {id(p): {} for p in pending}
        errors = {id(p): [] for p in pending}
        keys = {(id(p), k): self._key(p, q) for p in pending for k,q in p.questions.items()}
        cached = self.cache.get_many(keys.values(), self.config.model) if self.cache else {}
        unique, targets, identifiers = {}, {}, {}
        states={digest(p.state):p.state for p in pending if p.questions}
        cases={key:'case'+str(i) for i,key in enumerate(states)}
        def state_for(needed):
            # A request carries only the cases its questions reference.
            return next(iter(states.values())) if len(states)==1 else {
                'policy':'Each question identifies one independent context_case. Use only that case as its complete state. '
                         'Never borrow facts, job requirements or document instructions from another case.',
                'cases':{cases[k]:states[k] for k in states if k in needed}}
        case_of={}
        for p in pending:
            for k,q in p.questions.items():
                if q.get('type') != 'noul':
                    raise ValueError('Only yes/no fit decisions are supported')
                key = keys[(id(p),k)]
                hit = cached.get(key)
                event = {'task_id':p.task_id, 'purpose':p.purpose, 'question':k,
                         'cache_key':key, 'hit':bool(hit)}
                self.events.append(event)
                if hit:
                    answers[id(p)][k] = hit['answer']
                    event.update(expires_at=hit['expires_at'], provenance=hit['provenance'])
                    continue
                wire_question=q
                if len(states)>1:
                    wire_question={**q,'instructions':{**q['instructions'],
                        'context_case':cases[digest(p.state)],
                        'context_rule':'Evaluate using only state.cases[context_case]; other cases are unrelated.'}}
                qhash = digest([p.state,q])
                wire = identifiers.setdefault(qhash, 'q'+str(len(identifiers)))
                if wire in unique and unique[wire] != wire_question:
                    raise ValueError('Question hash collision')
                unique[wire] = wire_question
                case_of[wire] = digest(p.state)
                targets.setdefault(wire, []).append((p,k,key,event))
        batches, current = [], {}
        purpose = pending[0].purpose
        for k,q in unique.items():
            trial = {**current, k:q}
            payload = {'model':self.config.model,'state':state_for({case_of[w] for w in trial}),'questions':trial}
            if current and (len(trial)>MAX_QUESTIONS or len(json.dumps(payload,ensure_ascii=False).encode())>MAX_PAYLOAD_BYTES):
                batches.append(current)
                current={}
            current[k]=q
        if current:
            batches.append(current)
        for batch in batches:
            state=state_for({case_of[w] for w in batch})
            result,status,record = self._request(state,batch,purpose,pending[0].context)
            from eregion.domain.reasoning import cacheable_attempts
            eligible = cacheable_attempts([record]) if record else True
            writes=[]
            for wire in batch:
                for p,k,key,event in targets[wire]:
                    p.cacheable = p.cacheable and eligible
                    event['status']=status
                    if status=='ok':
                        answer=result[wire]
                        expires=time.time()+(self.config.unknown_ttl if boolean(answer)is None else self.config.ttl)
                        provenance={'call_id':record['call_id'],'run_id':self.run_id,'model':self.config.model}
                        event.update(expires_at=expires,provenance=provenance)
                        answers[id(p)][k]=answer
                        writes.append((key,answer,provenance))
            if self.cache and writes and eligible:
                self.cache.put_many(writes,self.config.model)
            if status!='ok':
                for p in pending:
                    if set(answers[id(p)]) != set(p.questions):
                        errors[id(p)].append(status)
                break
        for p in pending:
            p.future.set_result((answers[id(p)],errors[id(p)]))


class Client:
    def __init__(self, broker, scope):
        self.broker,self.scope=broker,scope
        self.task_id=str(uuid.uuid4())
        self.categories={}
        self.requests=0

    def checkpoint(self,purpose):
        """Join a concurrent stage without questions; never a paid request by itself."""
        if self.broker.active>1:
            self.broker.submit(state={},questions={},purpose=purpose,scope=self.scope,task_id=self.task_id)

    def evaluate(self,state,questions,purpose):
        if not questions:
            return {},[]
        self.requests+=1
        if self.requests>24:
            return {},['logical_request_budget']
        if purpose=='support_yes_no':
            kept={}
            for k,q in questions.items():
                category=boolean(self.categories.get('c'+k[1:]))
                if (k.startswith('k')and category is not True)or(k.startswith('r')and category is True):
                    continue
                kept[k]=q
            questions=kept
        answers,errors=self.broker.submit(state=state,questions=questions,purpose=purpose,
                                          scope=self.scope,task_id=self.task_id)
        if purpose=='condition_category_yes_no':
            self.categories.update(answers)
        return answers,errors
