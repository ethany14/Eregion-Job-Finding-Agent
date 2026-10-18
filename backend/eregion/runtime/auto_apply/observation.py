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
        return 'checkbox: save your evidence-backed yes/no decision; binding alone is not a decision'
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
