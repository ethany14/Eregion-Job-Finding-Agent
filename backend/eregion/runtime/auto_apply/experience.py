"""Work-experience records from the resume, cited as experience:<id>:<component> facts.

The model decides which form entry a record belongs to and cites the record's component
when it fills a field; this module only stores, normalizes and exposes the evidenced facts
(spec W2/§13: the Adobe pilot had to type both jobs in chat because none were imported).
"""
import hashlib
import json
import re

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


_CURRENT = {'present', 'current', 'currently', 'now', 'ongoing', 'to date', 'true', 'yes', 'y'}
_NOT_CURRENT = {'false', 'no', 'n'}
_ONGOING_END = {'present', 'current', 'now', 'ongoing', 'to date'}
_UNSETTLED = re.compile(r'(expected|anticipated|projected|planned|future|tbd)', re.I)


def _marker(text):
    value = ' '.join(str(text or '').casefold().split())
    return None if not value else True if value in _CURRENT else False if value in _NOT_CURRENT else 'unreadable'


def _past(end_date, now):
    """True for an end date before this month (or before this year when no month is written)."""
    from .education import date_parts
    if not end_date or _UNSETTLED.search(end_date):
        return None
    parts = date_parts(end_date)
    if 'year' not in parts:
        return None
    year = int(parts['year'])
    if 'month' in parts:
        return (year, int(parts['month'])) < (now.year, now.month)
    return True if year < now.year else None if year == now.year else False


def current_status(values, *, now):
    """(bool, basis) for a current-employment checkbox, or (None, None).

    True only for an explicit current marker ('Present', or a confirmed yes); False for an explicit
    no or an end date that has passed. A missing end date is not "current", a future or expected end
    date is undecided, and a marker contradicting the end date is a conflict: those stay with the
    model or the user. Only the representation changes; nothing is interpreted from free text.
    """
    stated = _marker(values.get('current'))
    end = str(values.get('end_date') or '').strip()
    if stated == 'unreadable':
        return None, None
    ongoing = stated is True or ' '.join(end.casefold().split()) in _ONGOING_END
    past = None if ' '.join(end.casefold().split()) in _ONGOING_END else _past(end, now)
    if ongoing:
        return (None, None) if stated is False or past is True else (True, 'current_marker')
    if stated is False:
        return False, 'stated_not_current'
    return (False, 'past_end_date') if past is True else (None, None)


def current_basis(profile, overrides, record_id):
    """Why current_status holds for one record ('current_marker', 'past_end_date', ...), or None."""
    record = next((r for r in profile.get('experience') or [] if r.get('id') == record_id), None)
    if record is None:
        return None
    values = {k: record.get(k, '') for k in COMPONENTS}
    values.update({k: item['value'] for k, item in (overrides or {}).get(record_id, {}).items()
                   if item.get('value') is not None and k in COMPONENTS})
    return current_status(values, now=_now(profile))[1]


def _now(profile):
    from datetime import date
    from .agent import current_clock  # imported here: agent imports this module
    return date.fromisoformat(current_clock(profile)['date'])


def facts(profile, overrides=None):
    """Every stated component, plus month/year parts of stated dates, and current_status when an
    explicit marker or a past end date determines it (see current_status)."""
    from .education import date_parts
    result = {}
    for record in profile.get('experience') or []:
        values = {k: record.get(k, '') for k in COMPONENTS}
        confirmed = {k: item['value'] for k, item in (overrides or {}).get(record['id'], {}).items() if item.get('value') is not None}
        values.update({k: v for k, v in confirmed.items() if k in COMPONENTS})
        for part in ('start', 'end'):
            values.update({f'{part}_{k}': v for k, v in date_parts(values[f'{part}_date']).items()})
        values.update(confirmed)
        status, _ = current_status(values, now=_now(profile))
        if status is not None:
            values['current_status'] = status
        for component, value in values.items():
            if value is not None and value != "":
                result[f"experience:{record['id']}:{component}"] = value
    return result


BINDING_COMPONENTS = (*COMPONENTS, 'start_month', 'start_year', 'end_month', 'end_year', 'current_status')


def binding(field, state):
    saved = state.get('experience_bindings', {}).get(field['key'])
    if saved and all(saved['field'].get(k) == field.get(k) for k in ('key', 'label', 'kind', 'group')):
        return saved
    return None


def bound_value(field, state):
    saved = binding(field, state)
    if not saved:
        return None
    # A textual employment fact is not a boolean decision. The model must interpret it; only the
    # derived current_status (an explicit marker or a past end date) drives a checkbox directly.
    if (field['kind'] == 'checkbox') != (saved['component'] == 'current_status'):
        return None
    if field['kind'] == 'checkbox':
        value = facts(state['profile'], state.get('experience_answer_overrides')).get(saved['source_ref'])
        return value if isinstance(value, bool) else None
    from .education import format_date
    return format_date(field, facts(state['profile'], state.get('experience_answer_overrides')).get(saved['source_ref']),
                       saved['component'].rsplit('_', 1)[-1])


# Workday's structural automation ids for the controls of one work row (ids, not label wording).
ROW_COMPONENTS = {'jobTitle': 'title', 'companyName': 'company', 'location': 'location',
                  'roleDescription': 'description', 'currentlyWorkHere': 'current_status', 'current': 'current_status',
                  'startDate-dateSectionMonth-input': 'start_month', 'startDate-dateSectionYear-input': 'start_year',
                  'endDate-dateSectionMonth-input': 'end_month', 'endDate-dateSectionYear-input': 'end_year'}


def inherited_bindings(fields, state):
    """Bindings for controls that appear in a work row the model already bound to one record.

    Unchecking "I currently work here" reveals the end date; the row's owner is already the model's
    saved choice, so a revealed control whose structural id names a component that record has is
    bound to it, marked origin=row_structure. Nothing is inherited for a row bound to several records,
    a field with a saved decision or binding, an unknown id, a missing fact, or a checkbox other than
    current_status. Which record a row is stays the model's decision.
    """
    from .decisions import active_decision, signature
    from .observation import record_identity
    owners = {}
    for field in fields:
        saved, row = binding(field, state), record_identity(field)
        if saved and row and row.startswith('workExperience-'):
            owners.setdefault(row, set()).add(saved['experience_id'])
    if not owners:
        return {}
    available = facts(state['profile'], state.get('experience_answer_overrides'))
    existing = state.get('experience_bindings', {})
    result = {}
    for field in fields:
        row = record_identity(field)
        if not row or len(owners.get(row, ())) != 1 or field['key'] in existing or active_decision(field, state):
            continue
        component = ROW_COMPONENTS.get((field.get('dom_id') or '')[len(row) + 2:])
        if not component or (component == 'current_status') != (field['kind'] == 'checkbox'):
            continue
        record = next(iter(owners[row]))
        ref = f'experience:{record}:{component}'
        if ref in available:
            result[field['key']] = {'experience_id': record, 'component': component, 'source_ref': ref,
                                    'field': signature(field), 'origin': 'row_structure'}
    return result


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
