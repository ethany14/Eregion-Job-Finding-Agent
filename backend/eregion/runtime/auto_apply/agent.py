"""A native tool-calling agent coordinating autofill and individual browser actions.

The model chooses tools; the host validates source references, observation freshness,
budgets and completion. Submission is deliberately outside this tool set.
"""
from __future__ import annotations
from eregion.runtime.telemetry import history_context

import re
from uuid import uuid4

from eregion.runtime.model_runtime import model_timeout

from .browser import BrowserBlocked
from .errors import failure
from .contracts import normalized_label
from .matching import resolve_option, search_text, boolean_answer, is_consent, clean, equivalent
from .autofill import desired_answer, matches, pending_documents
from .decisions import active_decision, protected, preserves_format, signature, source_value
from .contracts import candidate_answer
from .education import COMPONENTS, facts as education_facts, binding as education_binding, gaps as education_gaps, repeatable as repeatable_education, format_date, date_format
from .drafting import prepare_draft, visible_drafts

from eregion.runtime.browser.tools import BROWSER_TOOLS, BROWSER_NAMES, VISUAL_NAMES as BROWSER_VISUAL_NAMES


def tool(name, description, properties=None, optional=None):
    properties = properties or {}
    return {"type": "function", "function": {"name": name, "description": description,
        "parameters": {"type": "object", "properties": {**properties, **(optional or {})},
                       "required": list(properties), "additionalProperties": False}}}


TEXT = {"type": "string"}
OPTION_PAGE = {"offset": {"type": "integer", "minimum": 0, "description": "Continue reading at next_offset from the previous option result."}}
FIELD = {"observation_id": TEXT, "field_key": TEXT}
SCREEN = {"observation_id":TEXT, "screenshot_id":TEXT}
COORD = {"type":"number", "minimum":0, "exclusiveMaximum":1000,
         "description":"Screenshot coordinate normalized to 0..1000 (not CSS pixels)."}
POINT = {"x":COORD, "y":COORD}
VISUAL_TOOLS = {"view_browser", "view_field", "computer_click", "computer_move", "computer_scroll",
                "computer_drag", "computer_key", "computer_type", "computer_wait"}
from .experience import BINDING_COMPONENTS, binding as experience_binding, facts as experience_facts, coverage as experience_coverage, gaps as experience_gaps

TOOLS = [
    tool('bind_experience', 'Bind observed fields to one saved work-experience record. You choose the correspondence. For current-employment checkboxes, then use resolve_field with your explicit yes/no interpretation and cited fact; binding does not decide the boolean.',
         {'observation_id':TEXT, 'experience_id':TEXT, 'fields':{'type':'array','items':{'type':'object',
          'properties':{'field_key':TEXT,'component':{'type':'string','enum':list(BINDING_COMPONENTS)}},
          'required':['field_key','component'],'additionalProperties':False}}}),
    tool('bind_document','Identify an observed file upload as the resume or approved cover letter. Then use autofill to upload that document, replacing an older attachment if required.',
        {**FIELD,'document':{'type':'string','enum':['resume','cover_letter']}}),
    tool('ask_user', 'Ask your own focused question. Empty field_keys means a general question. Set wait_for_reply=false to keep working on independent steps while the user answers. The exact question stays visible.',
        {'text':TEXT,'field_keys':{'type':'array','items':TEXT}}, optional={'wait_for_reply':{'type':'boolean'}}),
    tool('wait_for_user', 'Pause for the existing pending question when no useful independent work remains. Does not repeat the question.'),
    tool('draft_answer', 'Compose an editable, evidence-grounded answer to an open-ended question. This saves a suggestion for the user, not an answer to fill or submit.',
        {**FIELD, 'text':TEXT, 'evidence':{'type':'array','items':{'type':'object',
            'properties':{'source_ref':TEXT,'quote':TEXT}, 'required':['source_ref','quote'],'additionalProperties':False}},
         'missing_details':{'type':'array','items':TEXT}}),
    tool('bind_education','Associate each visible field once with one saved education record. Combined dates use start_date/end_date; use month/year only for separate controls. Autofill formats explicit field date templates without changing source graduation status.',
        {'observation_id':TEXT,'education_id':TEXT,'fields':{'type':'array','items':{'type':'object','properties':{
            'field_key':TEXT,'component':{'type':'string','enum':list(COMPONENTS)}},'required':['field_key','component'],'additionalProperties':False}}}),
    tool('use_remembered_answer','Reuse a confirmed answer from an earlier application when the question means the same thing and its scope still applies.',
        {**FIELD,'memory_id':TEXT,'reason':TEXT}),
    tool("view_browser", "See the current application viewport. Start visual backup here when DOM/autofill cannot operate a control.", {"observation_id":TEXT}),
    tool("computer_click", "Click a location visible in the latest screenshot, including nested menus that have no DOM option roles. Returns a new screenshot.",
         {**SCREEN, **POINT, "click_count":{"type":"integer","enum":[1,2]}, "field_key":TEXT}),
    tool("computer_move", "Move the mouse to a screenshot location to reveal hover controls.", {**SCREEN, **POINT}),
    tool("computer_scroll", "Scroll the page or a dropdown under the pointer; distances are CSS pixels, positive down/right.",
         {**SCREEN, **POINT, "delta_x":{"type":"number"}, "delta_y":{"type":"number"}}),
    tool("computer_drag", "Drag between screenshot coordinates, for example a custom scrollbar.",
         {**SCREEN, **POINT, "end_x":COORD, "end_y":COORD, "field_key":TEXT}),
    tool("computer_key", "Use navigation/editing keys on the focused control. Enter/Space may select an open option, never submit. Returns a screenshot.",
         {**SCREEN, "key":{"type":"string","enum":["Tab","Shift+Tab","ArrowDown","ArrowUp","ArrowLeft","ArrowRight","Escape","Home","End","PageDown","PageUp","Backspace","Delete","ControlOrMeta+A","Enter","Space"]}, "field_key":TEXT}),
    tool("computer_type", "Type a confirmed source into the currently focused control; replace selects its existing text first. Never type a password here.",
         {**SCREEN, "field_key":TEXT, "source_ref":TEXT, "replace":{"type":"boolean"}}),
    tool("computer_wait", "Wait briefly for a visual transition, then read a fresh screenshot.", SCREEN),
    tool("resolve_field", "Apply a model-decided representation of a confirmed fact, semantic option mapping, or incidental default. "
         "Saves only a run-local decision, never a new candidate fact. mode=leave_empty records your decision to leave an "
         "OPTIONAL field empty when no observed option reliably represents the fact (value may be empty; give the reason); "
         "it is shown to the user immediately. Never pick a merely similar option instead.",
         {**FIELD, "mode":{"type":"string", "enum":["format", "equivalent", "default", "leave_empty"]}, "value":TEXT,
          "source_refs":{"type":"array", "items":TEXT}, "reason":TEXT}),
    tool("explore_option", "Enter an observed category to find a user's intended leaf. Read returned children/new fields; the category is not the final answer.",
         {**FIELD, "source_ref":TEXT, "option_label":TEXT, "reason":TEXT}),
    tool("click_page_action", "Open the application, move between steps, or add an experience/education section using an observed page control. Cannot submit.",
         {"observation_id": TEXT, "control_id": TEXT}),
    tool("record_answers", "Interpret explicit user answers in context and save grounded, normalized field values before autofill. "
         "For a picker with categories, record the final choice the user named (\"Adobe Source, then Adobe.com\" -> Adobe.com), "
         "not the category; reach it with explore_option.",
         {"observation_id": TEXT, "answers": {"type": "array", "items": {
             "type": "object", "properties": {"field_key": TEXT, "message_id": TEXT, "quote": TEXT, "value": TEXT,
                 "reuse": {"type": "string", "enum": ["all_applications", "this_employer", "this_application"],
                     "description": "Where this answer applies, judged from the question's meaning: a general fact (a profile URL) "
                         "applies to all applications; a company-specific one (worked here before) to this employer; one that "
                         "varies by posting (how you heard about this job) to this application only."}},
             "required": ["field_key", "message_id", "quote", "value"], "additionalProperties": False}}}),
    tool("update_plan", "Save the remaining steps and the current approach for this application.",
         {"steps": {"type": "array", "items": TEXT}, "approach": TEXT}),
    tool("autofill", "Fill the observed field_keys you choose. Scope persists until you select new fields; unrelated failures are not this operation's failures. Boolean/semantic decisions still require resolve_field. Omit field_keys only to reuse the saved scope (or initial page scope).", optional={'field_keys':{'type':'array','items':TEXT,'minItems':1,'maxItems':100}}),
    tool("observe", "Read the latest form values, missing fields and validation errors."),
    tool("say", "Send a short progress explanation or question to the user in the application chat.", {"text": TEXT}),
    tool("use_chat_answer", "Legacy literal-only answer tool; prefer record_answers for natural conversation.",
         {**FIELD, "message_id": TEXT, "answer": TEXT}),
    tool("read_field", "Read a field's current value, target, saved decision and date siblings without clicking, opening menus or typing.", FIELD),
    tool("focus_fields", "Choose fields for detailed observations; other fields remain in a page-wide index and blockers. Empty field_keys restores full detail. Does not write or decide any answers.", {"field_keys":{"type":"array","items":TEXT,"maxItems":100}}),
    tool("inspect_field", "Open a dropdown and read its actual options. Clears a previous search so parent categories can be explored when searching a leaf found nothing. Follow next_offset to read long lists.", FIELD, optional=OPTION_PAGE),
    tool("search_options", "Search an autocomplete dropdown using a supplied fact; expand known country/school aliases. Follow next_offset to read long results.", {**FIELD, "source_ref": TEXT}, optional=OPTION_PAGE),
    tool("click_field", "Click/focus an observed text control or dropdown, without changing checkbox answers.", FIELD),
    tool("type_from_source", "Type a value from an allowed source, with keyboard input events.", {**FIELD, "source_ref": TEXT}),
    tool("choose_option", "Select an observed option using this field's allowed_source_refs. If no answer is bound yet, use record_answers with the relevant original user message first.", {**FIELD, "source_ref": TEXT, "option_label": TEXT}),
    tool("press_key", "Use ArrowDown, ArrowUp, Escape or Tab on a control; Enter is unavailable.",
         {**FIELD, "key": {"type": "string", "enum": ["ArrowDown", "ArrowUp", "Escape", "Tab"]}}),
    tool("view_field", "Inspect an image of a field, only if optional visual fallback is enabled.", FIELD),
    tool("ready_for_review", "Return control to the host for independent validation and review."),
    tool("request_input", "Pause for missing answers to a nonempty list of observed fields. For a browser barrier or general question, use ask_user with the exact explanation and next step.",
         {"field_keys": {"type": "array", "items": TEXT}}),
]
TOOLS.extend(item for item in BROWSER_TOOLS if item['function']['name'] not in BROWSER_VISUAL_NAMES)
# Still executable for saved runs and scripted callers, but not offered to the model:
# a legacy duplicate of record_answers, and two browser tools that application mode
# always refuses (choices and checkboxes need grounded field tools).
HIDDEN_TOOLS = {"observe", "use_chat_answer", "browser_select", "browser_check"}
# Snapshot size: long option lists are previewed until the agent inspects the field.
OPTION_PREVIEW = 25
NARRATIVE = re.compile(r"\b(describe|explain|share|example|why|what|how|motivat\w*|summari\w*)\b|"
    r"tell us|tell me|cover letter|additional information|介绍|描述|经历|项目|简述|说明", re.I)


