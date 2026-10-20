"""Check record coverage without deciding which candidate records to include."""
from .browser import BrowserBlocked
from .decisions import active_decision, source_value
from .education import binding as education_binding
from .experience import binding as work_binding, bound_value as work_value
from .observation import record_identity
from eregion.runtime.telemetry import outcome_fingerprint


def candidates(state):
    return {f'{kind}:{r["id"]}': r for kind, name in [('work', 'experience'), ('education', 'education')]
            for r in state.get('profile', {}).get(name, [])}


def record_version(state, ref):
    kind, identifier = ref.split(':', 1)
    overrides = state.get('experience_answer_overrides' if kind == 'work' else 'education_answer_overrides', {})
    return outcome_fingerprint([candidates(state).get(ref), overrides.get(identifier)])


def active_plan(state, ref):
    record = candidates(state).get(ref)
    decision = state.get('record_plan', {}).get(ref)
    if not record or not decision or decision.get('fact_version') != record_version(state, ref):
        return None
    if decision.get('chat_revision') != state.get('chat_revision'):
        return None
    if any(source_value(state, k) != v for k, v in decision.get('sources', {}).items()):
        return None
    return decision


def plan_records(state, items):
    if not isinstance(items, list) or not 1 <= len(items) <= 50 or any(not isinstance(i, dict) for i in items):
        raise BrowserBlocked('Provide 1 to 50 record decisions.')
    records = candidates(state)
    result = dict(state.get('record_plan') or {})
    for item in items:
        ref, status = item.get('record'), item.get('status')
        if ref not in records or status not in {'include', 'omit', 'needs_input'}:
            raise BrowserBlocked('Choose an observed candidate record and include/omit/needs_input.')
        reason = item.get('reason', '')
        if not isinstance(reason, str):
            raise BrowserBlocked('The record decision needs a textual reason.')
        reason = reason.strip()
        if not reason or len(reason) > 1000:
            raise BrowserBlocked('Explain the record decision using the task and candidate evidence.')
        refs = item.get('source_refs', [])
        if not isinstance(refs, list) or len(refs) > 10 or any(not isinstance(r, str) for r in refs):
            raise BrowserBlocked('Use a bounded list of source references.')
        sources = {r: source_value(state, r) for r in refs}
        if any(v is None or v == '' for v in sources.values()) or (status == 'omit' and not sources):
            raise BrowserBlocked('A record omission needs existing cited facts and a reason.')
        result[ref] = {'status': status, 'reason': reason, 'sources': sources,
                       'fact_version': record_version(state, ref), 'chat_revision': state.get('chat_revision')}
    return result


def binding_ref(field, state):
    work, edu = work_binding(field, state), education_binding(field, state)
    return f'work:{work["experience_id"]}' if work else f'education:{edu["education_id"]}' if edu else None


def container(field):
    return record_identity(field) or field.get('group') or None


def coverage(fields, state):
    from .autofill import desired_answer, matches
    refs = {f['key']: binding_ref(f, state) for f in fields}
    containers = {}
    for f in fields:
        if refs[f['key']] and container(f):
            containers.setdefault(container(f), set()).add(refs[f['key']])
    rows = []
    for ref, record in candidates(state).items():
        decision = active_plan(state, ref)
        bound = [f for f in fields if refs[f['key']] == ref]
        group_ids = {container(f) for f in bound} - {None}
        peers = [f for f in fields if f in bound or container(f) in group_ids]
        pending, verified = [], []
        for f in peers:
            key = f['key']
            if container(f) and len(containers.get(container(f), set())) > 1:
                pending.append({'key': key, 'reason': 'conflicting_record_bindings'}); continue
            chosen = active_decision(f, state)
            prefix = ('experience:' if ref.startswith('work:') else 'education:') + ref.split(':', 1)[1] + ':'
            if chosen and any(s.startswith(('experience:', 'education:')) and not s.startswith(prefix)
                              for s in chosen.get('sources', {})):
                pending.append({'key': key, 'reason': 'decision_cites_another_record'}); continue
            if f.get('invalid'):
                pending.append({'key': key, 'reason': 'invalid'}); continue
            if chosen and chosen['mode'] == 'leave_empty':
                good = not f.get('required') and f.get('value') in ('', None, False)
            elif f['kind'] == 'checkbox' and not chosen and work_value(f, state) is None:
                good = False  # a checkbox needs a decision unless bound to a determined current_status
            else:
                target = desired_answer(f, state)
                good = target is not None and matches(f, target)
                if refs[key] is None and not chosen:
                    good = False  # A prefilled unbound peer is not verified evidence.
            if good:
                verified.append(key)
            else:
                pending.append({'key': key, 'reason': 'decision_or_binding_or_readback_missing'})
        status = decision['status'] if decision else 'include' if bound else 'pending'
        complete = bool(bound) and not pending and status == 'include'
        rows.append({'record': ref, 'label': record.get('company') or record.get('school') or ref,
                     'plan': status, 'reason': decision['reason'] if decision else None,
                     'bound': len(bound), 'verified': verified, 'pending': pending,
                     'complete': complete})
    return rows


