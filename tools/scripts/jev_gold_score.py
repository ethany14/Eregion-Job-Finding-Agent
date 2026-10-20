"""Score Jev requirement extractions against source-text annotations (P6, free; no provider call).

Annotations (`gold.py`: GOLD[job] = sections, units, not_required) name each candidate qualification by a
distinctive substring of the posting, with its level, whether it is a personal condition, OR groups and
negation; sections type every part of the posting. A Jev requirement is located in the posting by its
source text; it covers the units its span overlaps. Item by item, never by score closeness:

  recall        annotated, unambiguous qualifications covered by at least one requirement
  extra         requirements covering no unit, by the section type they come from (responsibility,
                company, benefits, legal, context) or a NOT-required span; unannotated = inside a
                qualification/logistics section but no unit (an annotation gap or a split fragment)
  level         covered units whose requirement level differs from the annotation
  personal      annotated personal conditions covered AND marked for user confirmation
  or_groups     alternatives kept together (one requirement, or one requirement_group_id) vs split
Ambiguous units count neither way.

Usage: jev_gold_score.py --gold DIR/gold.py --saved DIR/saved-jev.json [--extra NAME=FILE:JOB ...] --out FILE
  --extra adds another extraction of one job (a JSON file with job.requirements, e.g. a T6 arm result).
"""
import argparse
import importlib.util
import json
import re
from pathlib import Path

_DASHES = str.maketrans({'‑': '-', '–': '-', '—': '-', '’': "'", '‘': "'", '“': '"', '”': '"', ' ': ' '})


def norm(text):
    return ' '.join(str(text or '').translate(_DASHES).replace('**', '').casefold().split())


def span(haystack, text):
    needle = norm(text)
    if not needle:
        return None
    start = haystack.find(needle)
    if start < 0 and len(needle) > 60:  # a requirement may rephrase its tail; anchor on the head
        start, needle = haystack.find(needle[:60]), needle[:60]
    return (start, start + len(needle)) if start >= 0 else None


def overlap(a, b):
    return a and b and a[0] < b[1] and b[0] < a[1]


def line_ends(jd):
    """End offsets, in the normalized document, of each source line."""
    ends, position = [], -1
    for line in jd.split('\n'):
        text = norm(line)
        if text:
            position += len(text) + 1
            ends.append(position)
    return ends


