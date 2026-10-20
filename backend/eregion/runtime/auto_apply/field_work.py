"""What the observed fields need, for metering: classification only, never a decision or a gate.

done        the page holds the grounded target (or a verified explicit omission)
direct      a grounded target exists and code can write it without interpretation
model       needs a model decision: a record row to bind or map, a checkbox, an open question
unsourced   required, with no grounded source: a personal fact to ask for, or a mapping to decide
optional    optional and empty, with no grounded source: answer or leave empty is a decision
failed      the website rejects a value it holds, or the last write did not hold
"""
import re

from .autofill import desired_answer, matches
from .decisions import verified_empty_decision
from .observation import record_identity
from .record_coverage import binding_ref

KINDS = ('done', 'direct', 'model', 'unsourced', 'optional', 'failed')
_ELIGIBILITY = re.compile(r"\b(authori[sz]\w*|sponsor\w*|visa|citizen\w*|eligib\w*|clearance|right to work)\b", re.I)


def classify(fields, state, failed=frozenset()):
    work = {kind: [] for kind in KINDS}
    for field in fields:
        key = field['key']
        # An empty required field the site already flags is missing, not a failed write.
        if key in failed or (field.get('invalid') and field.get('value') not in ('', None, [], False)):
            work['failed'].append(key)
            continue
        if verified_empty_decision(field, state):
            work['done'].append(key)
            continue
        target = desired_answer(field, state)
        if target is not None:
            work['done' if matches(field, target) else 'direct'].append(key)
        elif field['kind'] == 'file':
            work['done' if field.get('value') else 'unsourced'].append(key)
        elif record_identity(field) or binding_ref(field, state) or field['kind'] in {'checkbox', 'textarea'}:
            work['model'].append(key)
        else:
            work['unsourced' if field.get('required') else 'optional'].append(key)
    return work


def turn_purpose(snapshot, fields, state, failed=frozenset()):
    """A descriptive label for a model turn's usage record, from what the page still needs."""
    if snapshot.get('recovery') or snapshot.get('runtime_feedback'):
        return 'recovery'
    work = classify(fields, state, failed)
    by_key = {field['key']: field for field in fields}
    open_keys = work['model'] + work['unsourced']
    if any(record_identity(by_key[key]) and not binding_ref(by_key[key], state) for key in work['model']):
        return 'record_binding'
    if any(by_key[key]['kind'] == 'textarea' for key in work['model']):
        return 'open_question'
    if any(_ELIGIBILITY.search(' '.join(str(by_key[key].get(part) or '') for part in ('label', 'group', 'help')))
           for key in open_keys):
        return 'eligibility'
    if open_keys or work['optional'] or work['failed']:
        return 'semantic_mapping'
    return 'coordination'