OTHER_PARTY = re.compile(r"\b(company|employer|organi[sz]ation|business|reference|referee|referr\w*|emergency|"
    r"manager|supervisor|recruiter|hiring|spouse|partner|parent|guardian|relative|contact person|"
    r"school|university|college|institution|job|position|title|user ?name|account)\b")


def confirms_proposal(history, quote, value):
    """A plain "yes" confirms the exact value the agent proposed in its question just before.

    Palantir pilot: "Use today's date, 10/15/2026?" -> "yes" was acknowledged but never
    recorded, because "yes" does not literally contain the date. The value must appear in
    that last assistant message; anything else still needs the user's own words.
    """
    if boolean_answer(quote) is not True or not clean(value):
        return False
    asked = next((m for m in reversed(history) if m.get("role") == "assistant"), None)
    return bool(asked) and clean(value).casefold() in clean(asked.get("text", "")).casefold()


def grounded(field, quote, value, options):
    """A recorded chat answer must be what the quote says, not merely cite a message."""
    q, v = clean(quote), clean(value)
    if not q or not v:
        return False
    if boolean_answer(value) is not None:
        # Nonliteral statements need a question-aware semantic check before
        # recording; word count says nothing about the meaning of Yes or No.
        return boolean_answer(quote) is boolean_answer(value)
    if len(q) < 2:
        return False
    if f" {v} " in f" {q} ":
        return True
    chosen = resolve_option(field["label"], value, options) if options else None
    if chosen is not None and resolve_option(field["label"], quote, options) == chosen:
        return True
    if equivalent(field["label"], quote, {"label": value, "value": value}):
        return True
    # A normalized value may expand what the user wrote ("Ithaca" -> "Ithaca,
    # New York, United States") if it keeps a distinctive word they used.
    # Interpreted answers stay pending until the user approves them at review.
    words = set(v.split())
    if any(len(word) >= 3 and word in words for word in q.split()):
        return True
    # Translation into one of the observed options ("领英" -> "LinkedIn").
    return chosen is not None and bool(re.search(r"[^\x00-\x7f]", quote))


# Reasoning counts against the cap. At 2400 the Adobe Workday experience page was cut off six
# times in a row (incomplete_response); the career agent already uses 8000.
MAX_OUTPUT_TOKENS = 8000


class ConfiguredToolModel:
    def __init__(self):
        from eregion.domain.model import shared_model
        self.model = shared_model(purpose='agent')
        self.fast_model = shared_model(purpose='fast')
        self.timeout = model_timeout(self.model)

    def invoke(self, messages, tools):
        return self.model.bind_tools(tools).invoke(messages, timeout=self.timeout, max_tokens=MAX_OUTPUT_TOKENS)

    def invoke_turn(self, messages, tools, snapshot):
        complex_turn = (snapshot.get('recovery') or snapshot.get('runtime_feedback') or
            any(f.get('kind') == 'textarea' for f in snapshot.get('fields', [])) or
            any(h.get('failed') or h.get('error_code') for h in snapshot.get('decision_history', [])))
        model = self.model if complex_turn else self.fast_model
        return model.bind_tools(tools).invoke(messages, timeout=self.timeout, max_tokens=MAX_OUTPUT_TOKENS)

    def verify_answers(self, answers):
        from .grounding import verify_boolean_answers
        return verify_boolean_answers(self.fast_model, answers, timeout=self.timeout)


class HybridApplicationAgent:
    def __init__(self, *, model_factory=ConfiguredToolModel, decisions=None):
        self.model_factory = model_factory
        # Injecting a model must not silently enable a second, real provider.
        # Production keeps configured Jev; custom models can opt in explicitly.
        if decisions is None and model_factory is not ConfiguredToolModel:
            from eregion.runtime.fast_decisions import FastDecisions
            decisions = FastDecisions()
        self.decisions = decisions

    def run(self, browser, state, *, autofill, active, reserve, report, refresh=None,
            communicate=None, accept_answer=None, checkpoint=None, bind_answers=None, publish_question=None):
        from .runner import DecisionRunner
        session = AgentTools(browser, state, autofill=autofill, active=active, refresh=refresh,
            communicate=communicate, accept_answer=accept_answer, checkpoint=checkpoint,
            bind_answers=bind_answers, publish_question=publish_question)
        return DecisionRunner(self.model_factory, decisions=self.decisions).run(session, active=active, reserve=reserve,
            report=report, refresh=refresh, communicate=communicate, checkpoint=checkpoint)


def _without_empty(entry):
    """Open fields omit empty attributes (help '', errors [], group '' ...); value, required and
    the status flags are always kept (request size review)."""
    keep = {'key', 'label', 'kind', 'value', 'required', 'mapping', 'write_verified', 'target', 'source_fact'}
    return {k: v for k, v in entry.items() if k in keep or v not in ('', [], {}, None, False)}


def unreviewed_checkboxes(fields, state):
    """An observed boolean is not a candidate fact or a saved interpretation."""
    return [{'key':f['key'], 'label':f['label'], 'value':f['value'],
             'reason':'No confirmed answer or saved model decision for this checkbox.'}
            for f in fields if f['kind']=='checkbox' and desired_answer(f,state) is None
            and active_decision(f,state) is None]


def record_status(fields, state):
    """Per saved record: bound, verified (page value equals the bound target or a saved
    leave_empty), pending, and bound checkboxes without a saved model decision. Verification
    status only; whether a record needs more fields stays the model's call."""
    from .education import binding as education_binding_of
    present = {f['key']: f for f in fields}
    result = []
    for kind, records, bind_of, id_key in (('work', state['profile'].get('experience', []), experience_binding, 'experience_id'),
                                           ('education', state['profile'].get('education', []), education_binding_of, 'education_id')):
        for record in records:
            bound, verified, pending, undecided = [], [], [], []
            for key, field in present.items():
                item = bind_of(field, state)
                if not item or item.get(id_key) != record['id']:
                    continue
                bound.append(key)
                decision = active_decision(field, state)
                if field['kind'] == 'checkbox' and decision is None:
                    undecided.append(key)
                    continue
                if decision and decision['mode'] == 'leave_empty' and not field.get('required'):
                    verified.append(key); continue
                target = desired_answer(field, state)
                (verified if target is not None and matches(field, target) and not field.get('invalid') else pending).append(key)
            result.append({'record': f"{kind}:{record['id']}",
                           'label': record.get('company') or record.get('school') or '',
                           'bound': len(bound), 'verified': verified, 'pending': pending, 'checkbox_without_decision': undecided,
                           'all_bound_fields_verified': bool(bound) and not pending and not undecided})
    return result


