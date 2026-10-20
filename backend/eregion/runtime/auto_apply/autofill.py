"""Reusable bulk-fill tool, with field-level failures and bounded verification."""
import hashlib
from datetime import datetime, timezone
import json
import time
import re

from .errors import failure_detail, failure
from .contracts import candidate_answer
from .matching import city_equivalent, consent_answer, equivalent, resolve_option, field_category
from .decisions import active_decision


def desired_answer(field, state):
    from .education import format_date
    from .experience import binding as experience_binding
    saved = experience_binding(field, state)
    part = saved['component'].rsplit('_', 1)[-1] if saved else None
    return format_date(field, _desired_answer(field, state), part)


def _desired_answer(field, state):
    from .education import binding, bound_value
    decision = active_decision(field, state)
    from .experience import binding as experience_binding
    bound = binding(field, state) or experience_binding(field, state)
    # A field tied to one education record keeps that record's facts. Adobe: the second
    # school's Degree kept a decision sourced from the profile's latest degree (M.Eng. ->
    # "Masters") although it was bound to the B.A. record.
    # A work field's decision may rest on any fact of its own record (same-record evidence,
    # experience-page test); education keeps the exact bound component.
    record = bound['source_ref'].rsplit(':', 1)[0] + ':' if bound and bound.get('experience_id') else None
    if decision and bound and decision.get('mode') != 'leave_empty' and not any(
            ref == bound['source_ref'] or ref.startswith(('answer:', 'message:')) or (record and ref.startswith(record))
            for ref in decision.get('sources', {})):
        decision = None
    if decision: return decision["value"]
    if field['key'] in state['answers']:return state['answers'][field['key']]
    if field['kind'] != 'file' and 'cover letter' in field['label'].casefold() and state.get('cover_letter'):
        return state['cover_letter']
    from .experience import binding as experience_binding, bound_value as experience_value
    if experience_binding(field, state):
        return experience_value(field, state)
    education=bound_value(field,state)
    if education is not None:return education
    from .structural_facts import structural_fact
    structural = structural_fact(field, state["profile"])
    if structural is not None:
        return structural
    answer = candidate_answer(field, state["profile"], state["answers"])
    return answer if answer is not None else consent_answer(field, state.get("consent_policy", "ask"))


def matches(field, value):
    from .browser import date_segment, same_segment
    if date_segment(field) and str(value or '').strip():
        # Workday shows a typed month "08" as "8" (Adobe re-test 2026-10-16).
        return same_segment(field['value'], value) and not field.get('invalid')
    if field['kind'] == 'location':
        # The browser exposes a value only after this picker commits a place.
        return bool(field['value']) and city_equivalent(value, field['value']) and not field.get('invalid')
    if field["kind"] in {"radio", "select", "combobox"} or field.get("choice_control"):
        option = resolve_option(field["label"], value, field.get("options", []))
        if option:
            selected = str(field["value"]).strip().casefold()
            exact = selected in {str(option["label"]).strip().casefold(), str(option["value"]).strip().casefold()}
            country_match = (field.get('value_committed') and field_category(field['label']) == 'country'
                and equivalent(field['label'], field['value'], option))
            return bool(exact or country_match) and not field.get("invalid")
        if not field.get("options") and equivalent(field["label"], value, {"label": field["value"]}):
            return not field.get("invalid")
        accepted = {str(value).strip().casefold()}
        for option in field.get("options", []):
            if accepted.intersection({str(option["label"]).strip().casefold(), str(option["value"]).strip().casefold()}):
                accepted.update((str(option["label"]).strip().casefold(), str(option["value"]).strip().casefold()))
        return str(field["value"]).strip().casefold() in accepted and not field.get("invalid")
    if field['kind'] == 'tel' or (field['kind'] == 'text' and re.search(r'\b(phone|telephone|mobile)\b', field['label'], re.I)):
        observed, expected = str(field['value']), str(value)
        if all(re.fullmatch(r'[\d\s()+.\-]+', text) for text in (observed, expected)):
            # Browser input masks may insert punctuation. Never drop a country
            # prefix here: the agent must explicitly bind that format decision.
            digits = re.sub(r'\D', '', expected)
            return bool(digits) and re.sub(r'\D', '', observed) == digits and not field.get('invalid')
    if field['kind'] in {'text', 'textarea', 'search'} and isinstance(value, str):
        # Single-line controls can flatten line breaks in prose. Verify all words
        # and punctuation while tolerating this presentation-only whitespace.
        normalize = lambda text: re.sub(r'\s+', ' ', str(text)).strip()
        return normalize(field['value']) == normalize(value) and not field.get('invalid')
    return field["value"] == value and not field.get("invalid")