def score(jd, gold, requirements):
    doc = norm(jd)
    ends = line_ends(jd)
    marks = sorted((s[0], t) for marker, t in gold['sections'] if (s := span(doc, marker)))
    def section_of(pos):
        kind = 'unsectioned'
        for start, t in marks:
            if start <= pos:
                kind = t
        return kind
    units = []
    for unit in gold['units']:
        # A unit names its qualification by a distinctive part; it covers the rest of its source line, so a
        # requirement quoting only the line's second half still covers it.
        where = span(doc, unit['text'])
        if where:
            where = (where[0], max(where[1], next((end for end in ends if end >= where[1]), where[1])))
        units.append({**unit, 'span': where})
    missing = [u['text'] for u in units if u['span'] is None]
    not_required = [s for text in gold.get('not_required', []) if (s := span(doc, text))]
    reqs = []
    for req in requirements:
        where = None
        for field in ('source_text', 'original_text', 'atomic_text', 'display_name'):
            where = span(doc, req.get(field))
            if where:
                break
        covered = [i for i, u in enumerate(units) if overlap(where, u['span'])]
        personal = req.get('category') == 'eligibility' or req.get('verification_mode') == 'user_confirmation'
        reqs.append({'text': (req.get('atomic_text') or '')[:120], 'level': req.get('level'), 'group': req.get('requirement_group_id'),
                     'personal': personal, 'span': where, 'covered': covered,
                     'section': section_of(where[0]) if where else None,
                     'not_required': any(overlap(where, s) for s in not_required)})
    firm = [i for i, u in enumerate(units) if not u['ambiguous'] and u['span']]
    covered_by = {i: [r for r in reqs if i in r['covered']] for i in firm}
    extra = {}
    for r in reqs:
        if r['not_required']:
            kind = 'not_required'
        elif r['covered']:
            continue
        elif r['span'] is None:
            kind = 'not_located'
        elif r['section'] in {'required', 'preferred', 'logistics'}:
            kind = 'unannotated'
        else:
            kind = r['section']
        extra.setdefault(kind, []).append(r['text'])
    level_errors = [{'unit': units[i]['text'][:80], 'gold': units[i]['level'], 'jev': sorted({r['level'] for r in rs})}
                    for i, rs in covered_by.items() if rs and any(r['level'] != units[i]['level'] for r in rs)]
    personal_units = [i for i in firm if units[i]['personal']]
    groups = {}
    for i in firm:
        if units[i]['or_group']:
            groups.setdefault(units[i]['or_group'], []).append(i)
    or_result = {}
    for name, members in groups.items():
        found = [r for i in members for r in covered_by[i]]
        together = len({id(r) for r in found}) == 1 or len({r['group'] for r in found}) == 1
        or_result[name] = 'not_extracted' if not found else 'kept_together' if together else 'split'
    return {
        'annotation_misses': missing,
        'requirements': len(reqs),
        'recall': {'covered': sum(bool(covered_by[i]) for i in firm), 'of': len(firm),
                   'missed': [units[i]['text'][:80] for i in firm if not covered_by[i]]},
        'recall_by_level': {lvl: {'covered': sum(bool(covered_by[i]) for i in firm if units[i]['level'] == lvl),
                                  'of': sum(units[i]['level'] == lvl for i in firm)} for lvl in ('required', 'preferred')},
        'extra': {k: {'count': len(v), 'examples': v[:4]} for k, v in sorted(extra.items())},
        'level_errors': level_errors,
        'personal': {'recognized': sum(any(r['personal'] for r in covered_by[i]) for i in personal_units),
                     'covered': sum(bool(covered_by[i]) for i in personal_units), 'of': len(personal_units),
                     'not_flagged': [units[i]['text'][:80] for i in personal_units if covered_by[i] and not any(r['personal'] for r in covered_by[i])]},
        'or_groups': or_result,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--gold', required=True)
    parser.add_argument('--saved', required=True)
    parser.add_argument('--extra', action='append', default=[])
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('gold', args.gold)
    gold = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gold)
    rows = json.loads(Path(args.saved).read_text(encoding='utf-8'))
    runs = [{'run': f'saved:{i}', 'job': i, 'title': row['title'],
             'result': score(row['jd'], gold.GOLD[i], row['job']['requirements'])} for i, row in enumerate(rows) if i in gold.GOLD]
    for item in args.extra:
        name, rest = item.split('=', 1)
        path, job = rest.rsplit(':', 1)
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        requirements = data['results'][next(iter(data['results']))]['job']['requirements'] if 'results' in data else data['job']['requirements']
        runs.append({'run': name, 'job': int(job), 'title': rows[int(job)]['title'],
                     'result': score(rows[int(job)]['jd'], gold.GOLD[int(job)], requirements)})
    saved = [r['result'] for r in runs if r['run'].startswith('saved:')]
    total = lambda f: sum(f(r) for r in saved)
    extra_kinds = sorted({k for r in saved for k in r['extra']})
    summary = {
        'jobs': len(saved), 'requirements': total(lambda r: r['requirements']),
        'recall': f"{total(lambda r: r['recall']['covered'])}/{total(lambda r: r['recall']['of'])}",
        'recall_required': f"{total(lambda r: r['recall_by_level']['required']['covered'])}/{total(lambda r: r['recall_by_level']['required']['of'])}",
        'recall_preferred': f"{total(lambda r: r['recall_by_level']['preferred']['covered'])}/{total(lambda r: r['recall_by_level']['preferred']['of'])}",
        'extra': {k: total(lambda r: r['extra'].get(k, {}).get('count', 0)) for k in extra_kinds},
        'level_errors': total(lambda r: len(r['level_errors'])),
        'level_errors_by_gold': {lvl: total(lambda r: sum(e['gold'] == lvl for e in r['level_errors'])) for lvl in ('required', 'preferred')},
        'personal_recognized': f"{total(lambda r: r['personal']['recognized'])}/{total(lambda r: r['personal']['of'])} "
                               f"(covered {total(lambda r: r['personal']['covered'])})",
        'or_groups': [g for r in saved for g in r['or_groups'].values()],
        'annotation_misses': total(lambda r: len(r['annotation_misses'])),
    }
    Path(args.out).write_text(json.dumps({'summary': summary, 'runs': runs}, indent=1, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(summary, indent=1, ensure_ascii=False))


if __name__ == '__main__':
    main()