class AgentTools:
    def __init__(self, browser, state, *, autofill, active, refresh=None, communicate=None, accept_answer=None, checkpoint=None, bind_answers=None, publish_question=None):
        self.browser, self.state, self.autofill, self.active = browser, state, autofill, active
        self.fields, self.observation_id = [], None
        self.autofill_called = False
        self.failed_fields = set()
        self.observed_options = {}
        self.refresh, self.communicate, self.accept_answer = refresh, communicate, accept_answer
        self.checkpoint = checkpoint
        self.bind_answers = bind_answers
        self.publish_question = publish_question
        self.page_context = None
        self.visual = None
        self.visual_field_key = None
        self.browsing = None
        self.verify_answers = None
        self.verified_answers = set()
        self.fast_choices = None
        self.fast_option_attempts = set()
        self._cached_fields = None

    def observe(self):
        if self.refresh: self.state = self.refresh()
        import time
        cached = self._cached_fields
        self._cached_fields = None
        self.fields = cached[1] if cached and time.monotonic() - cached[0] < .3 else self.browser.observe()
        if hasattr(self.browser, 'page_state'):
            from .browser import ApplicationBrowser
            self.page_context = self.browser.page_state(fields=self.fields) if isinstance(self.browser, ApplicationBrowser) else self.browser.page_state()
        else:
            self.page_context = None
        if repeatable_education(self.page_context) and self.state['profile'].get('education') and not self.state.get('education_required'):
            self._save_decisions(education_required=True)
        self._reconcile_paths()
        completed_keys = [f["key"] for f in self.fields
            if desired_answer(f, self.state) is not None and matches(f, desired_answer(f, self.state))]
        self.failed_fields.difference_update(completed_keys)
        self.observation_id = str(uuid4())
        sources = {f"profile:{key}": value for key, value in self.state["profile"].items() if value and key not in {'education', 'experience'}}
        sources.update(education_facts(self.state['profile'], self.state.get('education_answer_overrides')))
        from .experience import facts as experience_facts
        sources.update(experience_facts(self.state['profile'], self.state.get('experience_answer_overrides')))
        if self.state.get('cover_letter'):sources['material:cover_letter'] = self.state['cover_letter']
        sources.update({f"answer:{key}": value for key, value in self.state["answers"].items()})
        # The resume text is only needed to draft open-ended answers.
        resume = self.state.get('resume_context', {}) or {}
        if resume.get('text') and not (self.state.get('answer_drafts') or any(
                f["kind"] == "textarea" or NARRATIVE.search(f["label"]) for f in self.fields)):
            resume = {**{k: v for k, v in resume.items() if k != 'text'},
                'text_omitted': 'Included when this page has an open-ended question.'}
        page = self.page_context
        if page and self.fields and len(page.get("text", "")) > 2000:
            page = {**page, "text": page["text"][:2000], "text_truncated": True}
        def options(field):
            values = field.get("options", [])
            limit = 300 if field["key"] in self.observed_options or len(values) <= OPTION_PREVIEW else OPTION_PREVIEW
            return values[:limit]
        def allowed_sources(field):
            allowed=[]
            for ref in sources:
                try:self.source(field,ref)
                except BrowserBlocked:continue
                allowed.append(ref)
            return allowed
        from .memory import compatible
        unresolved = [f for f in self.fields if f['key'] not in completed_keys]
        memories = [{k: item.get(k) for k in ('memory_id', 'question', 'value')}
            for item in self.state.get('remembered_answers', [])
            if any(compatible(item, f, self.state) for f in unresolved)][:10]
        if page:
            page = {**page, 'controls': [{**c, 'section': str(c.get('section', ''))[:80]}
                for c in page.get('controls', []) if not c.get('disabled') and c.get('kind') not in {'field', 'unavailable'}][:40]}
        current_keys = {f["key"] for f in self.fields}
        # Refs allowed for every open field are listed once (they were repeated per field:
        # 16 experience refs x 46 fields ~ 33k characters of the replayed request).
        allowed = {f["key"]: allowed_sources(f) for f in unresolved}
        universal = set.intersection(*(set(v) for v in allowed.values())) if len(allowed) > 1 else set()
        status, blockers = {}, []
        bound_keys = set(self.state.get('education_bindings', {})) | set(self.state.get('experience_bindings', {}))
        for f in unresolved:
            target = desired_answer(f, self.state)
            decision = active_decision(f, self.state)
            mapping = (decision['mode'] if decision else 'no_target' if target is None
                       else 'exact' if matches(f, target) else 'undecided')
            verified = (target is not None and not f.get('invalid') and matches(f, target))
            status[f["key"]] = {'target': target[:300] if isinstance(target, str) else target, 'mapping': mapping,
                                'write_verified': verified}
            work_binding = experience_binding(f, self.state)
            if work_binding:
                status[f['key']]['source_fact'] = {'ref': work_binding['source_ref'],
                    'value': experience_facts(self.state['profile'], self.state.get('experience_answer_overrides')).get(work_binding['source_ref'])}
                if target is None and not decision:
                    status[f['key']]['mapping'] = 'undecided'
                    blockers.append({'key': f['key'], 'label': f['label'], 'reason': 'bound_fact_needs_model_decision'})
            from .runner import attempt_record
            count, basis = attempt_record(self.state, f)
            if basis:
                status[f["key"]]['attempts'] = {'count': count, 'basis': basis}
            reason = ('required_empty' if f['required'] and not f['value'] else 'invalid' if f.get('invalid')
                      else 'bound_value_differs_from_fact' if f["key"] in bound_keys and target is not None and not matches(f, target)
                      else None)
            if reason:
                blockers.append({'key': f["key"], 'label': f["label"][:120], 'reason': reason,
                                 **({'selected': f['value']} if f.get('value') else {})})
        blockers += [{'key': k, 'reason': 'unresolved_option_path'} for k in self._unresolved_paths()]
        search_log = self.state.get('option_search_log', {})
        def origin(ref):
            if not ref.startswith('answer:'):
                return ref.split(':', 1)[0]
            source = self.state.get('answer_sources', {}).get(ref.removeprefix('answer:'), {})
            return {k: source[k] for k in ('type', 'interpretation', 'message_id') if source.get(k)}
        snapshot = {"observation_id": self.observation_id, "autofill_called": self.autofill_called, "autofill_scope": self.state.get("autofill_scope"),
            'pending_document_uploads':pending_documents(self.state,self.fields),
            'document_bindings':self.state.get('document_bindings',{}),
            "visual_fallback":bool(self.state.get("visual_fallback")),
            "screen":self.visual.screen if self.visual else None,
            "page": page,
            # W3: bindings/decisions only for fields on this page (others stay in saved state).
            'education_bindings':{k:b.get('source_ref') for k,b in self.state.get('education_bindings',{}).items() if k in current_keys},
            'experience_coverage':experience_coverage(self.state),
            'record_status':record_status(self.fields,self.state),
            'unreviewed_checkboxes':unreviewed_checkboxes(self.fields,self.state),
            'education_gaps':education_gaps(self.state,self.fields,self.page_context),
            'remembered_answers':memories if self.state.get('reuse_answers',True) else [],
            'resume_context':resume,
            'answer_drafts':visible_drafts(self.state, self.fields),
            "field_decisions":{k:{'value':d.get('value'),'mode':d.get('mode'),'sources':list(d.get('sources') or {})}
                for k,d in self.state.get("field_decisions", {}).items() if k in current_keys}, "option_paths":self.state.get("option_paths", {}),
            "task": {"url": self.state.get("url"), "goal": "Complete this application using confirmed candidate facts, then return for review."},
            "task_plan": self.state.get("task_plan", []), "approach": self.state.get("approach", ""),
            'decision_history':application_history(self.state.get('decision_history',[])),
            'input_request':self.state.get('input_request'), 'budget_remaining':self.state.get('budget_remaining'),
            "page_history":[h.get('heading') for h in self.state.get("page_history", [])][-6:],
            "previous_page_answers":[{"label":f["label"][:160], "value":f["value"][:300] if isinstance(f["value"],str) else f["value"]}
                for f in self.state.get("review_fields", []) if f.get("value") not in (None, "", False) and f["key"] not in current_keys],
            "last_tool_result": self.state.get("last_tool_result"), "recovery": self.state.get("recovery_note"),
            "chat_revision": self.state.get("chat_revision", 0), "completed_fields": completed_keys,
            # Point 3: what blocks Next, separately from what is selected on the page.
            "next_step_blockers": blockers,
            "any_field_source_refs": sorted(universal),
            "user_messages": [{k: v for k, v in {"id": m["id"], "role": m["role"], "text": m["text"], "field_key": m.get("field_key"),
                "bound_answers": m.get("bound_answers"), "binding_method": m.get("binding_method"),
                "question_keys": m.get("question_keys"), 'in_reply_to':m.get('in_reply_to')}.items() if v not in (None, [], {})}
                for m in self.state.get("messages", [])[-12:]],
            "facts": [{'ref': key, 'value': value[:2000] if isinstance(value, str) else value,
                'origin': origin(key)} for key, value in sources.items()],
            "fields": [{"key": f["key"], "label": f["label"][:250], "value": f["value"], "done": True} if f["key"] in completed_keys else _without_empty({"key": f["key"], "label": f["label"][:250], "kind": f["kind"],
                "allowed_source_refs":[r for r in allowed[f["key"]] if r not in universal],
                # selected (value) vs target fact vs saved semantic mapping vs verified write.
                **status[f["key"]],
                **({"search_history": search_log[f["key"]]} if f["key"] in search_log else {}),
                "required": f["required"], "invalid": f.get("invalid", False),
                "choice_control":f.get("choice_control", False), "group":f.get("group", ""),
                "search_text":f.get("search_text", ""), "section":f.get("section", ""),
                "help":f.get("help", ""), "errors":f.get("errors", []),
                "constraints":{k:v for k,v in (f.get("constraints") or {}).items() if v not in (None, "", False)},
                "value": f["value"][:2000] if isinstance(f["value"], str) else f["value"],
                "option_count": len(f.get("options", [])),
                "options_truncated": len(options(f)) < len(f.get("options", [])),
                "options": [{"label": o["label"][:200], "value": o["value"][:200], **({"expandable":True} if o.get("expandable") else {}),
                    **({"disabled":True} if o.get("disabled") else {})} for o in options(f)]}) for f in self.fields]}

        from .observation import focus_observation
        return focus_observation(snapshot, self.state, self.fields)

    def source(self, field, reference):
        if reference == 'material:cover_letter':
            if 'cover letter' not in field['label'].casefold() or not self.state.get('cover_letter'):
                raise BrowserBlocked('Use the approved cover letter only for a cover-letter control.')
            return self.state['cover_letter']
        decision = active_decision(field, self.state)
        if decision and decision["mode"] != "category" and reference in decision["sources"]:
            return decision["value"]
        if reference == f"answer:{field['key']}" and field["key"] in self.state["answers"]:
            return self.state["answers"][field["key"]]
        # A newly observed dependent choice may use its parent's confirmed
        # leaf goal. This permission is restricted to the recorded live path,
        # exact observed choices and non-sensitive fields, never general facts.
        if field['key'] not in self.state['answers'] and not protected(field):
            for path in self.state.get('option_paths',{}).values():
                goal=source_value(self.state,reference)
                if path['source_ref']==reference and field['key'] in path['field_keys'] and goal==path['goal']:
                    options=self.observed_options.get(field['key']) or field.get('options',[])
                    if goal is not None and resolve_option(field['label'],goal,options):return goal
        if reference.startswith('education:'):
            bound=education_binding(field,self.state)
            if not bound:
                raise BrowserBlocked('Bind this field to the correct education record before using that source.', code='education_binding_required')
            if bound['source_ref'] != reference:
                raise BrowserBlocked('This field is already bound. Use its component source: '+bound['source_ref'], code='education_source_mismatch')
            value=source_value(self.state,reference)
            if value is None:raise BrowserBlocked('That education detail is not in the resume; do not invent it.')
            return value
        if reference.startswith('experience:'):
            bound = experience_binding(field, self.state)
            if bound and bound['source_ref'] != reference:
                raise BrowserBlocked('Use the bound work-experience source: ' + bound['source_ref'])
            # The reference names the resume record and component the model chose for this
            # entry; the value must exist in that record.
            value = source_value(self.state, reference)
            if value is None:raise BrowserBlocked('That work-experience detail is not in the resume; do not invent it.')
            return value
        if not reference.startswith("profile:"):
            raise BrowserBlocked("Use an explicit answer for this field.", code='source_required')
        key = reference.removeprefix("profile:")
        if key not in self.state["profile"] or not self.state["profile"][key]:
            raise BrowserBlocked("That candidate fact is unavailable.")
        # Contact data is useful for unfamiliar contact labels, never a basis for
        # making up eligibility, consent, preferences or other application answers.
        label = normalized_label(field["label"])
        basic = {"country": ("country", "country/region", "country of residence"), "school": ("school", "school name", "university", "college/university", "school / university", "school or university", "college", "institution"),
                 "degree": ("degree",), "city": ("city", "location (city)", "current city")}
        if key in basic and label in basic[key]: return self.state["profile"][key]
        hints = {"first_name": ("name", "given", "prénom", "nombre"), "last_name": ("name", "family", "surname", "nom", "apellido"),
            "email": ("email", "e-mail", "mail"), "phone": ("phone", "mobile", "telephone", "tel"),
            "linkedin": ("linkedin",), "github": ("github",), "website": ("website", "portfolio", "homepage")}
        # Whole words only ("tel" is not "tell us"), and never another person's
        # or organisation's details ("Company name", "Reference email").
        if (field["kind"] in {"checkbox", "radio", "select", "combobox", "location"}
                or OTHER_PARTY.search(label)
                or not any(re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", label) for word in hints.get(key, ()))):
            raise BrowserBlocked("This question requires an explicit answer, not an inferred profile fact.", code='source_required')
        return self.state["profile"][key]

    def execute(self, name, args):
        if not self.active():
            raise BrowserBlocked("Application cancelled.")
        definition = next((tool["function"] for tool in TOOLS if tool["function"]["name"] == name), None)
        if definition is None or not isinstance(args, dict) or (not set(definition["parameters"]["required"]).issubset(args) or not set(args).issubset(definition["parameters"]["properties"])):
            raise BrowserBlocked("Unknown tool or invalid arguments.")
        if name in VISUAL_TOOLS and not self.state.get("visual_fallback"):
            raise BrowserBlocked("Visual fallback is not enabled for this attempt.")
        if self.visual and name not in VISUAL_TOOLS and name not in {"observe","say","update_plan","record_answers","ready_for_review","request_input","ask_user"}:
            self.visual.invalidate()
        if name in BROWSER_NAMES:
            from eregion.runtime.browser.session import BrowserSession
            from eregion.runtime.browser.tools import execute
            if self.browsing is None:self.browsing = BrowserSession(self.browser,application=True)
            return execute(self.browsing,name,args)
        if name == 'focus_fields':
            keys = args['field_keys']
            if not isinstance(keys, list) or len(keys)>100 or any(not isinstance(k,str) or k not in {f['key'] for f in self.fields} for k in keys):
                raise BrowserBlocked('Choose observed field keys, or an empty list for the full page.')
            self._save_decisions(observation_scope=list(dict.fromkeys(keys)),
                observation_scope_page=[(self.page_context or {}).get('url'),(self.page_context or {}).get('heading')])
            return {'observation_scope':keys, 'next':'The next observation uses this scope; page-wide blockers remain visible.'}
        if name == "record_answers":
            return self._record_answers(args)
        if name == "update_plan":
            steps, approach = args["steps"], args["approach"]
            if not isinstance(steps, list) or len(steps) > 8 or not all(isinstance(s, str) and 0 < len(s) <= 200 for s in steps) or not isinstance(approach, str) or len(approach) > 500:
                raise BrowserBlocked("Keep the plan to at most eight short steps.")
            self.state["task_plan"], self.state["approach"] = steps, approach
            if self.checkpoint: self.checkpoint(task_plan=steps, approach=approach)
            return {"saved": True}
        if name == "autofill":
            self.autofill_called = True
            from .fast_options import fill_options
            # A scope belongs to the page it was chosen on. Precheck 2026-10-16: a scope kept
            # from My Information made the host's page-entry autofill skip every later page.
            page = [(self.page_context or {}).get('url'), (self.page_context or {}).get('heading')]
            if 'field_keys' not in args and self.state.get('autofill_scope') is not None and self.state.get('autofill_scope_page') != page:
                self._save_decisions(autofill_scope=None, autofill_scope_page=None)
            keys = args.get('field_keys', self.state.get('autofill_scope'))
            if keys is not None:
                if not isinstance(keys,list) or not keys or not all(isinstance(k,str) for k in keys) or len(keys)>100:
                    raise BrowserBlocked('Choose a nonempty list of observed field keys.')
                if 'field_keys' in args and not set(keys).issubset({f['key'] for f in self.fields}):
                    raise BrowserBlocked('Observe fields before selecting the autofill scope.')
                self._save_decisions(autofill_scope=list(dict.fromkeys(keys)), autofill_scope_page=page)
                if 'field_keys' in args and not getattr(self, '_host_literal_autofill', False):
                    self._save_decisions(observation_scope=list(dict.fromkeys(keys)), observation_scope_page=page)
            result = self.autofill() or {}
            fast_filled = [] if (getattr(self, '_host_literal_autofill', False) or keys is None) else fill_options(self, field_keys=keys)
            result['filled'] = list(dict.fromkeys([*result.get('filled', []), *fast_filled]))
            for key in fast_filled:
                result.get('failed', {}).pop(key, None)
                result.get('failure_details', {}).pop(key, None)
            initial_host = getattr(self, '_host_literal_autofill', False) and keys is None
            if initial_host:
                stale = {k for k,d in result.get('failure_details', {}).items() if d.get('error_code')=='unchanged_failure'}
                # Keep durable failure memory; a skipped historical failure is not a new
                # page-entry failure, nor a request to repair unrelated fields.
                for key in stale:
                    result.get('failed', {}).pop(key, None)
                    result.get('failure_details', {}).pop(key, None)
                result['historical_failure_count'] = len(stale)
            relevant = set(keys) if keys is not None else (set(result.get('filled', [])) | set(result.get('failed', {}))
                if initial_host else {f['key'] for f in self.fields})
            self.failed_fields = (self.failed_fields - relevant) | set(result.get('failed', {}))
            result['scope'] = sorted(relevant)
            result['out_of_scope_failure_count'] = len(set(self.state.get('autofill_failures', {})) - relevant)
            result['scope_status'] = ('partial' if result.get('filled') else 'failed') if result.get('failed') else 'ok'
            result['scope_note'] = 'Only selected fields were processed; other fields are not validated by this result.'
            observed = {f['key']:f for f in self.browser.observe()}
            result['verified_fields'] = sorted(k for k in relevant if k in observed and
                desired_answer(observed[k], self.state) is not None and matches(observed[k], desired_answer(observed[k], self.state)))
            result['unreviewed_checkboxes'] = unreviewed_checkboxes(list(observed.values()), self.state)
            result['completion_level'] = 'selected_fields_only' if keys is not None else 'literal_writes_only'
            result['unresolved_fields'] = sorted(relevant - set(result['verified_fields']) - set(result.get('filled', [])))
            if result['scope_status'] == 'ok' and result['unresolved_fields']:
                result['scope_status'] = 'needs_decision'
            if initial_host:
                result['scope_note'] = 'Automatic literal writes only. Historical failures were not retried. Select field_keys for further work; this is not page or record completion.'
            if not result.get('filled') and not result.get('failed'):
                result = {**result, 'unchanged':True,
                    'next':'Select the fields for your current task. Resolve unreviewed checkboxes with cited facts (or ask a focused question). A selected-field success is not record completion; do not repeat unchanged autofill.'}
            return result
        if name == "observe":
            return {"refreshed": True, "next": "Use the observation in this turn."}
        if name == "say":
            if not isinstance(args["text"], str) or not 0 < len(args["text"]) <= 1500:
                raise BrowserBlocked("Keep chat messages short.")
            if self.communicate: self.communicate(args["text"])
            return {"sent": bool(self.communicate)}
        if name == "request_input":
            keys = args["field_keys"]
            if keys == []:
                raise BrowserBlocked("There are no fields to ask about. Use ask_user with a specific explanation and next step, or continue working.", code='invalid_action')
            if not isinstance(keys, list) or not all(isinstance(key, str) and key in {f["key"] for f in self.fields} for key in keys):
                raise BrowserBlocked("Only request answers for observed fields.")
            if any(f["key"] in keys and f["value"] and not f.get("invalid") and f["key"] not in self.failed_fields
                and not (is_consent(f) and f["key"] not in self.state["answers"]) for f in self.fields):
                raise BrowserBlocked("Keep completed fields; request only missing or failed answers.")
            return {"field_keys": keys}
        if name=='ask_user':
            text,keys=args['text'],args['field_keys']
            if not isinstance(text,str) or not 0<len(text.strip())<=4000:
                raise BrowserBlocked('Ask a concise, nonempty question.')
            if not isinstance(keys,list) or len(keys)>30 or not all(isinstance(k,str) and k in {f['key'] for f in self.fields} for k in keys):
                raise BrowserBlocked('Associate observed fields, or use an empty field list for a general question.')
            waiting = args.get('wait_for_reply', True)
            if not isinstance(waiting, bool):raise BrowserBlocked('wait_for_reply must be a boolean.')
            if not keys:
                # A text question that names empty fields becomes an answer batch for them, so
                # the user gets answer cards with their drafts (Palantir pilot: a text-only list
                # hid both). A question naming no field stays a general question.
                asked = ' '.join(text.casefold().split())
                keys = [f['key'] for f in self.fields if not f.get('value') and f.get('kind') not in {'file', 'password'}
                        and len(name := ' '.join(str(f.get('label') or '').casefold().replace('✱', '').replace('*', '').split())[:40]) >= 8
                        and name in asked][:30]
            if self.publish_question:
                request = self.publish_question(text.strip(), list(dict.fromkeys(keys)), self.fields,
                    self.state.get('chat_revision',0))
                self.state['input_request'] = request
            else:
                request = self.state.get('input_request') or {'id':str(uuid4()),'text':text.strip(),
                    'field_keys':list(dict.fromkeys(keys))}
                self.state['input_request'] = request
                if self.checkpoint:self.checkpoint(input_request=request)
            return {'question':request['text'],'field_keys':request['field_keys'],
                'request_id':request['id'],'waiting_for_user':waiting}
        if name == 'wait_for_user':
            request = self.state.get('input_request')
            if not request:raise BrowserBlocked('There is no unanswered question. Read the latest replies and continue, or ask_user a new question.')
            return {'question':request['text'],'field_keys':request['field_keys'],
                'request_id':request['id'],'waiting_for_user':True}
        if name == "ready_for_review":
            consents = [f for f in self.fields if is_consent(f) and self.state.get('consent_policy', 'ask') == 'ask'
                        and f['key'] not in self.state['answers']]
            if consents:
                raise BrowserBlocked('Ask the user with ask_user (field_keys set) whether to agree to: '
                    + '; '.join(f"{f['key']} ({f['label'][:80]})" for f in consents), code='input_pending')
            if pending_documents(self.state,self.fields):
                raise BrowserBlocked('Upload the selected documents before review. Bind an unfamiliar file control with bind_document, then use autofill.', code='document_pending')
            if self.state.get("input_request"):
                raise BrowserBlocked("Wait for the pending user reply before final review.", code='input_pending')
            # Drafts for optional fields are offered at review; only required ones hold it.
            required = {f['key'] for f in self.fields if f.get('required')}
            if required & set(visible_drafts(self.state, self.fields)):
                raise BrowserBlocked("Show the editable drafts with request_input before final review.")
            if self.page_context is not None and not self.browser.page_state()["can_submit"]:
                raise BrowserBlocked("This is not the final application step. Continue using the observed page controls.")
            invalid = [f["key"] for f in self.fields if f.get("invalid") or (f["required"] and not f["value"])]
            if invalid or self._unresolved_paths():
                raise BrowserBlocked("Repair the website's validation errors and complete the option path before review: " + "; ".join(invalid + self._unresolved_paths()))
            gaps=education_gaps(self.state,self.fields,self.page_context,final=True) + experience_gaps(self.state,self.fields)
            if gaps:raise BrowserBlocked('Complete supplied education details before review: '+'; '.join(gaps))
            return {"host_validation_required": True}
        if args["observation_id"] != self.observation_id:
            raise BrowserBlocked("Stale observation. Observe the form again.", code='stale_observation')
        if name=='bind_experience':return self._bind_experience(args)
        if name=='bind_education':return self._bind_education(args)
        if name == "view_browser": return self._view_browser()
        if name.startswith("computer_"): return self._computer(name,args)
        if name == "click_page_action":
            if self.page_context is None:
                raise BrowserBlocked("Page navigation is unavailable for this browser.")
            control = next((c for c in self.page_context["controls"] if c["id"] == args["control_id"]), None)
            if control is None or control["kind"] in {"submit","guarded","field","unavailable"} or control["disabled"]:
                raise BrowserBlocked("Choose an ordinary observed page control. Use field tools for choices; guarded or final-send controls require browser help/review.")
            if control["kind"] == "next":
                missing = [f["key"] for f in self.fields if (f["required"] and not f["value"]) or
                    (is_consent(f) and self.state.get("consent_policy", "ask") == "ask" and f["key"] not in self.state["answers"])]
                missing += self._unresolved_paths()
                missing += [k for k in visible_drafts(self.state, self.fields) if any(f['key'] == k and f.get('required') for f in self.fields)]
                missing += education_gaps(self.state,self.fields,self.page_context) + experience_gaps(self.state,self.fields)
                if missing:
                    raise BrowserBlocked("Complete this step's required answers and consent before continuing: " + "; ".join(missing))
            history = [*self.state.get("page_history", []), {"url":self.page_context["url"], "heading":self.page_context["heading"]}][-20:]
            review = {f["key"]:f for f in self.state.get("review_fields", [])}
            review.update({f["key"]:f for f in self.fields})
            self.state.update(page_history=history, review_fields=list(review.values()))
            if self.checkpoint: self.checkpoint(page_history=history, review_fields=list(review.values()))
            self.browser.click_page_action(control)
            self.visual_field_key = None
            self.autofill_called = False
            self.observed_options.clear()
            self.failed_fields.clear()
            return {"navigated": True, "label":control["label"], "next":"Observe the new page before choosing another action."}
        field = next((f for f in self.fields if f["key"] == args["field_key"]), None)
        if field is None:
            raise BrowserBlocked("The field is not in the latest observation.", code="stale_observation")
        key = field["key"]
        if name == 'read_field':
            from .browser import date_segment
            siblings = []
            if date_segment(field):
                prefix = re.split(r'dateSection(?:Month|Day|Year)', field.get('dom_id') or key)[0]
                siblings = [{'key':f['key'],'value':f['value'],'invalid':f.get('invalid',False)}
                    for f in self.fields if (f.get('dom_id') or f['key']).startswith(prefix) and date_segment(f)]
            return {'field_key':key,'value':field['value'],'kind':field['kind'],
                    'target':desired_answer(field,self.state),'decision':active_decision(field,self.state),
                    'errors':field.get('errors',[]),'date_group':siblings,'read_only':True}
        if name=='bind_document':
            if field['kind']!='file' or args['document'] not in {'resume','cover_letter'}:
                raise BrowserBlocked('Choose an observed file upload and an available document.')
            from .autofill import other_document
            if other_document(field):
                raise BrowserBlocked('This upload asks for a different document than the resume or cover letter. '
                    'Ask the user to upload it in the browser; never substitute another file.', code='input_pending')
            if args['document']=='cover_letter' and not self.state.get('cover_letter'):
                raise BrowserBlocked('No approved cover letter is available.')
            self._save_decisions(document_bindings={**self.state.get('document_bindings',{}),key:args['document']})
            return {'bound':True,'next':'Call autofill to upload this document.'}
        if name == "draft_answer":
            draft = prepare_draft(field, self.state, text=args['text'], evidence=args['evidence'],
                missing_details=args['missing_details'])
            self._save_decisions(answer_drafts={**self.state.get('answer_drafts', {}), key:draft})
            return {'field_key':key, 'saved':True,
                'next':'Finish independent fields, then request_input to show this editable draft. It is not a confirmed answer.'}
        if name=='use_remembered_answer':return self._recall_answer(field,args)
        if name == "resolve_field": return self._resolve_field(field, args)
        if name == "explore_option": return self._explore_option(field, args)
        if name in {"inspect_field", "search_options", "click_field", "press_key"} and key not in self._unresolved_paths():
            desired = desired_answer(field, self.state)
            if desired is not None and matches(field, desired):
                return {"field_key": key, "already_filled": True, "value": field["value"],
                    "options": field.get("options", []), "next": "Continue with other fields or finish review."}
        if name == "use_chat_answer":
            message = next((m for m in self.state.get("messages", []) if m["id"] == args["message_id"] and m["role"] == "user"), None)
            answer = args["answer"]
            if not message or not isinstance(answer, str) or not answer.strip() or answer.casefold() not in message["text"].casefold():
                raise BrowserBlocked("Quote an answer actually supplied in a user message.")
            if message.get("field_key") and message["field_key"] != key:
                raise BrowserBlocked("That message answers a different field.")
            if "bound_answers" in message and (message.get('binding_method')!='model' or key in message['bound_answers']):
                bound = message["bound_answers"].get(key)
                candidate = boolean_answer(answer) if field["kind"] == "checkbox" else answer
                if bound is None or str(candidate).casefold() != str(bound).casefold():
                    raise BrowserBlocked("That ordered reply does not answer this field. Ask only for its missing answer.")
            if field["kind"] == "checkbox":
                answer = boolean_answer(answer)
                if answer is None:
                    raise BrowserBlocked("Ask for a clear yes/no answer to this checkbox.")
            if self.bind_answers:
                self._record_answers({"observation_id": self.observation_id, "answers": [{
                    "field_key": key, "message_id": message["id"],
                    "quote": args["answer"], "value": str(answer)}]})
            else:
                if self.accept_answer: self.accept_answer(key, answer, message["id"])
                self.state["answers"][key] = answer
            if not matches(field, answer):
                self.browser.fill(field, answer)
            return {"answer_source": message["id"], "field_key": key, "already_filled": matches(field, answer)}
        if name == "inspect_field":
            options = self.browser.inspect_options(key)
            self.observed_options[key] = options
            self._log_search(key, None, options, args)
            if not options and self.state.get("visual_fallback"):
                # The menu may render rows the DOM does not expose as options.
                # Report a DOM failure so the runner attaches a screenshot.
                return {**failure("dom_interaction_failed", "No options could be read from this control's DOM."),
                    "field_key": key, "options": []}
            return self._option_page(options, args)
        if name == "search_options":
            query = search_text(field["label"], self.source(field, args["source_ref"]))
            options = self.browser.inspect_options(key, query=query)
            self.observed_options[key] = options
            self._log_search(key, query, options, args)
            current = self.browser.field(key)
            if current.get("value_committed") and matches(current, query):
                return {"field_key":key, "already_filled":True, "value":current["value"],
                    "next":"The picker committed this value; continue with other fields."}
            page = self._option_page(options, args)
            if not resolve_option(field["label"], query, options):
                # Replay: the model searched and said it would "keep looking" without saving a
                # decision. Name the decisions that resolve the field; the choice stays the model's.
                page["unresolved"] = ("No candidate here is the fact itself. Resolve the field with a saved decision: "
                    "resolve_field mode=equivalent if an observed option represents the fact (say why), a different "
                    "grounded search, ask_user with a specific question, or resolve_field mode=leave_empty with your "
                    "reason for an optional field.")
            return page
        if name == "click_field":
            self.browser.click_field(key)
        elif name == "type_from_source":
            part = args["source_ref"].rsplit(":", 1)[-1].rsplit("_", 1)[-1]
            answer = format_date(field, self.source(field, args["source_ref"]), part)
            if answer is None:
                raise BrowserBlocked(f"The cited value does not give this control's {date_format(field)} format. Cite the "
                    "record's full date or its matching month/year component; ask the user only if the resume lacks it.")
            if matches(field, answer):
                return {"field_key": key, "already_filled": True, "value": field["value"]}
            self.browser.type_field(key, answer)
        elif name == "choose_option":
            answer = self.source(field, args["source_ref"])
            if matches(field, answer) and matches(field, args["option_label"]):
                return {"field_key": key, "already_filled": True, "value": field["value"]}
            # Native select/radio choices are already present in this fresh
            # observation; asking for another inspect call adds no evidence.
            choices = self.observed_options.get(key) or field.get("options", [])
            if not any(o["label"] == args["option_label"] and not o.get("disabled") for o in choices):
                raise BrowserBlocked("Inspect this field and choose one of its observed options.", code="options_changed")
            option = next(o for o in choices if o["label"] == args["option_label"])
            resolved = resolve_option(field["label"], answer, choices)
            if resolved is None or resolved != option:
                raise BrowserBlocked("The answer must identify one unambiguous equivalent option.", code="option_mapping_required")
            self.browser.choose_option(key, args["option_label"])
        elif name == "press_key":
            if (field["kind"] in {"checkbox", "radio", "select", "combobox", "location"} or field.get("choice_control")) and args["key"] != "Escape":
                raise BrowserBlocked("Use choose_option with an explicit answer to change a choice control.")
            self.browser.press_key(key, args["key"])
        elif name == "view_field":
            if not self.state.get("visual_fallback"):
                raise BrowserBlocked("Visual fallback is not enabled for this attempt.")
            screenshot = self.browser.screenshot_field(key)
            if len(screenshot) > 2_000_000:
                raise BrowserBlocked("The field image is too large.")
            return {"image": screenshot, "field_key": key}
        return {"action": name, "field_key": key, "verify_on_next_observation": True}

    @staticmethod
    def _option_page(options, args):
        offset = args.get("offset", 0)
        if type(offset) is not int or offset < 0 or offset > len(options):
            raise BrowserBlocked("Use an option offset within the observed list.")
        end = min(offset + 300, len(options))
        return {"options": options[offset:end], "total_options": len(options),
                "next_offset": end if end < len(options) else None}

    def _log_search(self, key, query, options, args):
        """Searched terms, candidates seen and whether more pages remain, per field (shown in the
        observation so the model sees what has already been tried)."""
        log = dict(self.state.get("option_search_log", {}))
        entry = dict(log.get(key) or {"queries": [], "seen": []})
        label = "(browse)" if query is None else str(query)
        if label not in entry["queries"]:
            entry["queries"] = [*entry["queries"], label][-12:]
        entry["seen"] = list(dict.fromkeys([*entry["seen"], *(o.get("label") for o in options)]))[:80]
        offset = args.get("offset", 0) if isinstance(args.get("offset"), int) else 0
        entry["more_pages"] = offset + 300 < len(options)
        log[key] = entry
        self._save_decisions(option_search_log=log)

    def _bind_experience(self, args):
        wanted = str(args['experience_id']).removeprefix('experience:')
        records = self.state['profile'].get('experience', [])
        if not any(r['id'] == wanted for r in records):
            raise BrowserBlocked('Choose a saved experience id: ' + ', '.join(r['id'] for r in records))
        items = args['fields']
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise BrowserBlocked('Bind 1 to 20 observed fields.')
        bindings = dict(self.state.get('experience_bindings', {})); seen = set()
        for item in items:
            if not isinstance(item, dict) or set(item) != {'field_key', 'component'} or item['component'] not in BINDING_COMPONENTS:
                raise BrowserBlocked('Choose a supported experience component.')
            field = next((f for f in self.fields if f['key'] == item['field_key']), None)
            if not field or field['key'] in seen or field['kind'] in {'password', 'file'}:
                raise BrowserBlocked('Use each observed work-experience field once.')
            old = experience_binding(field, self.state)
            if old and old['experience_id'] != wanted:
                raise BrowserBlocked('This field belongs to another work record. Locate or add the correct row.')
            ref = f"experience:{wanted}:{item['component']}"
            if ref not in experience_facts(self.state['profile'], self.state.get('experience_answer_overrides')):
                raise BrowserBlocked(f"No saved fact for {ref} (field {field['key']}); nothing in this batch was bound. "
                    'Bind only available_components listed in experience_coverage. For a semantic checkbox, '
                    'use resolve_field with existing source_refs and your reason, or ask a focused question; '
                    'do not invent a missing component.')
            seen.add(field['key'])
            bindings[field['key']] = {'experience_id': wanted, 'component': item['component'],
                                     'source_ref': ref, 'field': signature(field)}
        self._save_decisions(experience_bindings=bindings)
        return {'bound': sorted(seen), 'experience_id': wanted,
                'next': 'Autofill literal facts. Resolve boolean/semantic mappings yourself, verify values, and check remaining records.'}

    def _bind_education(self,args):
        # W6: the model sent the fact-reference form "education:<id>"; accept it as the id.
        wanted=str(args['education_id']).removeprefix('education:')
        records=self.state['profile'].get('education',[])
        record=next((r for r in records if r['id']==wanted),None)
        if not record or not isinstance(args['fields'],list) or not 1<=len(args['fields'])<=20:
            raise BrowserBlocked('Choose a saved education record and its observed row fields. Record ids: '
                                 + ', '.join(r['id'] for r in records))
        bindings=dict(self.state.get('education_bindings',{}));seen=set()
        from .education import combined_bindings
        for item in combined_bindings(args['fields']):
            if not isinstance(item,dict) or set(item)!={'field_key','component'} or item['component'] not in COMPONENTS:
                raise BrowserBlocked('Map each field to a supported education component.')
            field=next((f for f in self.fields if f['key']==item['field_key']),None)
            if not field or field['key'] in seen or field['kind'] in {'password','file','checkbox'}:
                raise BrowserBlocked('Use each visible education field once.')
            old=education_binding(field,self.state)
            if old and old['education_id']!=record['id']:
                raise BrowserBlocked('This row belongs to another education. Add or locate the correct row.')
            seen.add(field['key']);bindings[field['key']]={'education_id':record['id'],'component':item['component'],
                'source_ref':f"education:{record['id']}:{item['component']}",
                'field':{k:field.get(k) for k in ('key','dom_id','label','group')}}
        self._save_decisions(education_bindings=bindings)
        return {'bound':list(seen),'education_id':record['id'],'next':'Fill all available facts for this record, then add/map other education records.'}

    def _recall_answer(self,field,args):
        def allowed_sources(field):
            allowed=[]
            for ref in sources:
                try:self.source(field,ref)
                except BrowserBlocked:continue
                allowed.append(ref)
            return allowed
        from .memory import compatible
        memory=next((m for m in self.state.get('remembered_answers',[]) if m['memory_id']==args['memory_id']),None)
        if not self.state.get('reuse_answers',True) or not memory or not isinstance(args['reason'],str) or not 0<len(args['reason'])<=1000:
            raise BrowserBlocked('Choose an available confirmed memory and explain the matching question.')
        if field['key'] in self.state['answers'] and self.state.get('answer_sources',{}).get(field['key'],{}).get('type')!='memory':
            raise BrowserBlocked('Preserve the explicit answer for this application.')
        if not compatible(memory,field,self.state):raise BrowserBlocked('The remembered question has a different consent, country, time or employer scope.')
        value=memory['value']
        if field['kind']=='checkbox':
            value=boolean_answer(value)
            if value is None:raise BrowserBlocked('This checkbox requires a confirmed yes/no answer.')
        self._save_decisions(answers={**self.state['answers'],field['key']:value},
            answer_sources={**self.state.get('answer_sources',{}),field['key']:{'type':'memory','memory_id':memory['memory_id'],
                'question':memory['question'],'reason':args['reason']}})
        return {'field_key':field['key'],'value':value,'source':'memory','next':'Fill or map this confirmed answer to the current options.'}

    def _view_browser(self):
        if self.visual is None:
            from .visual import VisualBrowser
            self.visual = VisualBrowser(self.browser)
        return self.visual.capture()

    def _computer(self, name, args):
        import math
        if self.visual is None:
            raise BrowserBlocked("Call view_browser before using screenshot coordinates.")
        # Waiting does not act on a coordinate. A transition is precisely why
        # the previous screenshot may no longer match the current page.
        if name!='computer_wait':self.visual.validate(args["screenshot_id"])
        field = next((f for f in self.fields if f["key"] == args.get("field_key")), None)
        if args.get("field_key") and field is None:
            raise BrowserBlocked("Use a current field_key, or an empty key for a general page control.")
        target = None
        if "x" in args:
            target = self.visual.target(args["x"],args["y"])
        if name in {"computer_key","computer_type"}:
            target = self.visual.focused()
        if name == "computer_scroll":
            if any(isinstance(args[k],bool) or not isinstance(args[k],(int,float)) or not math.isfinite(args[k]) or abs(args[k]) > 2000 for k in ("delta_x","delta_y")):
                raise BrowserBlocked("Scroll at most 2000 CSS pixels per action, then inspect the result.")
        if name == "computer_click" and (type(args["click_count"]) is not int or args["click_count"] not in {1,2}):
            raise BrowserBlocked("Choose one click or a double click.")
        if name == "computer_drag":
            end = self.visual.target(args["end_x"],args["end_y"])
            self.visual.guard(end)
            if end["type"] in {"checkbox","radio"} or end["role"] in {"checkbox","radio"} or end.get("action_id"):
                raise BrowserBlocked("Use a single click for form choices and page navigation.")
        changing = name in {"computer_click","computer_drag","computer_key","computer_type"}
        if target and changing:
            self.visual.guard(target)
            actual = next((f for f in self.fields if f.get("id") == target.get("field_id") or
                any(o.get("id") == target.get("field_id") for o in f.get("options",[]) if o.get("id"))),None) if target.get("field_id") else None
            if actual is not None:
                if field and actual["key"] != field["key"]:
                    raise BrowserBlocked("The coordinates/focus point to a different field. Inspect the screenshot again.")
                field = actual
            elif field is None and not target.get("action_id"):
                field = next((f for f in self.fields if f["key"] == self.visual_field_key),None)
            if field: self.visual_field_key = field["key"]
            if name == "computer_drag" and target.get("action_id"):
                raise BrowserBlocked("Use a single click for page navigation.")
            if field and protected(field) and desired_answer(field,self.state) is None:
                # Opening a choice is safe; selecting or editing it requires the
                # same confirmed fact as autofill, including bound education
                # and validated run-local decisions.
                opening = name == "computer_click" and actual is not None and field["kind"] not in {"checkbox","radio"}
                moving_focus = name == "computer_key" and args["key"] in {"Tab","Shift+Tab","Escape"}
                if not opening and not moving_focus:
                    raise BrowserBlocked("This material question needs an explicit answer before selecting a value.", code='source_required')
            if target["type"] in {"checkbox","radio"} or target["role"] in {"checkbox","radio"}:
                if name == "computer_key" and args["key"] in {"Tab","Shift+Tab","Escape"}:
                    pass
                elif name in {"computer_click","computer_key"} and field:
                    answer = desired_answer(field,self.state)
                    if answer is None: raise BrowserBlocked("Confirm this choice in chat first.")
                    # A visual coordinate must not toggle a consent already set
                    # correctly, nor change to a different radio than the answer.
                    self.visual.invalidate()
                    self.browser.fill(field,answer)
                    return {**self._view_browser(), "field_key":field["key"], "action":name}
                else:
                    raise BrowserBlocked("Use an observed field and its confirmed answer for this choice.")
            control = next((c for c in (self.page_context or {}).get("controls",[]) if c["id"] == target.get("action_id")),None)
            if control and control["kind"] != "field" and name in {"computer_click","computer_key"}:
                if name == "computer_key" and args["key"] not in {"Enter","Space"}:
                    pass
                else:
                    outcome = self.execute("click_page_action",{"observation_id":self.observation_id,"control_id":control["id"]})
                    return {**outcome, **self._view_browser(), "action":name}
        text = None
        if name == "computer_key":
            allowed = next(t["function"]["parameters"]["properties"]["key"]["enum"] for t in TOOLS if t["function"]["name"] == name)
            if args["key"] not in allowed: raise BrowserBlocked("That key is unavailable to the application agent.")
            if args["key"] in {"Enter","Space"} and not (target["choice"] or target["type"] == "select-one" or target["expanded"]):
                raise BrowserBlocked("Enter/Space can only select an open choice. Use a visible page control for navigation.")
            if args["key"] in {"Backspace","Delete","ControlOrMeta+A"} and not target["editable"]:
                raise BrowserBlocked("Focus an editable application control first.")
        if name == "computer_type":
            if type(args["replace"]) is not bool or not target["editable"]:
                raise BrowserBlocked("Focus an editable application control before typing.")
            text = self.source(field,args["source_ref"]) if field else source_value(self.state,args["source_ref"])
            if text is None or len(str(text)) > 10000:
                raise BrowserBlocked("Use a confirmed candidate source for typing.")
            if field and (field.get("choice_control") or field["kind"] in {"combobox","location"}):
                self.browser._choice_queries[field["key"]] = str(text)
        result = self.visual.perform(name,args,text=text)
        # A visual leaf selection can retain the search text while its visible
        # option includes country/domain details. Verify that the picker closed
        # after the click before treating that query as a selected value.
        if field and name == "computer_click" and target and not target["editable"]:
            query = self.browser._choice_queries.get(field["key"])
            if query:
                locator = self.browser.locate(field)
                closed_choice = (target.get("choice") and locator.count() == 1
                    and locator.get_attribute("aria-expanded") == "false"
                    and locator.evaluate("el => !!el.value"))
                if closed_choice or normalized_label(target["label"]) == normalized_label(query):
                    self.browser._choice_queries.pop(field["key"],None)
        self.observed_options.clear()
        self._reconcile_paths()
        return {**result,"action":name,"field_key":field["key"] if field else None,
            "target":{"label":target["label"],"tag":target["tag"]} if target else None,
            "verify_on_next_observation":True}

    def _save_decisions(self, **changes):
        self.state.update(changes)
        if self.checkpoint: self.checkpoint(**changes)

    def _decision(self, field, value, mode, sources, reason):
        decision = {"field":signature(field), "value":value, "mode":mode, "sources":sources,
            "original_answer":candidate_answer(field, self.state["profile"], self.state["answers"]), "reason":reason}
        self._save_decisions(field_decisions={**self.state.get("field_decisions", {}), field["key"]:decision})

    def _require_confirmed_sources(self, field, sources):
        """Material answers may be represented, but not supplied by a default.

        Validate the source's relationship to this question using the same
        permissions as typing/choosing, without requiring identical option text.
        An explicit correction takes precedence over the original profile.
        """
        if field["kind"] in {"password","file"} or not sources:
            raise BrowserBlocked("Cite the confirmed fact for this question; an automatic default cannot supply it.")
        if field["key"] in self.state["answers"] and set(sources) != {f"answer:{field['key']}"}:
            raise BrowserBlocked("Use this field's explicit answer; it takes precedence over profile facts.", code='source_required')
        bound = experience_binding(field, self.state)
        for ref in sources:
            # Same-record work facts are evidence for a decision (checked in _resolve_field),
            # not the literal value; education and other sources keep the fill-source check.
            if bound and ref.startswith(f"experience:{bound['experience_id']}:"):
                continue
            self.source(field,ref)

    def _resolve_field(self, field, args):
        value, mode, refs, reason = (args[k] for k in ("value", "mode", "source_refs", "reason"))
        if mode == "leave_empty":
            # The model's explicit decision for an optional field; the attempt limit only stops
            # repetition and never decides this (W6 review).
            if field.get("required"):
                raise BrowserBlocked("Only an optional field can be left empty; ask the user for a required one.")
            if not isinstance(reason,str) or not 0 < len(reason) <= 1000:
                raise BrowserBlocked("Explain why no observed option represents the fact.")
            decisions = {**self.state.get("field_decisions", {}), field["key"]: {"field": signature(field), "value": None,
                "mode": "leave_empty", "sources": {}, "original_answer": candidate_answer(field, self.state["profile"], self.state["answers"]),
                "reason": reason}}
            self._save_decisions(field_decisions=decisions)
            return {"field_key": field["key"], "left_empty": True, "reason": reason,
                    "next": "Recorded and shown to the user. Continue with the other fields or the next step."}
        if mode not in {"format", "equivalent", "default"} or not isinstance(value,str) or not 0 < len(value) <= 2000 or not isinstance(reason,str) or not 0 < len(reason) <= 1000:
            raise BrowserBlocked("Give a bounded value and explain its relationship to the source and page.")
        if not isinstance(refs,list) or len(refs)>6 or not all(isinstance(r,str) for r in refs):
            raise BrowserBlocked("Use existing profile/answer source references.")
        sources = {ref:source_value(self.state,ref) for ref in refs}
        if any(v is None or v == "" for v in sources.values()):
            raise BrowserBlocked("A referenced fact is unavailable.", code='source_required')
        bound = experience_binding(field, self.state)
        if bound:
            # The binding names what a literal write copies (source()); a semantic decision may
            # cite any existing fact of that same record as evidence (experience-page test:
            # "current" + "end_date" for Cornell was refused). Other records stay refused.
            record = f"experience:{bound['experience_id']}:"
            foreign = [ref for ref in refs if ref.startswith('experience:') and not ref.startswith(record)]
            if foreign:
                raise BrowserBlocked(f"This field's bound work-experience record is {bound['experience_id']}; cite only that "
                                     f"record's facts (not {', '.join(foreign)}).", code='experience_source_mismatch')
        material = protected(field)
        if material:
            if mode == "default":
                raise BrowserBlocked("A default cannot supply a material fact or consent. Cite its confirmed source for an equivalent representation.")
            self._require_confirmed_sources(field,sources)
        options = self.observed_options.get(field["key"]) or field.get("options", [])
        choice = field["kind"] in {"select","radio","combobox","location"} or field.get("choice_control") or bool(options)
        if choice and isinstance(field.get("value"), str) and field["value"].strip() and not field.get("invalid"):
            # The control's committed selection is an observed option too (replay: Workday committed
            # "Computer and Information Science", outside the visible page of a long list).
            options = [*options, {"label": field["value"], "value": field["value"]}]
        if choice:
            option = resolve_option(field["label"], value, options)
            if option is None: raise BrowserBlocked("Inspect this control and select one unambiguous observed option.")
            value = option["label"]
        if material and mode == "equivalent":
            polarity = boolean_answer(value)
            if polarity is not None and any(boolean_answer(v) is not None and boolean_answer(v) is not polarity for v in sources.values()):
                raise BrowserBlocked("An equivalent option must preserve the confirmed answer's yes/no meaning.")
        if mode == "default":
            if not choice or field["key"] in self.state["answers"] or any(boolean_answer(o["label"]) is not None for o in options):
                raise BrowserBlocked("Defaults are only for incidental observed categories without a confirmed answer; never yes/no decisions.")
            if field["value"] and not field.get("invalid"):
                raise BrowserBlocked("Keep the existing valid choice unless the user changes it.")
        elif not sources:
            raise BrowserBlocked("Cite the confirmed fact you are adapting.")
        elif mode == "format" and not (preserves_format(value, sources) or
                any(format_date(field, v, ref.rsplit(":", 1)[-1].rsplit("_", 1)[-1])==value
                    for ref, v in sources.items())):
            raise BrowserBlocked("Preserve the source data when formatting or splitting it; do not invent digits or facts.")
        # The model can represent the same source in a text control too (for
        # example an explicitly stated date in the site's requested format).
        # Material fields still require applicable confirmed sources above.
        if field["kind"] == "checkbox":
            # W6 (Adobe "I currently work here"): the model decided "true" from the resume's
            # "Present", but the string reached a writer that takes only a boolean.
            checked = boolean_answer(value)
            if checked is None:
                raise BrowserBlocked("A checkbox takes a yes/no value.")
            value = checked
        self._decision(field, value, mode, sources, reason)
        if choice:
            if not matches(field,value): self.browser.choose_option(field["key"],value)
        elif not matches(field,value): self.browser.fill(field,value)
        current = next((f for f in self.browser.observe() if f["key"]==field["key"]),None)
        return {"field_key":field["key"], "value":value, "verified":bool(current and matches(current,value)),
            "errors":current.get("errors",[]) if current else [], "decision":mode,
            "next":"Read the updated page; repair remaining errors or continue to the next step."}

    def _explore_option(self, field, args):
        goal = source_value(self.state,args["source_ref"])
        if goal is None or not isinstance(args["reason"],str) or not 0 < len(args["reason"]) <= 1000:
            raise BrowserBlocked("Explore categories only for an available confirmed answer.")
        if protected(field): self._require_confirmed_sources(field,{args["source_ref"]:goal})
        # W6 (Adobe): after a category opened, observed_options held only its leaves, so the
        # category itself could not be explored again. Any option seen for this field counts.
        options = [*(self.observed_options.get(field["key"]) or []), *field.get("options",[])]
        if not any(o["label"]==args["option_label"] and not o.get("disabled") for o in options):
            raise BrowserBlocked("Explore an option actually observed for this field.")
        old_keys = {f["key"] for f in self.fields}
        children = self.browser.explore_option(field["key"],args["option_label"])
        latest = self.browser.observe()
        self.observed_options[field["key"]] = children
        previous = self.state.get("option_paths",{}).get(field["key"],{})
        path = {"source_ref":args["source_ref"], "goal":goal, "field_keys":list(dict.fromkeys([
            field["key"], *previous.get("field_keys",[]), *(f["key"] for f in latest if f["key"] not in old_keys)])),
            "labels":[*previous.get("labels",[]),args["option_label"]][-8:], "complete":False}
        self._save_decisions(option_paths={**self.state.get("option_paths",{}), field["key"]:path})
        current = next((f for f in latest if f["key"]==field["key"]),None)
        if current and current["value"] and not matches(current,goal):
            self._decision(field,current["value"],"category",{args["source_ref"]:goal},args["reason"])
        return {"options":children, "field_keys":path["field_keys"], "goal":goal,
            "next":"Choose the intended leaf or inspect the new dependent field using this path source_ref. This category is not yet a completed answer. If it exposed no matching leaf or child, try a different observed category before advancing."}

    def _reconcile_paths(self):
        paths = {k:dict(v) for k,v in self.state.get("option_paths",{}).items()}
        decisions = dict(self.state.get("field_decisions",{}))
        changed = False
        for key,path in list(paths.items()):
            if source_value(self.state,path["source_ref"]) != path["goal"]:
                del paths[key]
                if decisions.get(key,{}).get("mode")=="category": decisions.pop(key)
                changed=True;continue
            fields=[f for f in self.fields if f["key"] in path["field_keys"]]
            if not fields:continue
            matched=[]
            for field in fields:
                decision=active_decision(field,self.state)
                if matches(field,path["goal"]) or (decision and decision["mode"]=="equivalent" and
                    decision["sources"].get(path["source_ref"])==path["goal"] and matches(field,decision["value"])):
                    matched.append(field["key"])
            complete=bool(matched)
            if path.get("complete")!=complete: path["complete"]=complete;changed=True
            if key in matched and decisions.get(key,{}).get("mode")=="category": decisions.pop(key);changed=True
        if changed:self._save_decisions(option_paths=paths,field_decisions=decisions)

    def _unresolved_paths(self):
        keys={f["key"] for f in self.fields}
        return [k for k,v in self.state.get("option_paths",{}).items() if k in keys and not v.get("complete")]

    def _record_answers(self, args):
        from .conversation import ordered_answers, asked_keys
        if args["observation_id"] != self.observation_id:
            raise BrowserBlocked("Stale observation. Observe the form again.", code='stale_observation')
        items = args["answers"]
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise BrowserBlocked("Record between one and twenty grounded answers.")
        fields = {f["key"]: f for f in self.fields}
        messages = self.state.get("messages", [])
        order = {m["id"]: i for i, m in enumerate(messages)}
        prepared, seen, semantic = [], set(), []
        revision = self.state.get("chat_revision", 0)
        for item in items:
            if not isinstance(item, dict) or not {"field_key", "message_id", "quote", "value"} <= set(item) <= {
                    "field_key", "message_id", "quote", "value", "reuse"} or item.get("reuse", "this_application") not in {
                    "all_applications", "this_employer", "this_application"}:
                raise BrowserBlocked("Each answer needs a field, source message, literal quote and value (and optionally its reuse scope).")
            key, message_id, quote, value = (item[k] for k in ("field_key", "message_id", "quote", "value"))
            field = fields.get(key)
            message = next((m for m in messages if m["id"] == message_id and m["role"] == "user"), None)
            if not field or key in seen or field["kind"] in {"file", "password"}:
                raise BrowserBlocked("Choose each observed answer field at most once.")
            if not message or not isinstance(quote, str) or not quote.strip() or quote.casefold() not in message["text"].casefold():
                raise BrowserBlocked("Cite a literal quote from the user's message.")
            if not isinstance(value, str) or not value.strip() or len(value) > 2000:
                raise BrowserBlocked("Supply a bounded, nonempty field value.")
            if boolean_answer(value) is not None and boolean_answer(quote) is None:
                evidence = (field["label"], quote, value)
                if evidence not in self.verified_answers:
                    semantic.append(evidence)
            elif (not grounded(field, quote, value, self.observed_options.get(key) or field.get("options", []))
                    and not confirms_proposal(messages[:order[message_id]], quote, value)):
                raise BrowserBlocked("The value must be stated in the quoted part of the user's message. "
                    "Quote their words for this answer, or ask them if they did not give it.", code="quote_mismatch")
            if message.get("field_key") and message["field_key"] != key:
                raise BrowserBlocked("That explicit field reply belongs to another question.")
            previous = self.state.get("answer_sources", {}).get(key, {})
            if previous.get("message_id") in order and order[previous["message_id"]] > order[message_id]:
                raise BrowserBlocked("Use the latest correction, not an older answer.")
            # A bare yes/no carries no independent question identity. Resolve
            # it against what the user was actually asked, never another field.
            if boolean_answer(quote) is not None:
                history = messages[:order[message_id]]
                mapped = ordered_answers(message["text"], history, self.fields)
                keys = next((asked_keys(m, self.fields) for m in reversed(history) if asked_keys(m, self.fields)), [])
                if not message.get("field_key") and key not in mapped and keys != [key]:
                    raise BrowserBlocked("This yes/no does not identify this question. Ask a focused clarification.")
                if key in mapped and boolean_answer(mapped[key]) is not boolean_answer(value):
                    raise BrowserBlocked("The normalized answer reverses the user's answer.")
            if field["kind"] == "checkbox":
                value = boolean_answer(value)
                if value is None:
                    raise BrowserBlocked("Normalize an explicit checkbox answer to Yes or No.")
            elif field["kind"] in {"select", "radio", "combobox"}:
                options = self.observed_options.get(key) or field.get("options", [])
                if options:
                    option = resolve_option(field["label"], value, options)
                    if option is not None:value = option["label"]
                    # Save the user's fact even when only parent categories or
                    # partial suggestions are currently visible. Mapping/filling
                    # belongs to the next browser decision, not answer ingestion.
            seen.add(key)
            prepared.append({**item, "value": value})
        unsupported = set()
        if semantic:
            if self.verify_answers is None:
                raise BrowserBlocked("This nonliteral Yes/No answer needs semantic verification before filling.", code="quote_mismatch")
            checks = self.verify_answers([{"question": label, "quote": quote, "proposed": value}
                                          for label, quote, value in semantic])
            if len(checks) != len(semantic):
                raise BrowserBlocked('Incomplete semantic verification; no unverified answers were saved.', code='verification_unavailable')
            unsupported = {evidence for evidence, checked in zip(semantic, checks) if checked is not True}
            if not self.active() or (self.refresh and self.refresh().get("chat_revision", 0) != revision):
                raise BrowserBlocked("The conversation changed during verification. Read the latest reply.", code="stale_observation")
            self.verified_answers.update(evidence for evidence in semantic if evidence not in unsupported)
        rejected = [item['field_key'] for item in prepared
                    if (fields[item['field_key']]['label'], item['quote'],
                        next(raw['value'] for raw in items if raw['field_key'] == item['field_key'])) in unsupported]
        prepared = [item for item in prepared if item['field_key'] not in rejected]
        if rejected and not prepared:
            raise BrowserBlocked('The quoted statement does not support the proposed Yes/No answer. Preserve its negation and scope; clarify only genuinely ambiguous facts.', code='quote_mismatch')
        if self.bind_answers:
            self.bind_answers(prepared, revision)
        else:
            for item in prepared:
                if self.accept_answer: self.accept_answer(item["field_key"], item["value"], item["message_id"])
        self.state["answers"].update({item["field_key"]: item["value"] for item in prepared})
        if rejected:
            return {'saved': [item['field_key'] for item in prepared], 'failed_fields': rejected,
                'error': 'Some proposed answers were not supported by their quotes.', 'error_code': 'quote_mismatch',
                'next': 'Autofill the saved answers. Correct or clarify only failed_fields; do not ask for the saved answers again.'}
        return {"saved": [item["field_key"] for item in prepared], "next": "autofill"}


def application_history(history):
    result = []
    recent = history[-8:]
    for index, row in enumerate(recent):
        if index < len(recent) - 3:
            data = row.get('result') or {}
            result.append({'tool': row.get('tool'), 'field_key': row.get('arguments', {}).get('field_key'),
                'error_code': data.get('error_code'), 'next': data.get('next')})
        else:
            result.extend(history_context([row]))
    return result