# Uploads that ask for a document Eregion doesn't hold (Veeva pilot: the resume went into
# "Please upload your most recent transcript"). These are left for the user.
OTHER_DOCUMENT = re.compile(r'transcript|portfolio|writing sample|work sample|code sample|reference|recommendation|'
    r'certificat|diploma|licen[cs]e|passport|identification|\bid\b|visa|permit|photo|headshot|'
    r'video|thesis|publication|research statement|teaching statement|grade report', re.I)


def other_document(field):
    label = field.get('label') or ''
    # "Resume or portfolio" still accepts the resume.
    return bool(OTHER_DOCUMENT.search(label)) and not re.search(r'resume|résumé|curriculum|\bcv\b|cover letter', label, re.I)


def document_kind(field,state):
    if field['kind']!='file':return None
    if other_document(field):return None
    bound = state.get('document_bindings',{}).get(field['key'])
    if isinstance(bound, dict):bound = bound.get('document')
    if bound:return bound
    label=field['label'].casefold()
    if 'cover letter' in label:return 'cover_letter'
    if re.search(r'resume|résumé|curriculum|\bcv\b',label):return 'resume'
    # A required upload with only a generic label ("Attachments", "Upload file")
    # is the application's document slot; optional extra-document fields are not.
    if field.get('required') and re.search(r'attach|upload|document|\bfile\b',label):return 'resume'
    return None


UPLOAD_NAMES = {'resume': 'resume.pdf', 'cover_letter': 'cover-letter.pdf'}


def attached_names(field):
    """Successful attachments shown by this control, duplicates included."""
    if field.get('attachments') is not None:
        return list(field['attachments'])
    return [name.strip() for name in str(field.get('value') or '').split(';') if name.strip()]


def upload_plan(field, state, kind, data):
    """Upload, skip or hold one document control, from this application's durable upload
    record and the attachments the site shows. A file name alone never proves content: only
    a record of this application uploading this fingerprint under that name does."""
    filename = UPLOAD_NAMES[kind]
    fingerprint = hashlib.sha256(data).hexdigest()
    names = attached_names(field)
    record = (state.get('document_uploads') or {}).get(kind) or {}
    same = record.get('fingerprint') == fingerprint
    if same and record.get('status') == 'verified' and filename in names:
        return {'action': 'skip', 'reason': 'verified_present'}
    if same and record.get('status') == 'attempted' and names.count(filename) > record.get('before', 0):
        # The earlier upload finished after its wait timed out.
        return {'action': 'skip', 'reason': 'completed_after_attempt', 'verify': True}
    kept = state.get('document_bindings', {}).get(field['key'])
    if same and record.get('status') == 'kept_by_user' and sorted(record.get('kept', [])) == sorted(names):
        return {'action': 'skip', 'reason': 'kept_by_user'}
    if names and field.get('appends'):
        if isinstance(kept, dict) and kept.get('keep_existing') and sorted(kept.get('observed', [])) == sorted(names):
            return {'action': 'skip', 'reason': 'kept_by_user', 'keep': names}
        ours = record.get('status') in {'verified', 'attempted', 'kept_by_user'} and filename in names
        message = (f"This control appends files and already shows {names}: an older version of the {kind.replace('_', ' ')} "
                   "uploaded by this application. Uploading would add a second copy. Ask the user to remove the old "
                   "attachment in the browser (Control browser), then autofill again." if ours else
                   f"This control appends files and already shows {names}, which this application did not upload. "
                   "Do not add another copy. Ask the user whether to keep them (then bind_document with keep_existing "
                   "citing their reply) or remove them in the browser before uploading.")
        return {'action': 'hold', 'code': 'document_existing_unverified', 'message': message}
    return {'action': 'upload', 'filename': filename, 'fingerprint': fingerprint, 'before': names.count(filename)}


