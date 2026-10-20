"""Model-selected detail scope; full state remains authoritative for validation."""
import re
import json
from copy import deepcopy


def field_delta(previous, current, revision):
    """Exact changes; normal model observations stay self-contained after compaction."""
    if not previous:
        return {'kind': 'full', 'revision': revision, 'fields': deepcopy(current)}
    before = {f['key']: f for f in previous['fields']}
    after = {f['key']: f for f in current}
    delta = {'kind': 'delta' if previous['fields'] != current else 'unchanged',
            'base_revision': previous['revision'], 'revision': revision,
            'removed': [k for k in before if k not in after],
            'changed': [deepcopy(f) for f in current if before.get(f['key']) != f],
            'order': [f['key'] for f in current]}
    full = {'kind': 'full', 'revision': revision, 'fields': deepcopy(current)}
    size = lambda value: len(json.dumps(value, ensure_ascii=False).encode('utf-8'))
    return full if delta['kind'] == 'delta' and size(delta) >= size(full) * .7 else delta


def apply_field_delta(previous, delta):
    if delta['kind'] == 'full':
        return {'revision': delta['revision'], 'fields': deepcopy(delta['fields'])}
    if not previous or previous['revision'] != delta['base_revision']:
        raise ValueError('Observation baseline missing; request a full snapshot.')
    fields = {f['key']: deepcopy(f) for f in previous['fields'] if f['key'] not in delta['removed']}
    fields.update({f['key']: deepcopy(f) for f in delta['changed']})
    return {'revision': delta['revision'], 'fields': [fields[k] for k in delta['order']]}


def record_identity(field):
    # Browser identity only, never infer which candidate record belongs here.
    identifier = field.get('dom_id') or ''
    match = re.match(r'((?:workExperience|education)-[^-]+)--', identifier)
    return match[1] if match else None


def control_input(field):
    """Explain observed browser mechanics, never candidate meaning or an answer."""
    kind = field['kind']
    if kind == 'checkbox':
        return 'checkbox: save your evidence-backed yes/no decision; binding alone is not a decision, except to a listed current_status'
    if kind in {'select', 'radio'}:
        return 'choice: use listed options directly; inspect only when the needed options are not visible'
    if field.get('choice_control') or kind in {'combobox', 'location'}:
        return 'picker: typing is only a search query, not a selected value; select an observed option or save another grounded decision'
    if kind in {'text', 'textarea', 'email', 'tel', 'number', 'url', 'date'}:
        return 'literal: fill from a bound fact; no dropdown exists, do not search_options or inspect_field'
    return 'other: follow the observed control kind and constraints'


def focus_observation(snapshot, state, fields):
    scope = state.get('observation_scope')
    page = snapshot.get('page') or {}
    if not scope or state.get('observation_scope_page') != [page.get('url'), page.get('heading')]:
        return snapshot
    selected = set(scope) & {f['key'] for f in fields}
    if not selected:
        return snapshot  # Changed page/removed controls must never hide all fields.
    records = {record_identity(f) for f in fields if f['key'] in selected} - {None}
    selected.update(f['key'] for f in fields if record_identity(f) in records)
    # Keep peers bound by a model to the same record even if DOM record IDs are absent.
    for name in ('experience_bindings', 'education_bindings'):
        bindings = state.get(name, {})
        refs = {str(bindings[k].get('source_ref','')).rsplit(':',1)[0] for k in selected if k in bindings}
        selected.update(k for k,b in bindings.items() if str(b.get('source_ref','')).rsplit(':',1)[0] in refs)
    outside = [f for f in snapshot['fields'] if f['key'] not in selected]
    index = {f['key']: f for f in snapshot.get('field_index', []) if f['key'] not in selected}
    index.update({f['key']: {k:f[k] for k in ('key','label','kind','done','required','invalid') if k in f} for f in outside})
    snapshot['field_index'] = list(index.values())
    snapshot['fields'] = [f for f in snapshot['fields'] if f['key'] in selected]
    # Keep detailed decisions for this row only. Global record coverage/blockers
    # remain present; full detail is available again through focus_fields([]).
    snapshot['field_decisions'] = {k:v for k,v in snapshot.get('field_decisions',{}).items() if k in selected}
    snapshot['completed_fields'] = [k for k in snapshot.get('completed_fields',[]) if k in selected]
    snapshot['record_rows'] = [row if set(row.get('field_keys', [])) & selected else
        {k:v for k,v in row.items() if k != 'field_keys'} for row in snapshot.get('record_rows',[])]
    snapshot['observation_scope'] = {'field_keys':sorted(selected), 'outside_count':len(index),
        'next':'Use focus_fields with index keys for another record, or [] for the full page. Scope success is not page completion.'}
    # Raw page text repeats out-of-scope controls; retain navigation and all structured blockers.
    snapshot['page'] = {k:v for k,v in page.items() if k != 'text'}
    return focus_evidence(snapshot, state, fields, selected)


RECORD_KINDS = ('experience', 'education')
RECORD_IDENTITY = {'experience': ('title', 'company'), 'education': ('school', 'degree')}


def fact_record(ref):
    parts = str(ref or '').split(':')
    return ':'.join(parts[:2]) if len(parts) > 2 and parts[0] in RECORD_KINDS else None


