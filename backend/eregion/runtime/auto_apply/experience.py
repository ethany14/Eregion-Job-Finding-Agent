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


def facts(profile):
    """Every stated component, plus month/year parts of stated dates."""
    from .education import date_parts
    result = {}
    for record in profile.get('experience') or []:
        values = {k: record.get(k, '') for k in COMPONENTS}
        for part in ('start', 'end'):
            values.update({f'{part}_{k}': v for k, v in date_parts(values[f'{part}_date']).items()})
        for component, value in values.items():
            if value:
                result[f"experience:{record['id']}:{component}"] = value
    return result