def record_upload(state, kind, field, plan, status, after=None):
    material = (state.get('material_refs') or {})
    ref = material.get('tailored_resume' if kind == 'resume' else kind) or material.get(kind)
    state.setdefault('document_uploads', {})[kind] = {
        'fingerprint': plan['fingerprint'], 'filename': plan['filename'], 'field_key': field['key'],
        'material_ref': ref, 'status': status, 'before': plan['before'],
        **({'after': after} if after is not None else {}), 'at': datetime.now(timezone.utc).isoformat()}


def pending_documents(state,fields):
    refresh=state.get('document_refresh',{})
    # A changed material is an upload obligation only when this application
    # actually exposes that document control. Keep earlier steps in scope: a
    # later review page must not erase an observed, still stale upload.
    observed={document_kind(f,state) for f in [*fields,*state.get('review_fields',[])]}
    return [kind for kind in ('resume','cover_letter') if refresh.get(kind) and kind in observed]


def failure_signature(field, value):
    """What must change before a failed fill is worth trying again: the target answer, the
    control's value/options or its error state (W1, Adobe: 164 of 272 host autofills failed,
    mostly re-attempting the same unchanged Field of Study)."""
    from .browser import canonical_target
    target = 'document' if isinstance(value, (bytes, bytearray)) else canonical_target(field, value)
    payload = [target, canonical_target(field, field.get('value')), [o.get('label') for o in field.get('options') or []],
               bool(field.get('invalid')), field.get('errors') or []]
    return hashlib.sha256(json.dumps(payload, default=str, sort_keys=True).encode()).hexdigest()[:16]