def focus_evidence(snapshot, state, fields, selected):
    """Evidence closure of the scoped fields; every other fact stays one focus_fields([]) away.

    In full detail: profile facts; every fact of a record bound to or cited by a scoped field; every
    record of a kind when a scoped record row is still unbound (the model chooses among them); answers
    for scoped fields, cited by their decisions or bound by user messages; materials when a scoped field
    is open-ended or cites them. Other records keep their identity in an index. User messages, record
    coverage and blockers are never scoped.
    """
    from .agent import NARRATIVE
    by_key = {f['key']: f for f in fields}
    bindings = {**state.get('education_bindings', {}), **state.get('experience_bindings', {})}
    decisions = state.get('field_decisions') or {}
    records, open_kinds, cited = set(), set(), set()
    for key in selected:
        if key in bindings:
            records.add(fact_record(bindings[key].get('source_ref')))
        for ref in (decisions.get(key) or {}).get('sources') or {}:
            cited.add(ref)
            records.add(fact_record(ref))
        identity = record_identity(by_key[key]) if key in by_key else None
        if identity and key not in bindings:
            open_kinds.add('experience' if identity.startswith('workExperience') else 'education')
    answer_keys = set(selected) | {ref.removeprefix('answer:') for ref in cited if ref.startswith('answer:')}
    for message in snapshot.get('user_messages', []):
        answer_keys.update((message.get('bound_answers') or {}).keys())
    open_question = any(by_key[k].get('kind') == 'textarea' or NARRATIVE.search(by_key[k].get('label') or '')
                        for k in selected if k in by_key)

    def needed(fact):
        ref = fact['ref']
        kind = ref.split(':', 1)[0]
        if kind == 'profile':
            return True
        if kind in RECORD_KINDS:
            return fact_record(ref) in records or kind in open_kinds
        if kind == 'answer':
            return ref.removeprefix('answer:') in answer_keys
        return open_question or ref in cited

    kept, index, others = [], {}, []
    for fact in snapshot.get('facts', []):
        if needed(fact):
            kept.append(fact)
            continue
        record = fact_record(fact['ref'])
        if record:
            entry = index.setdefault(record, {'record': record})
            component = fact['ref'].split(':', 2)[2]
            if component in RECORD_IDENTITY[record.split(':')[0]]:
                entry[component] = fact['value'][:120] if isinstance(fact['value'], str) else fact['value']
        else:
            others.append(fact['ref'])
    if index or others:
        snapshot['facts'] = kept
        snapshot['facts_outside_scope'] = {'records': list(index.values()), 'refs': others,
            'read': 'Only identities are shown for these. focus_fields([]) (or focusing their fields) shows their full facts; '
                    'read a value before citing it.'}
    resume = snapshot.get('resume_context') or {}
    if resume.get('text') and not open_question:
        snapshot['resume_context'] = {**{k: v for k, v in resume.items() if k != 'text'},
            'text_omitted': 'Included when a scoped field is open-ended; focus_fields([]) shows the whole page.'}
    previous = snapshot.get('previous_page_answers') or []
    if previous:
        snapshot['previous_page_answers'] = []
        snapshot['previous_page_answers_outside_scope'] = {'count': len(previous), 'read': 'focus_fields([]) shows them.'}
    return snapshot


def observation_messages(messages):
    """Keep user evidence independent of progress chatter; never summarize its meaning."""
    users = [i for i,m in enumerate(messages) if m.get('role') == 'user']
    selected = set(users[-12:] + users[:1])
    selected.update(range(max(0,len(messages)-4),len(messages)))
    ids = {m.get('id'):i for i,m in enumerate(messages)}
    for i in list(selected):
        m=messages[i]
        if m.get('role')!='user': continue
        reply=ids.get(m.get('in_reply_to'))
        if reply is not None and reply<i: selected.add(reply)
        else:
            # Bare/numbered replies need the preceding question, not a progress note.
            for j in range(i-1,-1,-1):
                if messages[j].get('role')=='user': break
                if messages[j].get('question_keys') or messages[j].get('kind')=='question':
                    selected.add(j);break
    return [messages[i] for i in sorted(selected)]


def answer_verification_context(state, page, fields):
    """Bounded source context, with no inferred location or eligibility facts."""
    return {'application_url':state.get('url'),
        'opening_page':state.get('opening_page_evidence'),
        'current_page':{'url':(page or {}).get('url'),'heading':(page or {}).get('heading')},
        'questions':[{'label':f.get('label','')[:1000],'group':f.get('group','')[:500],
                      **({'help':f['help'][:1500]} if f.get('help') else {})} for f in fields],
        'user_messages':[{'id':m['id'],'text':m['text']} for m in observation_messages(state.get('messages',[])) if m.get('role')=='user']}


def record_rows(fields, state, completed_keys):
    """Expose DOM row membership; never assign a candidate or choose an answer."""
    from .record_coverage import binding_ref
    rows = {}
    completed = set(completed_keys)
    for field in fields:
        identity = record_identity(field)
        if identity is None:
            continue
        row = rows.setdefault(identity, {'row_id': identity, 'field_keys': [],
            'pending_field_keys': [], 'bound_records': []})
        row['field_keys'].append(field['key'])
        if field['key'] not in completed:
            row['pending_field_keys'].append(field['key'])
        ref = binding_ref(field, state)
        if ref and ref not in row['bound_records']:
            row['bound_records'].append(ref)
    for row in rows.values():
        row['batch_tool'] = 'apply_record' if len(row['bound_records']) <= 1 else None
    return list(rows.values())
