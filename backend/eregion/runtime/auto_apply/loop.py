"""A bounded observe/act/verify loop with durable pauses and submission fencing."""
from threading import RLock
import logging
import os
import random
import re
from urllib.parse import urlsplit
import hashlib
import json
from pathlib import Path
from eregion.paths import PROJECT_ROOT
import time
from uuid import uuid4

from eregion.runtime.model_runtime import MODEL_PAYMENT_MESSAGE
from eregion.runtime.telemetry import safe_traceback
from eregion.runtime.provider_usage import track_run

from .agent import HybridApplicationAgent
from .autofill import autofill, desired_answer, matches, pending_documents
from .browser import IDENTITY_PROVIDERS, ApplicationBrowser, BrowserBlocked, form_digest
from .contracts import PAUSED, TERMINAL
from .repository import ApplyConflict, now
from .worker import WorkerPool
from .matching import boolean_answer, is_consent
from .memory import pending_approvals, review_fields, review_required
from .conversation import ordered_answers, input_prompt
from .browser_sessions import BrowserSessionStore
from . import authentication
from .decisions import unresolved_paths
from .drafting import visible_drafts


logger = logging.getLogger(__name__)
# Handoffs that finish without a chat reply: the page itself shows when they are done.
AUTO_RESUME = {"verification", "identity"}
VOLATILE_TEXT = re.compile(r"\b\d+\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?)\s+ago\b|"
    r"\b(?:just now|a (?:few seconds|minute) ago)\b|\b\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]\.?m\.?)?", re.I)
# Automatic retries per failure. Rate limits back off longest and honor Retry-After.
RETRY_POLICY = {"model_rate_limited": (5, 15, 45, 120), "model_timeout": (2, 10, 30),
    "model_unavailable": (2, 10, 30), "default": (1, 3)}
MAX_RETRY_DELAY = 300
# internal_error is a defect in this program; retrying the same code cannot fix it.
DEFAULT_WORKERS = 3
# W4: the per-step budget resets on every continue/answer, so without a whole-application
# allowance a run could spend without bound (Adobe Workday: 589 reserved calls). Extensions
# are explicit user grants (ContinueApplication.extend_budget), recorded in budget_extensions.
APPLICATION_MODEL_CALLS = int(os.getenv('APPLY_MAX_MODEL_CALLS', '300'))
BUDGET_EXTENSION = 100


def configured_workers():
    try:
        return max(1, min(8, int(os.getenv("JOB_AGENT_APPLY_WORKERS", DEFAULT_WORKERS))))
    except ValueError:
        return DEFAULT_WORKERS


def site_key(url):
    """One employer site. Shared ATS hosts serve many employers, keyed by their board."""
    parts = urlsplit(url or "")
    host = parts.hostname or ""
    if re.search(r"greenhouse\.io$|lever\.co$|ashbyhq\.com$", host):
        return host + "/" + (parts.path.split("/")[1:2] or [""])[0]
    return host


RETRYABLE = {"validation_error", "model_error", "model_timeout", "model_rate_limited", "model_unavailable",
    "invalid_model_response", "browser_error", "no_progress"}