def autofill(browser, state, resume, *, edited_keys=frozenset(), active=lambda: True, progress=lambda *args: None):
    attempts, failed, completed, details, uploads = {}, {}, [], {}, {}
    memory = state.setdefault('autofill_failures', {})
    deadline = time.monotonic() + 90
    for _ in range(60):
        if not active():
            return {"cancelled": True, "filled": completed, "failed": failed, "failure_details": details}
        fields = browser.observe()
        if browser.barrier():
            break
        pending = []
        # Upload first: ATS resume parsers may populate contact fields afterwards.
        # Workday discards a provisional month while the year is blank. Write already
        # sourced years before months/days, without adding any field to the selected scope.
        def write_order(item):
            from .browser import date_segment
            identifier = item.get('dom_id') or item['key']
            part = 0 if 'dateSectionYear' in identifier else 1 if 'dateSectionMonth' in identifier else 2
            return (0, 0) if item['kind'] == 'file' else (2, part) if date_segment(item) else (1, 0)
        for field in sorted(fields, key=write_order):
            key = field["key"]
            if state.get('autofill_scope') is not None and key not in state['autofill_scope']:
                continue
            if attempts.get(key, 0) >= (1 if state.get("automation_mode") == "hybrid" else 2):
                continue
            # Optional fields set aside after repeated failures (runner FIELD_ATTEMPT_LIMIT).
            if key in state.get("abandoned_fields", ()) and not field.get("required"):
                continue
            value = desired_answer(field, state)
            # A website's preselected value must not override the candidate's
            # confirmed answer, especially remembered refusals of consent.
            if value is not None and matches(field, value):
                failed.pop(key, None); details.pop(key, None)
                continue
            if value is not None and field.get("invalid") and matches({**field, "invalid":False}, value):
                failed[key] = "; ".join(field.get("errors",[])) or "website_validation_error"
                details[key] = failure("validation_error", failed[key])
                continue # Rewriting a rejected value is not a repair strategy.
            kind=document_kind(field,state)
            if kind=='resume':
                if field["value"] and not state.get('document_refresh',{}).get(kind):
                    continue
                value = resume()
            elif kind=='cover_letter' and state.get('cover_letter'):
                if field['value'] and not state.get('document_refresh',{}).get(kind):continue
                from eregion.domain.pdf_rendering import render_letter_pdf
                value = render_letter_pdf(state['cover_letter'])
            if kind in UPLOAD_NAMES and isinstance(value, (bytes, bytearray)):
                plan = upload_plan(field, state, kind, value)
                if plan['action'] == 'skip':
                    record = state.setdefault('document_uploads', {}).setdefault(kind, {})
                    if plan.get('verify'):
                        record.update(status='verified', after=attached_names(field).count(record.get('filename')))
                    if plan.get('keep'):
                        record.update(fingerprint=hashlib.sha256(value).hexdigest(), status='kept_by_user',
                                      kept=plan['keep'], field_key=key, at=datetime.now(timezone.utc).isoformat())
                    if state.get('document_refresh'):
                        state['document_refresh'][kind] = False
                    failed.pop(key, None); details.pop(key, None); memory.pop(key, None)
                    continue
                if plan['action'] == 'hold':
                    failed[key] = plan['code']; details[key] = failure(plan['code'], plan['message'])
                    continue
                uploads[key] = (kind, plan)
            if value is not None:
                if (state.get("automation_mode") == "hybrid" and value in {state["profile"].get("phone"), state["profile"].get("city")}
                        and field["kind"] != "location" and key not in state["answers"] and not active_decision(field,state)):
                    # The model sees the whole contact section and decides whether this
                    # website expects a full number or separate components, or the city alone
                    # rather than the profile's "City, ST" (Adobe/Salesforce: City = "Ithaca, NY").
                    continue
                # A custom picker can contain text while still having no selected
                # value. Let the model explore its choices instead of repeatedly
                # typing the same answer into the search input.
                if (field.get("choice_control") and field["kind"] not in {"select", "radio", "combobox"}
                        and not field.get("options") and state.get("automation_mode") == "hybrid"):
                    continue
                # Across calls only: within one call the bounded retry above still applies.
                if not attempts.get(key) and memory.get(key, {}).get('signature') == failure_signature(field, value):
                    failed[key] = memory[key]['code']
                    details[key] = failure('unchanged_failure', 'Not retried: this fill failed before and its target, control and '
                        'options are unchanged. Decide a different option or source, or ask the user.')
                    continue
                plain = field['kind'] in {'text', 'email', 'tel', 'url', 'textarea', 'number', 'search'} and not field.get('choice_control')
                if not plain and pending:
                    break
                pending.append((field, value, plain))
                if not plain:
                    break
        if not pending or time.monotonic() > deadline:
            break
        attempted = []
        for field, value, plain in pending:
            if not active():
                break
            key = field['key']
            attempts[key] = attempts.get(key, 0) + 1
            try:
                upload = document_kind(field, state)
                target = {**field, 'upload_filename': 'cover-letter.pdf' if upload == 'cover_letter' else 'resume.pdf'} if upload else field
                if key in uploads:
                    # Recorded before the write, so a timed-out upload that completes is recognized.
                    record_upload(state, *uploads[key][:1], field, uploads[key][1], 'attempted')
                if plain and hasattr(browser, 'fill_plain'):
                    browser.fill_plain(target, value)
                else:
                    browser.fill(target, value)
                attempted.append((field, value))
            except Exception as exc:
                details[key] = failure_detail(exc)
                failed[key] = details[key]['error_code']
                latest = next((f for f in browser.observe() if f['key'] == key), field)
                memory[key] = {'signature': failure_signature(latest, value), 'code': failed[key]}
        observed = browser.observe()
        current_fields = {f['key']: f for f in observed}
        for field, value in attempted:
            key = field['key']
            current = current_fields.get(key)
            if key in uploads:
                kind, plan = uploads[key]
                after = attached_names(current).count(plan['filename']) if current is not None else None
                # One more card with this name, or the native input holding exactly this file. A
                # vanished control or a non-empty input is not evidence of this upload.
                verified = after is not None and (after > plan['before'] if field.get('appends')
                                                  else attached_names(current) == [plan['filename']])
                record_upload(state, kind, field, plan, 'verified' if verified else 'attempted', after)
            elif field['kind'] == 'file':
                verified = current is None or bool(current['value'])
            else:
                verified = current is not None and matches(current, value)
            if verified:
                completed.append(key)
                failed.pop(key, None); details.pop(key, None); memory.pop(key, None)
                if document_kind(field, state) and state.get('document_refresh'):
                    state['document_refresh'][document_kind(field, state)] = False
            else:
                failed[key] = 'value_not_retained'
                details[key] = failure('value_not_retained', 'The control did not retain the supplied value.')
                memory[key] = {'signature': failure_signature(current or field, value), 'code': failed[key]}
        progress(observed, 'autofill', None, 'needs_agent' if failed else 'verified')
    if failed and active():
        for current in browser.observe():
            key = current['key']
            if key not in failed:
                continue
            target = desired_answer(current, state)
            if target is not None and matches(current, target):
                completed.append(key)
                failed.pop(key, None); details.pop(key, None); memory.pop(key, None)
    return {"filled": list(dict.fromkeys(completed)), "failed": failed, "failure_details": details}