def exposed_kinds(fields, state, page=None):
    from .runner import record_page
    if not record_page(fields, state, page):
        return set()
    # Only apply a kind's coverage on a page that actually exposes that kind.
    kinds = {ref.split(':')[0] for f in fields if (ref := binding_ref(f, state))}
    for f in fields:
        identity = record_identity(f) or ''
        if identity.startswith('workExperience'): kinds.add('work')
        if identity.startswith('education'): kinds.add('education')
    for c in (page or {}).get('controls', []):
        if c.get('kind') != 'add_entry': continue
        text = (c.get('label', '') + ' ' + c.get('section', '') + ' ' + str(c.get('automation_id') or '')).lower()
        if 'work' in text or 'experience' in text: kinds.add('work')
        if 'education' in text or 'school' in text: kinds.add('education')
    return kinds


def gaps(fields, state, page=None):
    kinds = exposed_kinds(fields, state, page)
    return [f"Record {r['record']}: {r['plan']}; pending={r['pending']}"
            for r in coverage(fields, state) if r['record'].split(':')[0] in kinds
            and not r['complete'] and r['plan'] != 'omit']


def effective_state(state):
    """Existing education/work guards honor explicit, still-valid model omissions."""
    profile = dict(state.get('profile') or {})
    for kind, name in [('work', 'experience'), ('education', 'education')]:
        profile[name] = [r for r in profile.get(name, [])
                         if (active_plan(state, f'{kind}:{r["id"]}') or {}).get('status') != 'omit']
    return {**state, 'profile': profile}


def direct_support(field, state):
    """Evidence for a value code wrote without a model decision: the bound fact, its value and the
    conversion applied. None when a saved decision or no binding supports the field."""
    if active_decision(field, state):
        return None
    saved = work_binding(field, state) or education_binding(field, state)
    if not saved:
        return None
    component = saved.get('component') or saved['source_ref'].rsplit(':', 1)[-1]
    if component == 'current_status':
        from .experience import current_basis
        conversion = 'boolean:' + str(current_basis(state['profile'], state.get('experience_answer_overrides'),
                                                     saved['experience_id']))
    else:
        conversion = 'date_part' if component.endswith(('_month', '_year')) else 'format_only'
    return {'source_ref': saved['source_ref'], 'source_value': source_value(state, saved['source_ref']),
            'conversion': conversion}


def completion_evidence(fields, state):
    """Per complete record: its fact version, the verified fields, and for each field written from a
    bound fact without a model decision, that fact and conversion (model decisions stay in
    field_decisions; nothing here is recorded as one)."""
    by_key = {f['key']: f for f in fields}
    result = {}
    for row in coverage(fields, state):
        if not row['complete']:
            continue
        direct = {key: support for key in row['verified'] if (support := direct_support(by_key[key], state))}
        result[row['record']] = {'fact_version': record_version(state, row['record']),
                                 'verified_fields': row['verified'], **({'direct_facts': direct} if direct else {})}
    return result


def review_gaps(state):
    """Recheck earlier record-page coverage at final review, including edited facts."""
    records = candidates(state)
    evidence = state.get('record_completion', {})
    required_kinds = set(state.get('record_kinds_seen', []))
    result = []
    for ref, record in records.items():
        if ref.split(':')[0] not in required_kinds: continue
        plan = active_plan(state, ref) or {}
        if plan.get('status') == 'omit': continue
        if plan.get('status') == 'needs_input':
            result.append(f'Record {ref} still needs a model/user decision: {plan["reason"]}')
            continue
        if evidence.get(ref, {}).get('fact_version') != record_version(state, ref):
            result.append(f'Record {ref} has no current verified completion evidence.')
    return result


def ready_to_advance(fields, state, page=None):
    """Whether a work/education page may go to Next without another model turn.

    Every candidate record of a kind this page shows must be complete (bound, every peer field read
    back against its fact or saved decision) or explicitly omitted by a still-valid model plan. A
    record not yet on the page (its row never added) is pending, so a page showing one of two jobs
    does not advance however complete its visible fields are. Pending, needs_input, conflicting or
    unread records all block. Evidence only; which records belong is the model's plan.
    """
    kinds = exposed_kinds(fields, state, page)
    rows = [row for row in coverage(fields, state) if row['record'].split(':')[0] in kinds]
    return bool(kinds and rows) and all(row['plan'] == 'omit' or row['complete'] for row in rows)
