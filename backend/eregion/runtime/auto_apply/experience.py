"""Work-experience records from the resume, cited as experience:<id>:<component> facts.

The model decides which form entry a record belongs to and cites the record's component
when it fills a field; this module only stores, normalizes and exposes the evidenced facts
(spec W2/§13: the Adobe pilot had to type both jobs in chat because none were imported).
"""
import hashlib
import json

COMPONENTS = ('title', 'company', 'location', 'start_date', 'end_date', 'current', 'description')
LIMITS = {'description': 2000}


def normalize_records(records):
    result, seen, ids = [], set(), set()
    for record in records or []:
        value = {k: str(record.get(k) or '').strip()[:LIMITS.get(k, 300)] for k in COMPONENTS}
        if not (value['title'] or value['company']):
            continue
        fingerprint = json.dumps({k: value[k] for k in COMPONENTS if k != 'description'}, sort_keys=True)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        identifier = record.get('id') or 'exp-' + hashlib.sha256(fingerprint.encode()).hexdigest()[:16]
        if identifier in ids:
            identifier += '-' + str(len(result))
        ids.add(identifier)
        result.append({'id': identifier, **value})
    return result


def facts(profile, overrides=None):
    """Every stated component, plus month/year parts of stated dates."""
    from .education import date_parts
    result = {}
    for record in profile.get('experience') or []:
        values = {k: record.get(k, '') for k in COMPONENTS}
        confirmed = {k: item['value'] for k, item in (overrides or {}).get(record['id'], {}).items() if item.get('value') is not None}
        values.update({k: v for k, v in confirmed.items() if k in COMPONENTS})
        for part in ('start', 'end'):
            values.update({f'{part}_{k}': v for k, v in date_parts(values[f'{part}_date']).items()})
        values.update(confirmed)
        for component, value in values.items():
            if value is not None and value != "":
                result[f"experience:{record['id']}:{component}"] = value
    return result


BINDING_COMPONENTS = (*COMPONENTS, 'start_month', 'start_year', 'end_month', 'end_year')


def binding(field, state):
    saved = state.get('experience_bindings', {}).get(field['key'])
    if saved and all(saved['field'].get(k) == field.get(k) for k in ('key', 'label', 'kind', 'group')):
        return saved
    return None


def bound_value(field, state):
    saved = binding(field, state)
    if not saved:
        return None
    # A textual employment fact is not a boolean decision. The model must interpret it.
    if field['kind'] == 'checkbox':
        return None
    from .education import format_date
    return format_date(field, facts(state['profile'], state.get('experience_answer_overrides')).get(saved['source_ref']),
                       saved['component'].rsplit('_', 1)[-1])


def coverage(state):
    """Advisory record coverage, not a semantic completion decision or navigation gate."""
    bindings = state.get('experience_bindings', {})
    available = facts(state['profile'], state.get('experience_answer_overrides'))
    return [{'id': r['id'], 'company': r.get('company', ''),
             'available_components': [ref.rsplit(':',1)[-1] for ref in available if ref.startswith(f"experience:{r['id']}:")],
             'bound_fields': [k for k, b in bindings.items() if b['experience_id'] == r['id']]}
            for r in state['profile'].get('experience', [])]


def gaps(state, fields):
    """Verify only model-bound fields; never infer what a job record means."""
    from .autofill import desired_answer, matches
    from .decisions import active_decision
    pending = []
    for field in fields:
        if not binding(field, state):
            continue
        decision = active_decision(field, state)
        if decision and decision['mode'] == 'leave_empty' and not field.get('required'):
            continue
        target = desired_answer(field, state)
        if target is None or not matches(field, target):
            pending.append(f"Resolve work-experience field: {field['label']} ({field['key']})")
    return pending
