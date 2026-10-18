"""Business-step identity and persistent budgets, separate from observation revisions."""
import re
from eregion.runtime.telemetry import outcome_fingerprint


def step_marker(page):
    page = page or {}
    # Number is stable when total steps or localized labels change.
    text = str(page.get('current_step') or '')
    match = re.search(r'current step (\d+) of (\d+)', text or str(page.get('text') or ''), re.I)
    if match:
        return f'{match[1]}/{match[2]}'
    return text or None


def step_key(page):
    marker = step_marker(page)
    if not marker:
        return None
    number = re.fullmatch(r'(\d+)/\d+', marker)
    return 'step:' + (number[1] if number else marker)


def budget_record(state, page):
    """Return a persisted per-step bucket; missing identity never grants new calls.

    Old single-page counters cannot be reconstructed. Conservatively carry the
    retained counter forward once and preserve it on subsequent revisits.
    """
    saved = state.get('step_budgets') or {}
    counts = dict(saved.get('counts') or {})
    key = step_key(page)
    if key is None:
        key = saved.get('active') or 'unidentified'
    if not counts:
        counts[key] = int((state.get('page_calls') or {}).get('count', 0))
    elif key not in counts:
        counts[key] = counts.get('unidentified', 0) if saved.get('active') == 'unidentified' else 0
    return {'v': 1, 'active': key, 'counts': counts}, {'page': key, 'count': counts[key]}


def structure_signature(page, fields):
    return outcome_fingerprint([step_key(page), (page or {}).get('url'),
        [(f.get('key'), f.get('dom_id'), f.get('kind'), f.get('group')) for f in fields],
        [(c.get('id'), c.get('kind'), c.get('label')) for c in (page or {}).get('controls', [])]])
