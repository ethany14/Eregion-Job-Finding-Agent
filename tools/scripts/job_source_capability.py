"""Job source capability metrics from saved search results (offline; makes no requests).

Layer 1 of the JobSpy validation: with sources chosen explicitly, compare what each source
returns for the same fixed query — valid postings, postings unique to the source, full
description coverage, which filters were actually applied, request counts and time. It does
not score fit and says nothing about whether a model would choose the source (layer 2).

Input: a JSON file {"query": ..., "sources": {name: {"rows": [...], "requests": n,
"elapsed_s": t, "applied_filters": {...}, "unapplied_filters": [...]}}}.
Usage: python tools/scripts/job_source_capability.py INPUT.json [OUTPUT.json]
"""
import json
import re
import sys
from pathlib import Path

FULL_DESCRIPTION_CHARS = 500


def identity(row):
    """Deterministic posting identity: the canonical URL key when available."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
        from eregion.runtime.career.alerts import job_key
        return job_key(row.get('url') or row.get('job_url') or '')
    except Exception:
        return (row.get('url') or row.get('job_url') or '').split('?')[0].rstrip('/').casefold()


def loose(row):
    """Same title and company. Reported separately: never used to merge across sites."""
    clean = lambda text: re.sub(r'\W+', ' ', (text or '').casefold()).strip()
    return clean(row.get('title')), clean(row.get('company'))


def valid(row):
    return bool((row.get('url') or row.get('job_url')) and row.get('title') and row.get('company'))


def metrics(data):
    sources = data['sources']
    rows = {name: [r for r in src.get('rows', []) if valid(r)] for name, src in sources.items()}
    ids = {name: {identity(r) for r in found} for name, found in rows.items()}
    titles = {name: {loose(r) for r in found} for name, found in rows.items()}
    report = {'query': data.get('query'), 'sources': {}}
    for name, src in sources.items():
        others_ids = set().union(*(v for k, v in ids.items() if k != name)) if len(ids) > 1 else set()
        others_titles = set().union(*(v for k, v in titles.items() if k != name)) if len(titles) > 1 else set()
        found = rows[name]
        descriptions = [len((r.get('description') or '').strip()) for r in found]
        report['sources'][name] = {
            'returned': len(src.get('rows', [])), 'valid': len(found),
            'missing_company': sum(1 for r in src.get('rows', []) if not r.get('company')),
            'unique_by_identity': len(ids[name] - others_ids),
            'shared_title_company_with_others': len(titles[name] & others_titles),
            'full_description': sum(1 for n in descriptions if n >= FULL_DESCRIPTION_CHARS),
            'missing_description': sum(1 for n in descriptions if n == 0),
            'direct_apply_url': sum(1 for r in found if r.get('apply_url') or r.get('job_url_direct')),
            'applied_filters': src.get('applied_filters', {}), 'unapplied_filters': src.get('unapplied_filters', []),
            'requests': src.get('requests'), 'elapsed_s': src.get('elapsed_s'),
        }
    return report


if __name__ == '__main__':
    data = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    result = metrics(data)
    text = json.dumps(result, indent=1, ensure_ascii=False)
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(text, encoding='utf-8')
    print(text)
