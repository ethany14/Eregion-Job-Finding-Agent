"""Optimistic state writes and one durable attempt per posting."""
import base64
import hashlib
import json
import re
import os
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .contracts import TERMINAL, SUBMISSION_STATES, can_reopen, posting, candidate_answer
from .models import AutoApplicationRow, ApplicationMemoryRow, SavedCandidateRow
from .matching import question_identity, boolean_answer, clean


class ApplyConflict(RuntimeError):
    pass


class DuplicateApplication(ApplyConflict):
    def __init__(self, run_id):
        super().__init__("An attempt already exists for this job. Open the existing application to view or resume it.")
        self.run_id = run_id


class ApplyNotFound(LookupError):
    pass


def open_limit():
    """Unfinished attempts allowed at once (each may keep a Chrome window open)."""
    try:
        return max(1, int(os.getenv("JOB_AGENT_MAX_OPEN_APPLICATIONS", "6")))
    except ValueError:
        return 6


def now():
    return datetime.now(UTC).isoformat()


class ApplyRepository:
    def __init__(self, session_factory):
        self.sessions = session_factory
        self.state_listeners = []

    def saved_candidate(self):
        with self.sessions.begin() as session:
            row=session.get(SavedCandidateRow,'local')
            if row is None:
                previous=session.scalars(select(AutoApplicationRow)).all()
                latest=max(previous,key=lambda r:json.loads(r.state_json).get('updated_at',''),default=None)
                data=json.loads(latest.state_json) if latest else {}
                row=SavedCandidateRow(slot='local',profile_json=json.dumps(data.get('profile',{})),
                    resume_bytes=latest.resume_bytes if latest else None,resume_name='resume.pdf',
                    extracted=False,updated_at=now())
                session.add(row)
            return {'profile':json.loads(row.profile_json),'resume_base64':base64.b64encode(row.resume_bytes).decode() if row.resume_bytes else None,
                'resume_name':row.resume_name,'needs_extraction':bool(row.resume_bytes and not row.extracted),'updated_at':row.updated_at}

    def save_candidate(self,profile,*,resume=None,name=None,extracted=None):
        from .contracts import CandidateDetails
        from .education import normalize_records
        profile=CandidateDetails.model_validate(profile).model_dump()
        profile['education']=normalize_records(profile.get('education',[]))
        from .experience import normalize_records as normalize_experience
        profile['experience']=normalize_experience(profile.get('experience',[]))
        with self.sessions.begin() as session:
            row=session.get(SavedCandidateRow,'local')
            if row is None:
                row=SavedCandidateRow(slot='local',profile_json='{}',resume_bytes=None,resume_name='resume.pdf',extracted=False,updated_at=now())
                session.add(row)
            if resume is not None:
                if row.resume_bytes!=resume:row.extracted=False
                row.resume_bytes=resume
            if name:row.resume_name=name
            if extracted is not None:row.extracted=extracted
            row.profile_json=json.dumps(profile);row.updated_at=now()
        if resume:
            from io import BytesIO
            from pypdf import PdfReader
            from eregion.runtime.analysis_cache import warm_resume
            from eregion.runtime.memory.policy import LocalOwnerResolver
            try:
                text = '\n\n'.join((page.extract_text() or '').strip() for page in PdfReader(BytesIO(resume)).pages).strip()
                if text:
                    warm_resume(self.sessions, text, owner=LocalOwnerResolver().resolve().owner_id)
            except Exception:
                pass  # Saving candidate details must not depend on model availability.
        return profile

    def clear_candidate(self):
        # Keep a tombstone so legacy attempt backfill cannot resurrect a removal.
        self.save_candidate({})
        with self.sessions.begin() as session:
            row=session.get(SavedCandidateRow,'local');row.resume_bytes=None;row.extracted=False

    def refresh_education(self,state):
        if state['profile'].get('education'):return state
        saved=self.saved_candidate()
        if (saved['profile'].get('education') and self.owner_key(saved['profile'])==self.owner_key(state['profile'])
            and saved['resume_base64'] and base64.b64decode(saved['resume_base64'])==self.resume(state['run_id'])):
            return self.save(state,profile={**state['profile'],'education':saved['profile']['education']})
        return state

    def create(self, request, *, material_refs=None, origin_task_id=None):
        platform, identity, url = posting(request.url)
        fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        with self.sessions.begin() as session:
            existing = session.scalar(select(AutoApplicationRow).where(
                AutoApplicationRow.posting_key == identity))
            if existing is None or existing.status not in SUBMISSION_STATES:
                # Pre-normalization rows retain their old key. Inspect their
                # original URL so upgrading/restarting cannot reopen a job
                # already submitted under its /apply alias. Do not rewrite IDs.
                aliases=[]
                for row in session.scalars(select(AutoApplicationRow)):
                    try:matches=posting(json.loads(row.state_json)['url'])[1]==identity
                    except (ValueError,KeyError):matches=False
                    if matches:aliases.append(row)
                existing=next((r for r in aliases if r.status in SUBMISSION_STATES),existing or next(iter(aliases),None))
            if existing:
                if existing.idempotency_key == request.idempotency_key and existing.request_hash == fingerprint:
                    return self._state(existing), False
                raise DuplicateApplication(existing.run_id)
            active = session.scalars(select(AutoApplicationRow.run_id).where(
                AutoApplicationRow.status.not_in(TERMINAL))).all()
            if len(active) >= open_limit():
                raise ApplyConflict(f"Finish or cancel an existing attempt before opening another browser (limit: {open_limit()}).")
            state = {"run_id": str(uuid4()), "platform": platform, "url": url,
                "profile": request.profile.model_dump(), "auto_submit": request.auto_submit,
                "automation_mode": request.automation_mode, "visual_fallback": request.visual_fallback,
                "reuse_answers": request.reuse_answers, "consent_policy": request.consent_policy,
                "cover_letter": request.cover_letter,
                "material_refs": material_refs or {},
                "origin_task_id": origin_task_id,
                "document_refresh": {'resume':True,'cover_letter':bool(request.cover_letter)} if material_refs is not None else {},
                "messages": [], "chat_revision": 0, "answer_sources": {}, "pause_requested": False,
                "memory_indexed": True,
                "agent_calls": 0, "agent_tools": 0,
                "budget_model_base": 0, "budget_tool_base": 0, "recovery_attempts": 0,
                "continuations": 0, "task_plan": [], "last_tool_result": None,
                "answers": {}, "fields": [], "review_digest": None, "message": "Waiting to open the application.",
                "created_at": now(), "updated_at": now(), "events": [], "cancel_requested": False}
            if request.initial_message:
                state["messages"] = [{"id": str(uuid4()), "role": "user", "text": request.initial_message, "at": now()}]
            state["messages"].append({"id": str(uuid4()), "role": "assistant", "at": now(),
                "text": "I’m opening the application and using your resume details. You can send corrections or ask me to pause at any time."})
            row = AutoApplicationRow(run_id=state["run_id"], posting_key=identity,
                idempotency_key=request.idempotency_key, request_hash=fingerprint,
                version=1, status="queued", state_json=json.dumps(state),
                resume_bytes=base64.b64decode(request.resume_base64))
            session.add(row)
            try:
                session.flush()
            except IntegrityError as exc:
                raise ApplyConflict("This job or request already has an attempt. Refresh the list.") from exc
            return self._state(row), True

    @staticmethod
    def _state(row):
        return {**json.loads(row.state_json), "version": row.version, "status": row.status}

    def get(self, run_id):
        with self.sessions() as session:
            row = session.get(AutoApplicationRow, run_id)
            if row is None:
                raise ApplyNotFound("Application attempt not found.")
            return self._state(row)

    def resume(self, run_id):
        with self.sessions() as session:
            row = session.get(AutoApplicationRow, run_id)
            if row is None:
                raise ApplyNotFound("Application attempt not found.")
            return row.resume_bytes

    def replace_documents(self, state, resume, cover_letter, *, profile=None, material_refs=None):
        """Replace an unsubmitted attempt's files and invalidate its review atomically."""
        from .contracts import SUBMISSION_STATES
        with self.sessions.begin() as db:
            row = db.get(AutoApplicationRow, state['run_id'])
            if row is None or row.version != state['version'] or row.status in SUBMISSION_STATES:
                raise ApplyConflict('The application changed; inspect it before updating materials.')
            updated = {**self._state(row), 'cover_letter':cover_letter,
                'material_refs':material_refs or {},
                'document_refresh':{'resume':True,'cover_letter':bool(cover_letter)},
                'review_digest':None, 'resume_context':None, 'reopening':True,
                'pause_requested':True, 'input_request':None, 'pending_fields':[],
                'fields':[], 'review_fields':[], 'drafts':{}, 'field_decisions':{},
                'chat_revision':state.get('chat_revision',0)+1, 'updated_at':now(),
                'message':'Reopening to upload the updated application materials.'}
            if profile is not None:updated['profile']=profile
            row.resume_bytes = resume
            row.status, row.version = 'queued', row.version+1
            row.state_json = json.dumps(updated)
            result = self._state(row)
        for listener in tuple(self.state_listeners):
            listener(result)
        return result

    @staticmethod
    def owner_key(profile):
        return hashlib.sha256(profile.get("email",'').strip().casefold().encode()).hexdigest()

    def memories(self, profile):
        from .memory import default_scope
        with self.sessions() as session:
            rows = session.scalars(select(ApplicationMemoryRow).where(ApplicationMemoryRow.owner_key == self.owner_key(profile))).all()
            records = [json.loads(row.record_json) for row in rows]
        # Records from before approval tracking were explicit answers; the user
        # chose to keep them approved with their previous (global) reuse.
        return [{"status": "approved", "scope": "global", **record} if "status" not in record
                else {"scope": default_scope(record["question"]), **record} for record in records]

    def _answer_rows(self, session, owner, identity, scope, url=None):
        from .memory import record_scope, same_employer
        found = []
        for row in session.scalars(select(ApplicationMemoryRow).where(ApplicationMemoryRow.owner_key == owner)).all():
            record = json.loads(row.record_json)
            if (question_identity(record["question"]) == identity and record_scope(record) == scope
                    and (scope == "global" or same_employer(record.get("url"), url))):
                found.append((row, record))
        return sorted(found, key=lambda item: item[1].get("updated_at", ""), reverse=True)

    def update_memory(self, profile, memory_id, *, value=None, scope=None, url=None):
        from .memory import SCOPES, employer_key, record_scope
        if scope is not None and scope not in SCOPES:
            raise ApplyConflict("Choose where this answer can be reused.")
        if isinstance(value, str) and (not value.strip() or len(value) > 10_000):
            raise ApplyConflict("Enter a nonempty answer.")
        with self.sessions.begin() as session:
            row = session.get(ApplicationMemoryRow, memory_id)
            if row is None or row.owner_key != self.owner_key(profile): raise ApplyNotFound("Remembered answer not found.")
            record = json.loads(row.record_json)
            record["scope"] = record_scope(record)
            if value is not None: record["value"] = value.strip() if isinstance(value, str) else value
            if scope is not None:
                record["scope"] = scope
                if scope == "employer":
                    record["url"] = url or record.get("url")
            if record["scope"] == "employer" and not employer_key(record.get("url")):
                raise ApplyConflict("Choose an application with an identified employer before limiting this answer.")
            # IDs are stable references. Coalesce the destination bucket when
            # the user explicitly moves an answer, without touching other employers.
            for other, _ in self._answer_rows(session, row.owner_key, question_identity(record["question"]),
                                              record["scope"], record.get("url")):
                if other.memory_id != memory_id:
                    session.delete(other)
            record.update(status="approved", updated_at=now(), approved_at=now())
            row.record_json = json.dumps(record)
            return record

    def remember(self, state, answers, *, historical=False, scopes=None):
        """Save answers the user approved. Run-local interpretations are never saved here."""
        from .memory import SCOPES, default_scope, employer_key, record_scope
        if not state.get("reuse_answers", True): return
        fields = {f["key"]: f for f in state["fields"]}
        owner = self.owner_key(state["profile"])
        profile_updates={}
        with self.sessions.begin() as session:
            for key, answer in answers.items():
                if key not in fields or answer == "": continue
                field = fields[key]
                if field["kind"] in {"file", "password"}: continue
                if clean(field["label"]) in {"i agree", "agree", "accept", "yes", "no", "consent", "authorization", "acknowledge confirm", "acknowledge", "confirm"}: continue
                # Store the label, not employer-specific numeric option IDs.
                option = next((o for o in field.get("options", []) if str(o["value"]) == str(answer)), None)
                value = option["label"] if option else answer
                # Explicit contact corrections should outlive this application.
                # Keep split phone values, account identity and per-row education
                # out of this flat update; their sources have different scopes.
                from .contracts import normalized_label
                contact={'first name':'first_name','last name':'last_name','city':'city','location (city)':'city',
                    'current city':'city','country':'country','country of residence':'country','linkedin':'linkedin','linkedin profile':'linkedin',
                    'linkedin url':'linkedin','github':'github','github url':'github','website':'website','portfolio':'website'}
                member=contact.get(normalized_label(field['label']))
                identity = question_identity(field["label"])
                if sum(question_identity(f["label"]) == identity for f in fields.values()) > 1: continue
                if not historical and member and isinstance(value,str):profile_updates[member]=value
                local = self._answer_rows(session, owner, identity, "employer", state.get("url"))
                global_rows = self._answer_rows(session, owner, identity, "global")
                previous = (local or global_rows or [(None, {})])[0][1]
                scope = (scopes or {}).get(key) or (record_scope(previous) if previous else default_scope(field["label"]))
                if scope not in SCOPES:
                    raise ApplyConflict("Choose where this answer can be reused.")
                if scope == "employer" and not employer_key(state.get("url")):
                    continue
                bucket = local if scope == "employer" else global_rows
                row, previous = (bucket or [(None, {})])[0]
                # Opaque IDs survive edits to scope. Old IDs remain valid; new
                # employer buckets can never overwrite an old global-key row.
                memory_id = row.memory_id if row else uuid4().hex
                stamp = state["updated_at"] if historical else now()
                record = {"memory_id": memory_id, "question": field["label"], "identity": identity,
                    "value": value, "run_id": state["run_id"], "url":state.get('url'), "updated_at": stamp,
                    "source": "user_answer", "status": "approved", "scope": scope, "approved_at": stamp}
                if row and historical and previous["updated_at"] >= record["updated_at"]: continue
                for duplicate, _ in bucket[1:]:
                    session.delete(duplicate)
                if row: row.record_json = json.dumps(record)
                else: session.add(ApplicationMemoryRow(memory_id=memory_id, owner_key=owner, record_json=json.dumps(record)))
        if profile_updates:
            saved=self.saved_candidate()
            if self.owner_key(saved['profile'])==owner:
                try:self.save_candidate({**saved['profile'],**profile_updates})
                except ValueError:pass # Keep the confirmed answer even if it is too long for a flat contact field.

    def save_literal_profile_facts(self,state,answers,message_text):
        """Save explicit contact facts, never inferred eligibility or consent.

        Model bindings may be reused only when the selected value is actually
        present in the user's message and the field has an unambiguous label.
        """
        from .contracts import normalized_label
        contact={'country':'country','country of residence':'country','city':'city','current city':'city',
                 'location (city)':'city','linkedin':'linkedin','linkedin profile':'linkedin',
                 'linkedin url':'linkedin','github':'github','github url':'github','website':'website','portfolio':'website'}
        saved=self.saved_candidate()
        if self.owner_key(saved['profile'])!=self.owner_key(state['profile']):return {}
        fields={f['key']:f for f in state.get('fields',[])}
        changes={}
        for key,value in answers.items():
            field=fields.get(key,{})
            member=contact.get(normalized_label(field.get('label','')))
            option=next((o for o in field.get('options',[]) if str(o['value'])==str(value)),None)
            text=option['label'] if option else value
            if member and isinstance(text,str) and clean(text) and re.search(
                    r'(?<!\w)'+re.escape(clean(text))+r'(?!\w)',clean(message_text)):
                if saved['profile'].get(member)!=text:changes[member]=text
        if changes:self.save_candidate({**saved['profile'],**changes})
        return changes

    def standard_answers(self, profile):
        """Only globally approved answers belong in the all-jobs setup form."""
        from .standard import catalog
        latest = {question_identity(m["question"]): m for m in
                  sorted(self.memories(profile), key=lambda m: m.get("updated_at", ""))
                  if m.get("status") == "approved" and m.get("scope") == "global"}
        return [{**item, "value": latest.get(item["identity"], {}).get("value", ""),
                 "scope": latest.get(item["identity"], {}).get("scope", "global")} for item in catalog()]

    def save_standard_answers(self, profile, answers):
        """Answers entered in setup are approved answers; an empty value removes one."""
        from .standard import catalog
        if not profile.get("email"):
            raise ApplyConflict("Save your email in your details before adding standard answers.")
        items = {item["topic"]: item for item in catalog()}
        if not set(answers).issubset(items):
            raise ApplyConflict("Unknown standard question.")
        owner = self.owner_key(profile)
        with self.sessions.begin() as session:
            for topic, value in answers.items():
                item = items[topic]
                value = value.strip() if isinstance(value, str) else value
                bucket = self._answer_rows(session, owner, item["identity"], "global")
                row = bucket[0][0] if bucket else None
                memory_id = row.memory_id if row else uuid4().hex
                keep = value not in ("", None)
                # Setup edits and clears affect only this global question. Keep
                # employer answers and questions with different salary units.
                for duplicate, _ in (bucket[1:] if keep else bucket):
                    session.delete(duplicate)
                if not keep:
                    continue
                stamp = now()
                record = json.dumps({"memory_id": memory_id, "question": item["question"], "identity": item["identity"],
                    "value": value, "run_id": None, "url": None, "updated_at": stamp, "source": "user_answer",
                    "origin": "standard", "status": "approved", "scope": "global", "approved_at": stamp})
                if row: row.record_json = record
                else: session.add(ApplicationMemoryRow(memory_id=memory_id, owner_key=owner, record_json=record))
        return self.standard_answers(profile)

    def forget(self, profile, memory_id):
        with self.sessions.begin() as session:
            row = session.get(ApplicationMemoryRow, memory_id)
            if row is None or row.owner_key != self.owner_key(profile): raise ApplyNotFound("Remembered answer not found.")
            session.delete(row)

    def recalled_answers(self, state, fields):
        from .memory import compatible
        if not state.get("reuse_answers", True): return {}, {}
        # Filter before choosing: another employer's newer answer cannot hide
        # this employer's answer. A local preference overrides a global default.
        memories = sorted(self.memories(state["profile"]),
            key=lambda m: (m.get("scope") == "employer", m.get("updated_at", "")), reverse=True)
        answers, sources = {}, {}
        for field in fields:
            identity = question_identity(field["label"])
            memory = next((m for m in memories
                if (question_identity(m["question"]) == identity or
                    (identity.split("|")[0] == "standard:salary" and
                     question_identity(m["question"]).split("|")[0] == "standard:salary"))
                and compatible(m, field, state)), None)
            if memory and field["key"] not in state["answers"] and candidate_answer(field, state["profile"], {}) is None:
                value = memory["value"]
                if field["kind"] == "checkbox" and isinstance(value, str):
                    value = boolean_answer(value)
                    if value is None: continue
                answers[field["key"]] = value
                sources[field["key"]] = {"type": "memory", "memory_id": memory["memory_id"], "question": memory["question"]}
        return answers, sources

    def agent_context(self, run_id):
        from .drafting import resume_context
        state=self.get(run_id)
        return {**state, 'resume_context':resume_context(self.resume(run_id)),
            'remembered_answers':self.memories(state['profile']) if state.get('reuse_answers',True) else []}

    def list(self):
        with self.sessions() as session:
            rows = session.scalars(select(AutoApplicationRow).order_by(AutoApplicationRow.run_id)).all()
            return sorted((self._state(row) for row in rows), key=lambda state: state["created_at"], reverse=True)

    def save(self, state, *, status=None, message=None, **changes):
        next_state = {**state, **changes, "version": state["version"] + 1,
                      "status": status or state["status"], "updated_at": now()}
        if (state.get("submission_started") or state["status"] in SUBMISSION_STATES
                or next_state["status"] in SUBMISSION_STATES
                or any(event.get("status") in SUBMISSION_STATES for event in state.get("events", []))):
            next_state["submission_started"] = True
        if next_state['status'] in {'awaiting_review', 'needs_input', 'submitted'}:
            from eregion.runtime.telemetry import application_perf_summary
            next_state['perf_summary'] = application_perf_summary(next_state)
        if message:
            next_state["message"] = message
            next_state["events"] = [*state["events"], {"at": now(), "status": next_state["status"], "message": message}][-100:]
        with self.sessions.begin() as session:
            updated = session.execute(update(AutoApplicationRow).where(
                AutoApplicationRow.run_id == state["run_id"],
                AutoApplicationRow.version == state["version"]).values(
                    version=next_state["version"], status=next_state["status"], state_json=json.dumps(next_state)))
            if updated.rowcount != 1:
                raise ApplyConflict("The attempt changed. Refresh before continuing.")
        for listener in tuple(self.state_listeners):
            listener(next_state)
        return next_state

    def reopen(self, state):
        if not can_reopen(state):
            raise ApplyConflict("Only an application stopped before submission can be resumed.")
        with self.sessions() as session:
            active = session.scalars(select(AutoApplicationRow.run_id).where(
                AutoApplicationRow.status.not_in(TERMINAL))).all()
            if len(active) >= open_limit():
                raise ApplyConflict(f"Finish or cancel an existing attempt before opening another browser (limit: {open_limit()}).")
        # Reserve a browser slot now, but keep the old worker stopped until its
        # cancellation and browser-close jobs have drained from the same queue.
        return self.save(state, status="queued", reopening=True, pause_requested=True,
            cancel_requested=False, review_digest=None, account_request=None, auto_submit=False,
            retry_at=None, recovery_attempts=0, continuations=0,
            chat_revision=state.get("chat_revision", 0) + 1,
            budget_model_base=state.get("agent_calls", 0), budget_tool_base=state.get("agent_tools", 0),
            message="Reopening the application from saved progress.")

    def recover(self):
        resumable = []
        for state in self.list():
            if not state.get("memory_indexed"):
                self.remember(state, state.get("answers", {}), historical=True)
                state = self.save(state, memory_indexed=True)
            if state["status"] == "submitting":
                self.save(state, status="submission_unknown", message="The service stopped during submission. Check the employer confirmation before taking any further action.")
            elif state.get("reopening") and state["status"] == "queued":
                self.save(state, status="needs_input", reopening=False, pause_requested=True, review_digest=None,
                    message="Reopening was interrupted. Resume to restore the saved application.")
            elif (state.get("automation_mode") == "hybrid" and not state.get("pause_requested")
                  and not state.get("cancel_requested") and not state.get("account_request")
                  and (state["status"] in {"running", "queued"}
                       or (not state.get("input_request") and not state.get("pending_fields")
                           and (state["status"] == "awaiting_review"
                                or (state["status"] == "needs_input" and state.get("agent_reason") == "browser_session_ended"))))):
                self.save(state, status="queued", review_digest=None, retry_at=None, account_request=None,
                    recovery_review_required=True, recovery_origin=state["status"],
                    agent_reason="restoring_session", recovery_attempts=0,
                    message="Restoring the browser from saved progress. Submission will require a fresh review.")
                resumable.append(state["run_id"])
            elif state["status"] in {"running", "queued", "awaiting_review", "needs_input"}:
                # Do not replace a real question, account request or user pause
                # with a generic browser-session-ended prompt.
                waiting = state.get("pause_requested") or state.get("input_request") or state.get("pending_fields") or state.get("account_request")
                self.save(state, status="needs_input", review_digest=None,
                    **({} if waiting else {"agent_reason":"browser_session_ended",
                       "message":"The browser session ended. Continue to restore saved progress."}))
        return resumable
