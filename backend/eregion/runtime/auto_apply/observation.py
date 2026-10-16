"""Model-selected detail scope; full state remains authoritative for validation."""
import re


def record_identity(field):
    # Browser identity only, never infer which candidate record belongs here.
    identifier = field.get('dom_id') or ''
    match = re.match(r'((?:workExperience|education)-[^-]+)--', identifier)
    return match[1] if match else None


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
    snapshot['field_index'] = [{k:f[k] for k in ('key','label','kind','done','required','invalid') if k in f} for f in outside]
    snapshot['fields'] = [f for f in snapshot['fields'] if f['key'] in selected]
    snapshot['observation_scope'] = {'field_keys':sorted(selected), 'outside_count':len(outside),
        'next':'Use focus_fields with index keys for another record, or [] for the full page. Scope success is not page completion.'}
    # Raw page text repeats out-of-scope controls; retain navigation and all structured blockers.
    snapshot['page'] = {k:v for k,v in page.items() if k != 'text'}
    return snapshot