class ApplicationLoop:
    def __init__(self, repository, *, browser_factory=ApplicationBrowser, agent_factory=HybridApplicationAgent, retry_delays=None, max_model_calls=120, max_tool_calls=360, workers=None, execution=None):
        self.repository = repository
        self.browser_factory = browser_factory
        self.agent_factory = agent_factory
        self.lock = RLock()
        self.controls, self.revisions, self.buffered = {}, {}, {}
        if hasattr(repository, 'state_listeners'):
            repository.state_listeners.append(self._track_control)
        self.browsers = {}
        self.previews = {}
        self.preview_times = {}
        self.handoff_checks = {}
        self.closed = False
        self.retry_delays = retry_delays
        self.max_model_calls,self.max_tool_calls=max_model_calls,max_tool_calls
        self.scheduled = {}
        self.session_store = BrowserSessionStore(PROJECT_ROOT / ".browser-sessions")
        # Applications run in parallel on separate browser threads. Each run stays
        # on one worker (Playwright objects are thread-bound), and runs on the
        # same employer site share a worker so one site never sees parallel bursts.
        self.assigned, self.sites = {}, {}
        self.executor = WorkerPool(workers or configured_workers(), self._pump_browsers)
        # Runs stay on their browser thread; the process ExecutionService owns the
        # once-only startup recovery and records these threads as their owner.
        from eregion.runtime.execution import ExecutionService
        self.execution = execution or ExecutionService()
        self.execution.register_owner('application_run', recover=self._recover_runs,
            role='Browser-affine application workers; each run stays on its thread.')
        self.execution.recover('application_run')

    def _recover_runs(self):
        for run_id in self.repository.recover() or []:
            self._submit(run_id, self._drive, run_id, None)

    def _worker(self, run_id):
        with self.lock:
            if run_id not in self.assigned:
                try:
                    site = site_key(self.repository.get(run_id).get("url"))
                except Exception:
                    site = None
                same = [index for other, index in self.assigned.items() if site and self.sites.get(other) == site]
                load = lambda index: sum(1 for value in self.assigned.values() if value == index)
                self.assigned[run_id] = same[0] if same else min(range(len(self.executor)), key=load)
                self.sites[run_id] = site
            return self.executor.workers[self.assigned[run_id]]

    def _submit(self, run_id, function, *args):
        return self._worker(run_id).submit(self._metered, run_id, function, *args)

    def _metered(self, run_id, function, *args):
        """W6: application work started by a career task counts against that task's provider
        budget. Without this scope only calls made inside the starting tool call were in the
        ledger, so a 60-attempt task cap missed the worker's own calls (40 vs 17 recorded)."""
        from eregion.runtime.provider_usage import TaskProviderBudgetExceeded, UsageLedger, usage_scope
        try:
            state = self.repository.get(run_id)
        except Exception:
            return function(*args)
        sessions = getattr(self.repository, 'sessions', None)
        try:
            with usage_scope(ledger=UsageLedger(sessions) if sessions else None,
                             task_id=state.get('origin_task_id'), run_id=run_id):
                return function(*args)
        except TaskProviderBudgetExceeded as exc:
            with self.lock:
                current = self._active(run_id)
                if current is not None:
                    self.repository.save(current, status='needs_input', agent_reason='application_budget', retry_at=None,
                        message='This application reached its provider budget (' + str(exc.reason) + '). Completed fields '
                                'are saved; allow more to continue, or finish it in the browser.')

    def _owned(self, index):
        with self.lock:
            return [run_id for run_id in list(self.browsers) if self.assigned.get(run_id) == index]

    def _pump_browsers(self, index=0):
        with self.lock:
            for run_id, (deadline, revision) in list(self.scheduled.items()):
                if time.monotonic() >= deadline:
                    del self.scheduled[run_id]
                    state = self._active(run_id)
                    if state and state["status"] == "queued" and not state.get("pause_requested") and state.get("chat_revision", 0) == revision:
                        self._submit(run_id, self._drive, run_id, None)
        for run_id in self._owned(index):
            browser = self.browsers.get(run_id)
            if browser is None:
                continue
            pump = getattr(browser, "pump", None)
            if callable(pump):
                try:
                    pump()
                except Exception:
                    pass
            self._capture_preview(run_id, browser)
            self._recheck_shared_login(run_id,browser)
            if hasattr(browser, "save_login_state"):
                browser.save_login_state()
            self._resume_after_handoff(run_id, browser)

    def _recheck_shared_login(self,run_id,browser):
        with self.lock:
            state=self._active(run_id)
            if not state or state['status']!='needs_input' or not state.get('account_request') or state.get('pause_requested') or state.get('paused_for_control'):return
            revision=state.get('chat_revision',0)
        reload=getattr(browser,'reload_shared_login',None)
        if not reload:return
        try:
            if not reload() or browser.barrier():return
        except Exception:return
        with self.lock:
            current=self._active(run_id)
            if not current or current.get('chat_revision',0)!=revision or current.get('pause_requested') or current.get('paused_for_control'):return
            self.repository.save(current,status='queued',account_request=None,input_request=None,review_digest=None,
                message='Your existing sign-in is available. Rechecking this application.')
            self._submit(run_id,self._drive,run_id,None)

    def _resume_after_handoff(self, run_id, browser):
        """Continue on its own once the user clears a verification or sign-in page."""
        if time.monotonic() - self.handoff_checks.get(run_id, 0) < 3 or not hasattr(browser, "barrier"):
            return
        self.handoff_checks[run_id] = time.monotonic()
        waiting = lambda state: (state is not None and state["status"] == "needs_input"
            and state.get("handoff_kind") in AUTO_RESUME and not state.get("account_request")
            and not state.get("pause_requested") and not state.get("paused_for_control"))
        with self.lock:
            if not waiting(self._active(run_id)): return
        try:
            if browser.barrier(): return
        except Exception:
            return
        with self.lock:
            state = self._active(run_id)
            if not waiting(state): return
            self.repository.save(state, status="queued", pause_requested=False, paused_for_control=False,
                handoff_kind=None, chat_revision=state.get("chat_revision", 0) + 1, review_digest=None,
                message="The verification is complete. Continuing the application.")
        self._submit(run_id, self._drive, run_id, None)

    def _capture_preview(self, run_id, browser):
        if not hasattr(browser, "preview") or time.monotonic() - self.preview_times.get(run_id, 0) < 2:
            return
        self.preview_times[run_id] = time.monotonic()
        try:
            content = browser.preview()
            with self.lock:
                self.previews[run_id] = content
        except Exception:
            pass

    def preview(self, run_id):
        self.repository.get(run_id)
        with self.lock:
            return self.previews.get(run_id)

    def set_browser_visible(self,run_id,visible,version):
        """Only the user's window button invokes this; model tools cannot show Chrome."""
        with self.lock:
            state=self.repository.get(run_id)
            if self.closed or state['version']!=version or state['status'] in TERMINAL|{'submitting'}:
                raise ApplyConflict('The application changed or is submitting. Refresh before taking control.')
            if run_id not in self.browsers:raise ApplyConflict('No application browser is open. Resume the task first.')
            state=self.repository.save(state,status='needs_input',pause_requested=True,
                chat_revision=state.get('chat_revision',0)+1,review_digest=None,retry_at=None,
                paused_for_control=True,
                message='Paused for browser control. Saved answers remain. Resume when ready.')
            self.scheduled.pop(run_id,None)
        def change():
            latest=self.repository.get(run_id)
            if not latest.get('pause_requested') or latest.get('chat_revision')!=state['chat_revision']:
                raise ApplyConflict('The task changed. Pause before switching browser mode.')
            browser=self.browsers.get(run_id)
            if not browser or not hasattr(browser,'set_visible'):raise ApplyConflict('This browser does not support manual window control.')
            browser.set_visible(visible)
            self._capture_preview(run_id,browser)
            return self.repository.get(run_id)
        try:return self._submit(run_id, change).result()
        except BrowserBlocked as exc:raise ApplyConflict(str(exc)) from None

    def browser_control(self, run_id, request, *, parent_scope=''):
        from eregion.runtime.browser.handoff import control_browser
        with self.lock:
            state = self.repository.get(run_id)
            if self.closed or state['status'] in TERMINAL | {'submitting'}:
                raise ApplyConflict('This application is closed or submitting.')
            if run_id not in self.browsers:
                raise ApplyConflict('No application browser is open. Resume the task first.')
            if request.action == 'start':
                if state['version'] != request.expected_version:
                    raise ApplyConflict('The application changed. Refresh before taking control.')
                state = self.repository.save(state, status='needs_input', pause_requested=True,
                    chat_revision=state.get('chat_revision', 0)+1, review_digest=None, retry_at=None, paused_for_control=True,
                    message='Paused for browser control. Your current page is preserved. Resume when ready.')
                self.scheduled.pop(run_id, None)
            if not state.get('pause_requested'):
                raise ApplyConflict('Pause the agent before controlling its browser.')
            revision = state.get('chat_revision', 0)
        def control():
            latest = self.repository.get(run_id)
            if (self.closed or not latest.get('pause_requested')
                    or latest.get('chat_revision', 0) != revision
                    or latest['status'] in TERMINAL | {'submitting'}):
                raise ApplyConflict('The task resumed or changed. Take control again.')
            browser = self.browsers.get(run_id)
            if not browser:
                raise ApplyConflict('The browser is no longer open.')
            result = control_browser(browser, request, f'{parent_scope}:application:{run_id}:{revision}')
            self.preview_times.pop(run_id, None)
            self._capture_preview(run_id, browser)
            return result
        try:
            return self._submit(run_id, control).result()
        except BrowserBlocked as exc:
            raise ApplyConflict(str(exc)) from None
        except ApplyConflict:
            raise
        except Exception:
            # Playwright exceptions can contain typed input. Never return them.
            raise ApplyConflict('Browser control could not complete. Refresh the view before trying again.') from None

    def account(self, run_id, request, *, switch=False):
        with self.lock:
            state = self.repository.get(run_id)
            if (self.closed or state["status"] != "needs_input" or state["version"] != request.expected_version
                    or not state.get("account_request") or run_id not in self.browsers):
                raise ApplyConflict("The account request changed. Refresh and review the current page.")
            if request.origin != state["account_request"]["origin"]:
                raise ApplyConflict("Credentials must be sent to the site shown in the account request.")
            updated = self.repository.save(state, status="queued", pause_requested=False, review_digest=None,
                account_request=None, message="Opening the account form." if switch else "Completing the account step.")
            # This request is held only by the worker job; it is never persisted,
            # appended to messages, or passed to the model.
            self._submit(run_id, self._account, run_id, request, switch, state["account_request"])
            return updated

    @track_run('run_id', 'account')
    def _account(self, run_id, request, switch, expected):
        try:
            state = self._active(run_id)
            if state is None or state.get("pause_requested"): return
            browser = self.browsers[run_id]
            options = {'model_factory':getattr(self.agent_factory(), 'model_factory', None),
                'active':lambda:self._can_act(run_id), 'reserve':lambda kind:self._reserve(run_id,kind)}
            if switch:
                authentication.switch_mode(browser, request.mode, request.origin, **options)
            else:
                authentication.authenticate(browser, request, expected=expected, **options)
            self._drive(run_id, None)
        except Exception as exc:
            browser = self.browsers.get(run_id)
            message = str(exc) if isinstance(exc, BrowserBlocked) else "The account step needs attention in Chrome. Continue after checking it."
            try:
                fields = browser.observe() if browser else []
            except Exception:
                fields = []
            self._browser_handoff(run_id, message, fields)

    def chat(self, run_id, request):
        with self.lock:
            state = self.repository.get(run_id)
            if any(m["id"] == request.message_id for m in state.get("messages", [])):
                return state
            if self.closed or state["status"] in TERMINAL | {"submitting"}:
                raise ApplyConflict("This attempt no longer accepts changes.")
            if state.get("reopening"):
                raise ApplyConflict("The application is reopening. Wait for the browser to be ready.")
            fields = {f["key"]: f for f in state["fields"]}
            if request.field_key and request.field_key not in fields:
                raise ApplyConflict("Choose a question from the current application.")
            from eregion.runtime.tasks.control import requests_resume, requests_pause, resume_command_only
            pause = requests_pause(request.text)
            resume = requests_resume(request.text)
            control_only = pause or (resume and resume_command_only(request.text))
            if not pause and not resume and not request.field_key and state.get("automation_mode", "autofill") != "hybrid":
                raise ApplyConflict("General feedback requires Agent + autofill. In autofill-only mode, choose the question you are answering.")
            answers = {}
            if request.field_key and not pause:
                answer = request.text
                if fields[request.field_key]["kind"] == "checkbox":
                    answer = boolean_answer(answer)
                    if answer is None:
                        raise ApplyConflict("Answer this checkbox with yes or no.")
                answers[request.field_key] = answer
            message = {"id": request.message_id, "role": "user", "text": request.text,
                "field_key": request.field_key, "control": control_only, "at": now()}
            input_request=state.get('input_request')
            if input_request and not control_only:
                message['in_reply_to']=input_request['id']
            if answers:
                message["bound_answers"] = answers
                message["binding_method"] = "explicit_field"
            was_paused = state["status"] in PAUSED or run_id in self.scheduled
            self.scheduled.pop(run_id, None)
            updated = self.repository.save(state,
                status="needs_input" if pause else "queued" if was_paused else state["status"],
                paused_for_control=False,
                messages=[*state.get("messages", []), message][-100:], chat_revision=state.get("chat_revision", 0) + 1,
                input_request=input_request if control_only else None,
                answers={**state["answers"], **answers}, review_digest=None, pause_requested=pause,
                retry_at=None, recovery_attempts=0, continuations=0,
                visual_fallback=request.visual_fallback if request.visual_fallback is not None else state.get("visual_fallback", False),
                budget_model_base=state.get("agent_calls", 0), budget_tool_base=state.get("agent_tools", 0),
                answer_sources={**state.get("answer_sources", {}), **{key: {"type": "chat", "message_id": request.message_id} for key in answers}},
                message="Paused at your request. Continue when ready." if pause else "Your feedback will be read before the next agent action.")
            self.repository.remember(updated, answers)
            if was_paused and not pause:
                self._submit(run_id, self._drive, run_id, None, frozenset(answers))
            return updated

    def _say(self, run_id, text):
        with self.lock:
            state = self._active(run_id)
            if state is not None:
                for old in reversed(state.get('messages', [])):
                    if old.get('role') == 'user':break
                    if old.get('role') == 'assistant' and old.get('text','').strip() == text.strip():return
                message = {"id": str(uuid4()), "role": "assistant", "text": text, "at": now()}
                self.repository.save(state, messages=[*state.get("messages", []), message][-100:])

    def _checkpoint(self, run_id, **changes):
        with self.lock:
            state = self._active(run_id)
            if state is not None and not state.get("pause_requested"):
                pending = self.buffered.pop(run_id, {})
                self.repository.save(state, **{**pending, **changes})

    def _retry_delay(self, reason, attempts, retry_after=None):
        """Seconds before the next automatic attempt, or None when retries are exhausted."""
        if self.retry_delays is not None:  # An explicit schedule applies to every reason.
            return self.retry_delays[attempts] if attempts < len(self.retry_delays) else None
        delays = RETRY_POLICY.get(reason, RETRY_POLICY["default"])
        if attempts >= len(delays):
            return None
        delay = delays[attempts] * random.uniform(.8, 1.2)  # Jitter avoids synchronized retries.
        if retry_after:
            delay = max(delay, retry_after)
        return min(delay, MAX_RETRY_DELAY)

    def _schedule_recovery(self, run_id, reason, *, progressed=False, retry_after=None):
        with self.lock:
            state = self._active(run_id)
            if state is None or state.get("pause_requested") or state["status"] == "submitting": return False
            if state.get("automation_mode") != "hybrid": return False
            if state.get("agent_calls", 0) - state.get("budget_model_base", 0) >= self.max_model_calls: return False
            if state.get("agent_calls", 0) >= self._allowance(state): return False
            if state.get('agent_tools',0)-state.get('budget_tool_base',0)>=self.max_tool_calls:return False
            attempts = state.get("recovery_attempts", 0)
            if reason == "yielded":
                delay = 0
                attempts=0
                message = "Continuing from the saved decisions and current page."
            elif (reason in RETRYABLE
                    and (delay := self._retry_delay(reason, attempts, retry_after)) is not None):
                attempts += 1
                message = "Recovering this step automatically. Your completed answers are saved."
            else:
                return False
            self.repository.save(state, status="queued", retry_at=time.time() + delay,
                recovery_attempts=attempts, continuations=state.get("continuations", 0) + 1,
                recovery_note=f"Previous step ended with {reason}. Observed progress: {progressed}. Inspect the latest state and change unsuccessful strategies.",
                review_digest=None, message=message)
            self.scheduled[run_id] = (time.monotonic() + delay, state.get("chat_revision", 0))
            return True

    def _accept_chat_answer(self, run_id, key, value, message_id):
        with self.lock:
            state = self._active(run_id)
            if state is None or state.get("pause_requested"):
                raise BrowserBlocked("The attempt is paused or cancelled.")
            # The model chose which field this reply answers. Use it in this run,
            # but reuse it elsewhere only after the user approves it at review.
            updated=self.repository.save(state, answers={**state["answers"], key: value},
                answer_sources={**state.get("answer_sources", {}), key: {"type": "chat", "message_id": message_id,
                    "interpretation": "model", "pending_approval": True}})
            self._save_literal_profile_facts(updated,{key:value},message_id)

    def _save_literal_profile_facts(self,state,answers,message_id):
        message=next((m for m in state.get('messages',[]) if m['id']==message_id and m['role']=='user'),{})
        changes=self.repository.save_literal_profile_facts(state,answers,message.get('text',''))
        if changes:self._say(state['run_id'],'Saved '+', '.join(f'{k.title()} = {v}' for k,v in changes.items())+' to your details.')

    def _bind_answers(self, run_id, items, chat_revision):
        with self.lock:
            state = self._active(run_id)
            if state is None or state.get("pause_requested") or state.get("chat_revision", 0) != chat_revision:
                raise BrowserBlocked("New feedback arrived; interpret the latest conversation again.")
            values = {item["field_key"]: item["value"] for item in items}
            messages = [dict(message) for message in state.get("messages", [])]
            for message in messages:
                bound = {item["field_key"]: item["value"] for item in items if item["message_id"] == message["id"]}
                if bound:
                    message["bound_answers"] = {**message.get("bound_answers", {}), **bound}
                    message["binding_method"] = "model"
            # §13: the model judges each answer's reuse scope; a verbatim statement of the user
            # with a broader scope is reusable now, while interpretations wait for review.
            literal = lambda item: re.sub(r'\s+', ' ', str(item['value'])).strip().casefold() == re.sub(r'\s+', ' ', item['quote']).strip().casefold()
            scopes = {item["field_key"]: {"all_applications": "global", "this_employer": "employer"}[item["reuse"]]
                      for item in items if item.get("reuse") in {"all_applications", "this_employer"} and literal(item)}
            sources = {item["field_key"]: {"type": "chat", "message_id": item["message_id"], "quote": item["quote"],
                "interpretation": "user_statement" if item["field_key"] in scopes else "model",
                "pending_approval": item["field_key"] not in scopes} for item in items}
            updated=self.repository.save(state, answers={**state["answers"], **values},
                answer_sources={**state.get("answer_sources", {}), **sources}, messages=messages)
            if scopes:
                self.repository.remember(updated, {key: values[key] for key in scopes}, scopes=scopes)
            for message_id in dict.fromkeys(item['message_id'] for item in items):
                self._save_literal_profile_facts(updated,{i['field_key']:i['value'] for i in items if i['message_id']==message_id},message_id)

    def _recover_chat_answers(self, state):
        # A previously failed model call may have left a compact reply unused.
        # Replay later corrections over earlier chat answers, but never overwrite
        # newer chat replies or explicit edits without a known chronological order.
        messages, recovered, sources = [], {}, {}
        order = {m["id"]: index for index, m in enumerate(state.get("messages", []))}
        for index, original in enumerate(state.get("messages", [])):
            message = dict(original)
            if message["role"] == "user" and not message.get("field_key") and not message.get("control") and "bound_answers" not in message:
                bound = ordered_answers(message["text"], messages, state["fields"])
                if bound:
                    message["bound_answers"] = bound
                    message["binding_method"] = "fallback"
                    for key, value in bound.items():
                        source = sources.get(key, state.get("answer_sources", {}).get(key, {}))
                        earlier_chat = source.get("type") == "chat" and source.get("message_id") in order and order[source["message_id"]] < index
                        if key not in state["answers"] or earlier_chat:
                            recovered[key] = value
                            sources[key] = {"type": "chat", "message_id": message["id"],
                                "interpretation": "fallback", "pending_approval": True}
            messages.append(message)
        if messages != state.get("messages", []):
            state = self.repository.save(state, messages=messages,
                answers={**state["answers"], **recovered}, answer_sources={**state.get("answer_sources", {}), **sources})
        return state

    def start(self, request, *, material_refs=None, origin_task_id=None):
        from .contracts import Candidate
        from .education import normalize_records
        import base64
        with self.lock:
            if self.closed:
                raise ApplyConflict("The application service is stopping.")
            profile=request.profile.model_dump()
            profile['education']=normalize_records(profile.get('education',[]))
            from .experience import normalize_records as normalize_experience
            profile['experience']=normalize_experience(profile.get('experience',[]))
            request=request.model_copy(update={'profile':Candidate.model_validate(profile)})
            state, created = self.repository.create(request,material_refs=material_refs,origin_task_id=origin_task_id)
            if created:
                if request.save_as_default:
                    self.repository.save_candidate(profile,resume=base64.b64decode(request.resume_base64),name=request.resume_name)
                self._submit(state["run_id"], self._drive, state["run_id"], None)
            return state

    def continue_run(self, run_id, request):
        with self.lock:
            state = self.repository.get(run_id)
            if self.closed or state["version"] != request.expected_version or state["status"] not in PAUSED:
                raise ApplyConflict("Refresh the attempt; it is no longer waiting at this step.")
            state=self.repository.refresh_education(state)
            if request.confirm_submit and state["status"] != "awaiting_review":
                raise ApplyConflict("The filled form must be ready for review before confirming submission.")
            allowed_keys = {field["key"] for field in state["fields"]}
            if not set(request.answers).issubset(allowed_keys):
                raise ApplyConflict("The form changed. Only answer questions from the latest observation.")
            approval = state["review_digest"] if request.confirm_submit and not request.answers else None
            if request.extend_budget:
                allowance = max(self._allowance(state), state.get('agent_calls', 0)) + BUDGET_EXTENSION
                state = self.repository.save(state, model_call_allowance=allowance,
                    budget_extensions=[*state.get('budget_extensions', []), {'at': now(), 'added': BUDGET_EXTENSION,
                        'allowance': allowance, 'used': state.get('agent_calls', 0)}][-20:])
            sources = {**state.get("answer_sources", {}), **{key: {"type": "user"} for key in request.answers}}
            # Confirming the review approves the interpreted answers it showed.
            # Checked ones are remembered for later applications (approve once).
            promoted = {}
            if approval:
                for item in pending_approvals(state, state["fields"]):
                    sources[item["key"]] = {**sources[item["key"]], "pending_approval": False}
                    if request.remember.get(item["key"], True):
                        promoted[item["key"]] = item["value"]
            messages=state.get('messages',[])
            if request.answers:
                fields={f['key']:f for f in state['fields']}
                messages=[*messages,{'id':str(uuid4()),'role':'user','at':now(),
                    'text':'\n'.join(f"{fields[k]['label']}: {('Yes' if v else 'No') if isinstance(v,bool) else v}" for k,v in request.answers.items()),
                    'bound_answers':request.answers,'binding_method':'explicit_batch',
                    'in_reply_to':(state.get('input_request') or {}).get('id')}][-100:]
            updated = self.repository.save(state, status="queued", answers={**state["answers"], **request.answers},
                messages=messages,chat_revision=state.get('chat_revision',0)+bool(request.answers),
                input_request=None,
                review_digest=None if request.answers else state.get('review_digest'),
                pause_requested=False,
                recovery_attempts=0, continuations=0, retry_at=None,
                budget_model_base=state.get("agent_calls", 0), budget_tool_base=state.get("agent_tools", 0),
                answer_sources=sources, pending_approvals=[],
                consent_policy=request.consent_policy or state.get("consent_policy", "ask"),
                automation_mode=request.automation_mode or state.get("automation_mode", "autofill"),
                visual_fallback=request.visual_fallback if request.visual_fallback is not None else state.get("visual_fallback", False),
                message="Continuing the application.")
            self.repository.remember(updated, request.answers)
            if promoted:
                self.repository.remember({**updated, "fields": list(review_fields(updated, updated["fields"]).values())},
                    promoted, scopes=request.memory_scopes)
            self._submit(run_id, self._drive, run_id, approval, frozenset(request.answers))
            return updated

    def cancel(self, run_id, version):
        with self.lock:
            state = self.repository.get(run_id)
            if state["version"] != version or state["status"] in TERMINAL | {"submitting"}:
                raise ApplyConflict("This attempt cannot be cancelled at its current step.")
            state = self.repository.save(state, status="cancelled", pause_requested=False, reopening=False,
                review_digest=None, retry_at=None, message="Application cancelled before submission. Your saved progress can be resumed.")
            self.scheduled.pop(run_id, None)
            self._submit(run_id, self._close_browser, run_id)
            return state

    def _handoff_message(self, state, text, message_id):
        if not text or not message_id or any(m['id'] == message_id for m in state.get('messages', [])):
            return state
        return self.repository.save(state, messages=[*state.get('messages', []),
            {'id': message_id, 'role': 'user', 'text': text, 'at': now()}][-100:])

    def resume(self, run_id, version, *, initial_message=None, message_id=None):
        with self.lock:
            state = self.repository.get(run_id)
            if self.closed or state["version"] != version:
                raise ApplyConflict("The attempt changed. Refresh before resuming.")
            updated = self.repository.reopen(state)
            updated = self._handoff_message(updated, initial_message, message_id)
            self._submit(run_id, self._finish_reopen, run_id, updated["chat_revision"])
            return updated

    def replace_materials(self, run_id, resume, cover_letter, *, profile=None, material_refs=None,
                          initial_message=None, message_id=None):
        """Browser work remains serialized; no previous submission approval survives."""
        with self.lock:
            if self.closed:raise ApplyConflict('The application service is stopping.')
            state = self.repository.get(run_id)
            updated = self.repository.replace_documents(state,resume,cover_letter,profile=profile,material_refs=material_refs)
            updated = self._handoff_message(updated, initial_message, message_id)
            self.scheduled.pop(run_id,None)
            self._submit(run_id, self._finish_reopen, run_id, updated['chat_revision'])
            return updated

    def _finish_reopen(self, run_id, revision):
        with self.lock:
            state = self._active(run_id)
            if (state is None or not state.get("reopening") or state["status"] != "queued"
                    or state.get("chat_revision") != revision):
                return
            self.repository.save(state, reopening=False, pause_requested=False,
                message="Restoring saved answers in a new browser session.")
        self._close_browser(run_id)
        self._drive(run_id, None)

    def _close_browser(self, run_id):
        browser = self.browsers.pop(run_id, None)
        self.previews.pop(run_id, None)
        self.preview_times.pop(run_id, None)
        with self.lock:
            try:
                finished = self.repository.get(run_id)["status"] in TERMINAL
            except Exception:
                finished = True
            if finished:  # Free the worker slot; a reopened run is assigned again.
                self.assigned.pop(run_id, None); self.sites.pop(run_id, None)
        if browser:
            try:
                browser.close()
            except Exception:
                pass

    def _active(self, run_id):
        state = self.repository.get(run_id)
        return state if not self.closed and state["status"] not in TERMINAL else None

    def _track_control(self, state):
        with self.lock:
            previous = self.controls.get(state['run_id'], {})
            if previous.get('version', 0) > state.get('version', 0):
                return
            self.revisions[state['run_id']] = state.get('chat_revision', 0)
            self.controls[state['run_id']] = {key: state.get(key, 0) for key in
                ('version', 'status', 'pause_requested', 'chat_revision', 'agent_calls', 'agent_tools', 'budget_model_base', 'budget_tool_base')}

    def _control(self, run_id):
        if run_id not in self.controls:
            self._track_control(self.repository.get(run_id))
        return self.controls[run_id]

    def _can_act(self, run_id):
        state = self._control(run_id)
        return not self.closed and state['status'] not in TERMINAL and not state.get('pause_requested')

    def _refresh_hook(self, run_id):
        def refresh():
            return self._agent_context(run_id)
        refresh.revision = lambda: self._control(run_id).get('chat_revision', 0)
        return refresh

    def _progress(self, run_id, fields, tool, key, outcome):
        with self.lock:
            state = self._active(run_id)
            if state is not None:
                field = next((f for f in fields if f["key"] == key), None)
                action = {"tool": tool, "field_key": key, "label": field["label"] if field else "Application form",
                    "outcome": outcome, "source": state.get("answer_sources", {}).get(key), "at": now()}
                last = state.get("last_tool_result") or {}
                if last.get("tool") == tool and last.get("field_key") == key:
                    action["detail"] = {k:v for k,v in last.get("result",{}).items()
                        if k in {"error","errors","verified","decision","target"}}
                if state.get('automation_mode') == 'hybrid':
                    pending = self.buffered.setdefault(run_id, {})
                    pending.update(fields=fields, last_action=action,
                        actions=[*pending.get('actions', state.get('actions', [])), action][-100:])
                else:
                    self.repository.save(state, fields=fields, last_action=action,
                        actions=[*state.get('actions', []), action][-100:], message=f"{tool}: {outcome}.")
        browser = self.browsers.get(run_id)
        if browser: self._capture_preview(run_id, browser)

    def _reserve(self, run_id, kind):
        with self.lock:
            state = self._control(run_id)
            if not self._can_act(run_id):
                return False
            key, limit = ("agent_calls", self.max_model_calls) if kind == "model" else ("agent_tools", self.max_tool_calls)
            pending = self.buffered.setdefault(run_id, {})
            used = pending.get(key, state.get(key, 0))
            if kind == "model" and used >= self._allowance(state):
                return False
            base = state.get("budget_model_base" if kind == "model" else "budget_tool_base", 0)
            if used - base >= limit:
                return False
            pending[key] = used + 1
            return True

    @staticmethod
    def _allowance(state):
        return state.get('model_call_allowance') or APPLICATION_MODEL_CALLS

    def _agent_context(self,run_id):
        state=self.repository.agent_context(run_id)
        state={**state, **self.buffered.get(run_id, {})}
        return {**state,'budget_remaining':{
            'model_calls':max(0,min(self.max_model_calls-state.get('agent_calls',0)+state.get('budget_model_base',0),
                self._allowance(state)-state.get('agent_calls',0))),
            'tool_calls':max(0,self.max_tool_calls-state.get('agent_tools',0)+state.get('budget_tool_base',0))}}

    @staticmethod
    def _progress_digest(fields,page,state):
        # Navigating, discovering a menu and binding an answer are progress even
        # before an input's value changes. Operational counters are not progress.
        payload=[form_digest(fields),page,state.get('answers'),state.get('field_decisions'),state.get('education_bindings'),state.get('experience_bindings')]
        return hashlib.sha256(json.dumps(payload,sort_keys=True,default=str).encode()).hexdigest()

    def _publish_question(self, run_id, text, keys, fields, revision):
        with self.lock:
            state = self._active(run_id)
            if state is None or state.get('pause_requested'):
                raise BrowserBlocked('Application paused or cancelled.')
            if state.get('chat_revision', 0) != revision:
                raise BrowserBlocked('New feedback arrived. Read it before asking another question.')
            pending = state.get('input_request')
            if pending:
                return pending  # One batch, one reply; never overwrite or repeat it.
            keys = list(dict.fromkeys(keys))
            question = {'id':str(uuid4()), 'role':'assistant', 'text':text,
                'question_keys':keys, 'at':now(), 'kind':'question'}
            messages = list(state.get('messages',[]))
            if messages and messages[-1].get('role') == 'assistant' and messages[-1].get('text') == text:
                question['id'] = messages[-1]['id']
                messages[-1] = question
            else:
                messages.append(question)
            request = {'id':question['id'], 'text':text, 'field_keys':keys,
                'fields':[f for f in fields if f['key'] in keys]}
            self.repository.save(state, fields=fields, pending_fields=keys, input_request=request,
                messages=messages[-100:], review_digest=None,
                message='Your question is ready. I am continuing the independent steps.')
            return request

    def _pause_for_question(self, run_id, text, keys, fields, revision):
        with self.lock:
            state = self._active(run_id)
            if state is None or state.get('pause_requested'):return
            if state.get('chat_revision', 0) != revision:
                self._submit(run_id, self._drive, run_id, None)
                return
            self._publish_question(run_id, text, keys, fields, revision)
            state = self.repository.get(run_id)
            self.repository.save(state, status='needs_input', agent_reason='user_question', retry_at=None,
                message='Waiting for your reply. Your completed work is saved.')

    def reply_question(self, run_id, request):
        """Answer a question while the worker runs, independent of polling versions."""
        with self.lock:
            state = self.repository.get(run_id)
            previous = next((m for m in state.get('messages',[]) if m.get('in_reply_to') == request.request_id
                and m.get('binding_method') == 'explicit_batch'), None)
            if previous and previous.get('bound_answers') == request.answers:return state
            pending = state.get('input_request')
            if (self.closed or state['status'] in TERMINAL | {'submitting'} or state.get('reopening')
                    or not pending or pending['id'] != request.request_id):
                raise ApplyConflict('That question has already been answered or changed. Read the latest conversation.')
            fields = {f['key']:f for f in pending.get('fields',[])}
            if not request.answers or not set(request.answers).issubset(fields):
                raise ApplyConflict('Answer fields from this question, or reply in chat.')
            message = {'id':str(uuid4()), 'role':'user', 'at':now(), 'in_reply_to':pending['id'],
                'bound_answers':request.answers, 'binding_method':'explicit_batch',
                'text':'\n'.join(f"{fields[k]['label']}: {('Yes' if v else 'No') if isinstance(v,bool) else v}"
                    for k,v in request.answers.items())}
            paused = state.get('pause_requested',False)
            needs_worker = state['status'] in PAUSED or run_id in self.scheduled
            self.scheduled.pop(run_id, None)
            updated = self.repository.save(state, status='needs_input' if paused else 'queued' if needs_worker else state['status'],
                messages=[*state.get('messages',[]),message][-100:], chat_revision=state.get('chat_revision',0)+1,
                answers={**state['answers'],**request.answers}, input_request=None, pending_fields=[], review_digest=None,
                answer_sources={**state.get('answer_sources',{}),**{k:{'type':'chat','message_id':message['id']} for k in request.answers}},
                retry_at=None, recovery_attempts=0, budget_model_base=state.get('agent_calls',0),
                budget_tool_base=state.get('agent_tools',0), message='Your answers are saved. I will read them before the next action.')
            # A pending question can refer to the previous page. Its frozen field
            # labels preserve memory scope even after independent navigation.
            self.repository.remember({**updated,'fields':list(fields.values())},request.answers)
            if needs_worker and not paused:
                self._submit(run_id, self._drive, run_id, None,frozenset(request.answers))
            return updated

    @track_run('run_id', 'application')
    def _adopt_manual_edits(self, run_id, browser, before):
        """§13: after the user controlled the browser, their changes are their answers.
        A field whose value changed during the takeover and differs from the agent's target
        is kept as a manual answer, so the resumed agent does not overwrite it."""
        try:
            current = browser.observe()
        except Exception:
            return
        earlier = {f["key"]: f.get("value") for f in before}
        with self.lock:
            state = self._active(run_id)
            if state is None:
                return
            edits = {f["key"]: f["value"] for f in current
                     if f["key"] in earlier and f["kind"] not in {"file", "password"} and f.get("value") not in (None, "")
                     and f["value"] != earlier[f["key"]] and not f.get("invalid")
                     and (desired_answer(f, state) is None or not matches(f, desired_answer(f, state)))}
            if edits:
                self.repository.save(state, answers={**state["answers"], **edits},
                    answer_sources={**state.get("answer_sources", {}),
                        **{key: {"type": "manual", "interpretation": "browser_edit", "pending_approval": True} for key in edits}})

    def _drive(self, run_id, approval, edited_keys=frozenset()):
        fields = None
        try:
            with self.lock:
                state = self._active(run_id)
                if state is None or state.get("pause_requested"):
                    return
                state = self.repository.refresh_education(state)
                taken_over = state.get("paused_for_control") and state.get("fields")
                state = self.repository.save(state, status="running", retry_at=None, account_request=None,
                    handoff_kind=None, paused_for_control=False, message="Inspecting the application and choosing the next step.")
            browser = self.browsers.get(run_id)
            if browser is not None and not browser.is_open():
                self._close_browser(run_id)
                browser = None
            if browser is not None and taken_over:
                self._adopt_manual_edits(run_id, browser, taken_over)
            if browser is not None and getattr(browser, "headless", True) is False and hasattr(browser, "set_visible"):
                # The user controlled the real Chrome window; the agent works hidden again.
                try:browser.set_visible(False)
                except BrowserBlocked:pass
            if browser is None:
                # A finished browser may remain open briefly for inspection, but
                # never accumulate an unbounded number of browser processes.
                # Only this worker may close its own browsers (thread-bound).
                for old_id in self._owned(self.executor.current()):
                    if self.repository.get(old_id)["status"] in TERMINAL:
                        self._close_browser(old_id)
                if self.browser_factory is ApplicationBrowser:
                    browser = self.browser_factory(state["url"], session_store=self.session_store,
                        owner_key=self.repository.owner_key(state["profile"]))
                else:
                    browser = self.browser_factory(state["url"])
                self.browsers[run_id] = browser
                # Repeated controls can reuse their DOM IDs for another record.
                # Preserve confirmed education answers by record/component and
                # let the agent map the fresh page before filling them again.
                from .education import new_browser_bindings
                with self.lock:
                    current = self._active(run_id)
                    if current is None:return
                    state = self.repository.save(current, **new_browser_bindings(current))
                approval = None  # An approval never survives a new browser session.
            proof = browser.confirmation()
            if proof:
                self.repository.save(self.repository.get(run_id), status="submitted", confirmation=proof,
                    message="The employer displayed an application confirmation.")
                self._close_browser(run_id)
                return
            fields = browser.observe()
            barrier = browser.barrier()
            if barrier:
                self._browser_handoff(run_id, barrier, fields)
                return
            page = browser.page_state() if hasattr(browser, "page_state") else None
            before_digest=self._progress_digest(fields,page,state)
            if not fields and not (page and (page.get('controls') or page.get('text'))) and state.get('automation_mode')!='hybrid':
                raise BrowserBlocked("No supported application form is visible. Open the form in the browser, then continue.")
            with self.lock:
                state = self._active(run_id)
                if state is None or state.get("pause_requested"): return
                recalled, sources = self.repository.recalled_answers(state, fields)
                state = self.repository.save(state, fields=fields, answers={**state["answers"], **recalled},
                    answer_sources={**state.get("answer_sources", {}), **sources})
            initial_chat_revision = state.get("chat_revision", 0)
            outcome = {"reason": "ready", "field_keys": []}
            fill_report = {}
            if approval is None:
                def bulk_fill():
                    with self.lock:
                        current = self.repository.get(run_id)
                        recalled, sources = self.repository.recalled_answers(current, browser.observe())
                        if recalled:
                            current = self.repository.save(current, answers={**current["answers"], **recalled},
                                answer_sources={**current.get("answer_sources", {}), **sources})
                    chat_keys = {key for key, source in current.get("answer_sources", {}).items() if source["type"] == "chat"}
                    result = autofill(browser, current, lambda: self.repository.resume(run_id), edited_keys=edited_keys | chat_keys,
                        active=lambda: self._can_act(run_id) and self.repository.get(run_id).get("chat_revision", 0) == current.get("chat_revision", 0),
                        progress=lambda *args: self._progress(run_id, *args))
                    with self.lock:
                        latest=self.repository.get(run_id)
                        if self._can_act(run_id) and latest.get('chat_revision',0)==current.get('chat_revision',0):
                            self._checkpoint(run_id,document_refresh=current.get('document_refresh',{}),
                                autofill_failures=current.get('autofill_failures',{}),
                                document_uploads=current.get('document_uploads',{}))
                    fill_report.update(result)
                    return result
                if state.get("automation_mode", "autofill") == "hybrid":
                    # Explicit chat answers must survive an unavailable model.
                    if edited_keys:
                        bulk_fill()
                    outcome = self.agent_factory().run(browser, state, autofill=bulk_fill,
                        active=lambda: self._can_act(run_id),
                        reserve=lambda kind: self._reserve(run_id, kind),
                        report=lambda *args: self._progress(run_id, *args),
                        refresh=self._refresh_hook(run_id), communicate=lambda text: self._say(run_id, text),
                        publish_question=lambda text,keys,fields,revision: self._publish_question(run_id,text,keys,fields,revision),
                        accept_answer=lambda key, value, message_id: self._accept_chat_answer(run_id, key, value, message_id),
                        checkpoint=lambda **changes: self._checkpoint(run_id, **changes),
                        bind_answers=lambda items, revision: self._bind_answers(run_id, items, revision))
                    # Injected/legacy runners may yield without the shared host's
                    # persist hook. Flush reservations before checking recovery.
                    if self.buffered.get(run_id):
                        self._checkpoint(run_id)
                    if outcome["reason"] in {"model_payment_required", "model_error", "model_timeout", "model_unavailable", "model_rate_limited", "invalid_model_response", "internal_error"}:
                        with self.lock:
                            current = self._active(run_id)
                            if current and current.get("chat_revision", 0) == initial_chat_revision:
                                self._recover_chat_answers(current)
                        bulk_fill()
                else:
                    # Autofill-only mode deliberately has no model interpreter.
                    # Preserve its deterministic chat fallback.
                    with self.lock:
                        current = self._active(run_id)
                        if current and current.get("chat_revision", 0) == initial_chat_revision:
                            self._recover_chat_answers(current)
                    bulk_fill()
            state = self._active(run_id)
            if state is None:
                self._close_browser(run_id)
                return
            if state.get("pause_requested"): return
            barrier = browser.barrier()
            if barrier or outcome["reason"] == "authentication_required":
                self._browser_handoff(run_id, barrier or "Sign in or create your account in the open Chrome window, then send continue here.", browser.observe())
                return
            if state.get("chat_revision", 0) != outcome.get("chat_revision", initial_chat_revision):
                self._submit(run_id, self._drive, run_id, None)
                return
            fields = browser.observe()
            if outcome.get('question'):
                self._pause_for_question(run_id,outcome['question'],outcome.get('field_keys',[]),fields,
                    outcome.get('chat_revision',initial_chat_revision))
                return
            missing = [field for field in fields if (field["required"] and not field["value"]) or field.get("invalid") or
                (state.get("consent_policy", "ask") == "ask" and is_consent(field) and field["key"] not in state["answers"])]
            for field in fields:
                if (field["key"] in fill_report.get("failed", {}) or field["key"] in state["answers"]) and field not in missing:
                    desired = desired_answer(field, state)
                    if desired is not None and not matches(field, desired):
                        missing.append(field)
            reason = outcome["reason"]
            # The agent can ask about optional fields too (for example voluntary
            # self-identification). They must reach the same question/option UI
            # as required fields, rather than only the pending_fields list.
            requested = set(outcome.get("field_keys", []))
            drafts = visible_drafts(state, fields)
            for field in fields:
                if field["key"] not in requested or field in missing:
                    continue
                if not field.get("required") and field["key"] in drafts:
                    continue  # An optional drafted field stays a suggestion (see below).
                desired = desired_answer(field, state)
                if ((desired is None and (not field["value"] or field.get("invalid")))
                        or (desired is not None and not matches(field, desired))):
                    missing.append(field)
            # A draft for an optional field is a suggestion, not a blocker: left unapproved,
            # the field stays blank (Veeva pilot: two optional essays held review forever).
            missing.extend(f for f in fields if f['key'] in drafts and f.get('required') and f not in missing)
            paths = unresolved_paths(fields,state)
            missing.extend(f for f in fields if f['key'] in paths and f not in missing)
            if reason == "ready" and any(desired_answer(f,state) is not None for f in missing):
                # A model 'done' response is not proof that a known answer passed
                # the site's validation. Return its latest error to the agent.
                reason = "validation_error"
            page = browser.page_state() if hasattr(browser, "page_state") else None
            from .education import gaps as education_gaps
            if reason == 'ready' and education_gaps(state,fields,page,final=True):
                reason='validation_error'
            if reason=='ready' and pending_documents(state,fields):reason='document_upload_required'
            if reason == "ready" and page is not None and not page["can_submit"]:
                reason = "navigation_required"
            if reason != "ready" and not outcome.get("refused") and self._schedule_recovery(run_id, reason, retry_after=outcome.get("retry_after"),
                    progressed=self._progress_digest(fields,page,state) != before_digest):
                return
            if reason == 'yielded':
                reason = 'agent_limit'
            if reason == 'agent_limit' and state.get('agent_calls', 0) >= self._allowance(state):
                reason = 'application_budget'
            if state.get('input_request') and reason != 'ready':
                pending = state['input_request']
                self._pause_for_question(run_id,pending['text'],pending['field_keys'],fields,
                    outcome.get('chat_revision',initial_chat_revision))
                return
            if missing or reason != "ready":
                if reason != "model_payment_required" and hasattr(browser, "enrich_options"):
                    fields = browser.enrich_options(fields, active=lambda: self._can_act(run_id))
                    latest = {f["key"]: f for f in fields}
                    missing = [latest.get(f["key"], f) for f in missing]
                pending = list(dict.fromkeys([f["key"] for f in missing] + outcome.get("field_keys", [])))
                messages = {"model_payment_required": MODEL_PAYMENT_MESSAGE,
                    "model_unavailable": "The configured AI model is unavailable. Check the model settings, or switch to autofill only.",
                    "model_rate_limited": "The AI service is busy. Your answers are saved; continue to retry.",
                    "model_error": "The AI tool call failed. You can retry or use autofill only.",
                    "internal_error": "The agent hit an internal error and stopped this step. Your answers are saved; the server log has details. Continue to try again.",
                    "invalid_model_response": "The configured model did not return a valid tool call. Use a model supporting tool calls.",
                    "model_timeout": "The model response timed out. Completed fields are saved; continue to retry.",
                    "agent_limit": "The agent reached its bounded call limit. Completed fields are saved.",
                    "application_budget": "This application used its whole model-call allowance. Completed fields are saved. Allow more calls to continue, or finish it in the browser.",
                    "page_budget": "This page used its model-call budget without finishing. Completed fields are saved; check the highlighted fields, answer or fix them, then continue.",
                    "no_progress": "The agent stopped repeating an unsuccessful action. Check the highlighted control."}
                messages["navigation_required"] = "This application has another step. Enable Agent + autofill, or continue in the browser and then send continue here."
                messages['document_upload_required']='The selected resume or cover letter still needs uploading. Inspect the attachment control before reviewing this application.'
                messages["validation_error"] = "The website still rejects a known answer or needs its final sub-option. Your facts are saved; check the current control or continue to retry."
                with self.lock:
                    state = self._active(run_id)
                    if state is None or state.get("pause_requested"): return
                    if state.get("chat_revision", 0) != outcome.get("chat_revision", initial_chat_revision):
                        self._submit(run_id, self._drive, run_id, None)
                        return
                    chat_messages = state.get("messages", [])
                    if missing and reason not in {'no_progress','agent_limit','page_budget','application_budget','model_payment_required'}:
                        unknown = [f for f in missing if desired_answer(f, state) is None]
                        failed = [f for f in missing if desired_answer(f, state) is not None]
                        text, question_keys = input_prompt(unknown, failed, {f["key"]: desired_answer(f, state) for f in failed})
                        if any(key in drafts for key in question_keys):
                            text = "I drafted answers from your resume and supplied details below. Edit the drafts and add any missing details, then send your answers.\n\n" + text
                        if reason in {"model_error", "model_timeout", "model_unavailable", "model_rate_limited", "invalid_model_response", "internal_error"}:
                            text = messages[reason] + " Your chat messages are saved.\n\n" + text
                    else:
                        text = messages.get(reason, "The remaining step needs attention. Your completed answers are saved.")
                        question_keys = []
                    if not chat_messages or chat_messages[-1].get("text") != text:
                        chat_messages = [*chat_messages, {"id": str(uuid4()), "role": "assistant", "at": now(),
                            "text": text, "question_keys": question_keys}][-100:]
                    self.repository.save(state, status="needs_input", fields=fields, pending_fields=pending,
                        review_digest=None, agent_reason=reason, messages=chat_messages,
                        input_request=None,
                        message=messages.get(reason, "Completed fields are kept. Answer only the highlighted questions, then continue."))
                return
            digest = self._review_digest(fields, page)
            if (not state["auto_submit"] or state.get("recovery_review_required")
                    or review_required(state, fields)) and approval != digest:
                with self.lock:
                    state = self._active(run_id)
                    if state is None or state.get("pause_requested"): return
                    if state.get("chat_revision", 0) != outcome.get("chat_revision", initial_chat_revision):
                        self._submit(run_id, self._drive, run_id, None)
                        return
                    review = {f["key"]:f for f in state.get("review_fields", [])}
                    review.update({f["key"]:f for f in fields})
                    # Optional fields left empty are the model's decision; show them at review.
                    unfilled = [{"key": f["key"], "label": f["label"], "set_aside": f["key"] in state.get("abandoned_fields", [])}
                                for f in review.values() if not f.get("required") and f.get("value") in (None, "", False)
                                and f["kind"] not in {"checkbox", "file"}]
                    self.repository.save(state, status="awaiting_review", fields=fields, review_fields=list(review.values()),
                        unfilled_optional=unfilled,
                        pending_fields=[], review_digest=digest, pending_approvals=pending_approvals(state, fields),
                        task_plan=[],
                        message="The form is filled. Check the browser and the answers below before submitting.")
                return
            # Submission remains a host-only transition, never a model tool.
            with self.lock:
                current = self._active(run_id)
                if current is None or current.get("pause_requested"):
                    return
                if current.get("chat_revision", 0) != outcome.get("chat_revision", initial_chat_revision):
                    self._submit(run_id, self._drive, run_id, None)
                    return
                self.repository.save(current, status="submitting", fields=fields, recovery_review_required=False,
                    message="Submitting once and waiting for the employer confirmation.")
            browser.submit_clicked = False  # submit() sets True right before its one click
            browser.submit()
            proof = browser.wait_confirmation()
            self.repository.save(self.repository.get(run_id),
                status="submitted" if proof else "submission_unknown", confirmation=proof,
                message="Application confirmed by the employer." if proof else
                "Submission was attempted but confirmation is unavailable. Check the browser or employer email; automatic retry is disabled.")
            if proof:
                self._close_browser(run_id)
        except Exception as exc:
            state = self.repository.get(run_id)
            if state["status"] in TERMINAL or state.get("pause_requested"):
                return
            submitting = state["status"] == "submitting"
            if submitting and getattr(self.browsers.get(run_id), "submit_clicked", True) is False:
                # Failed before the submit button was clicked: nothing reached the employer.
                try:
                    self.repository.save(state, status="needs_input", error_type=type(exc).__name__,
                        message="Not submitted: " + (str(exc) if isinstance(exc, BrowserBlocked) else
                                "the submit step failed before the button was clicked.") +
                                " Check the form, then confirm the review again.",
                        **({"fields": fields} if fields is not None else {}))
                except ApplyConflict:
                    pass
                return
            if not isinstance(exc, BrowserBlocked):
                # Never log the message: Playwright errors can contain typed answers.
                logger.error("Application step failed: run=%s status=%s type=%s\n%s", run_id, state["status"],
                    type(exc).__name__, safe_traceback(exc))
            if not submitting and not isinstance(exc, BrowserBlocked) and self._schedule_recovery(run_id, "browser_error"):
                return
            message = ("Submission may have reached the employer. Check the browser or confirmation email; automatic retry is disabled."
                if submitting else str(exc) if isinstance(exc, BrowserBlocked) else
                "The browser could not finish this step. Check that Chrome is installed and the form is open, then continue.")
            try:
                self.repository.save(state, status="submission_unknown" if submitting else "needs_input",
                    message=message, error_type=type(exc).__name__,
                    **({"fields": fields} if fields is not None else {}))
            except ApplyConflict:
                pass

    @staticmethod
    def _review_digest(fields, page):
        if page is None: return form_digest(fields)
        # Summary-only review pages are compared by their text, except for clocks
        # and "saved 2 minutes ago" notices that change without any edit.
        text = VOLATILE_TEXT.sub("", page["text"])
        return hashlib.sha256(json.dumps({"fields":form_digest(fields), "url":page["url"],
            "heading":page["heading"], "text":text, "can_submit":page["can_submit"]},
            sort_keys=True).encode()).hexdigest()

    def _browser_handoff(self, run_id, message, fields):
        browser = self.browsers.get(run_id)
        try:
            account = authentication.context(browser) if isinstance(browser, ApplicationBrowser) else None
        except Exception:
            account = None
        kind = None
        try:
            if browser is not None and hasattr(browser, "verification_barrier") and browser.verification_barrier():
                kind = "verification"
            elif browser is not None and IDENTITY_PROVIDERS.search(urlsplit(browser.page.url).hostname or ""):
                kind = "identity"
        except Exception:
            pass
        with self.lock:
            state = self._active(run_id)
            if state is None or state.get("pause_requested"): return
            history = state.get("messages", [])
            if not history or history[-1].get("text") != message:
                history = [*history, {"id":str(uuid4()), "role":"assistant", "text":message,
                    "at":now(), "question_keys":[]}][-100:]
            self.repository.save(state, status="needs_input", fields=fields, pending_fields=[],
                input_request=None, review_digest=None, agent_reason="browser_handoff", account_request=account,
                handoff_kind=kind, messages=history, message=message)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.scheduled.clear()
        for index, worker in enumerate(self.executor.workers):
            worker.submit(lambda index=index: [self._close_browser(key) for key in self._owned(index)])
        self.executor.shutdown(wait=True)
        if hasattr(self.repository, 'state_listeners') and self._track_control in self.repository.state_listeners:
            self.repository.state_listeners.remove(self._track_control)
