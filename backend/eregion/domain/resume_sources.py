"""Preserve source headers and qualifiers in the shared evidence stream."""
import re
from eregion.domain.schemas import ResumeEvidence, ResumeSourceEntry


def complete_source_evidence(analysis, original):
    """Add omitted education header lines from the source, without inventing facts.

    Matching and writing both consume evidence. Keeping a degree only in metadata
    made it invisible to those consumers. Quote complete source lines so qualifiers
    such as expected, incomplete, or coursework-only remain attached to the degree.
    IDs are temporary here; the normal ownership normalizer assigns stable IDs.
    """
    analysis = _repair_unowned_education(analysis, original)
    evidence, entries = list(analysis.evidence), []
    for index, entry in enumerate(analysis.source_entries):
        ids = list(entry.evidence_ids)
        if entry.entry_type == 'education':
            known = {e.exact_text for e in evidence if e.evidence_id in ids}
            spans = []
            occurrences = {}
            for field in ('organization', 'heading', 'start_date', 'end_date'):
                value = getattr(entry, field)
                if value:
                    pattern = r'\s+'.join(re.escape(p) for p in value.split())
                    occurrences[field] = list(re.finditer(pattern, original, re.I))
            candidates = occurrences.get('organization') or occurrences.get('heading') or []
            # A school can also be an employer, or the same program can occur
            # twice. Select the occurrence closest to ALL this entry's metadata.
            anchor = min(candidates, key=lambda candidate: sum(
                min(abs(other.start() - candidate.start()) for other in found)
                for found in occurrences.values() if found)).start() if candidates else None
            for field in ('organization', 'heading', 'start_date', 'end_date'):
                value = getattr(entry, field)
                if not value:
                    continue
                pattern = r'\s+'.join(re.escape(p) for p in value.split())
                found = list(re.finditer(pattern, original, re.I))
                if not found:
                    continue  # Existing metadata validation still rejects inventions.
                match = min(found, key=lambda m: abs(m.start() - anchor)) if anchor is not None else found[0]
                if anchor is None:
                    anchor = match.start()
                start = original.rfind('\n', 0, match.start()) + 1
                end = original.find('\n', match.end())
                spans.append((start, len(original) if end < 0 else end))
            # Preserve adjacent status/date lines as one quote where possible;
            # never synthesize a completed degree out of separate metadata fields.
            if spans and max(b for a, b in spans) - min(a for a, b in spans) <= 1500:
                spans = [(min(a for a, b in spans), max(b for a, b in spans))]
            for start, end in dict.fromkeys(spans):
                quote = original[start:end].strip()
                if not quote or quote in known:
                    continue
                identifier = f'source-header-{index}-{len(ids)}'
                evidence.append(ResumeEvidence(evidence_id=identifier, source_section='Education',
                    source_entry_id=entry.source_entry_id, exact_text=quote))
                ids.append(identifier)
                known.add(quote)
        entries.append(entry.model_copy(update={'evidence_ids': ids}))
    evidence, entries = _complete_owned_entries(evidence, entries, original)
    # An extraction may keep only part of a long skills paragraph. Preserve the
    # entire explicitly delimited original section as one owned source instead
    # of depending on which tool names the model happened to quote.
    from eregion.domain.resume_headers import SECTIONS, SKILL_SECTIONS, section_name
    lines = original.splitlines(keepends=True)
    blocks, position = [], 0
    start = None
    for line in lines:
        name = section_name(line)
        if name in SECTIONS | SKILL_SECTIONS:
            if start is not None and position > start:
                blocks.append(original[start:position].strip())
                start = None
            if name in SKILL_SECTIONS:
                start = position + len(line)
        position += len(line)
    if start is not None:
        blocks.append(original[start:].strip())
    blocks = [block for block in blocks if block]
    if blocks:
        removed = {entry.source_entry_id for entry in entries if entry.entry_type == 'skills'}
        removed_ids = {key for entry in entries if entry.source_entry_id in removed for key in entry.evidence_ids}
        entries = [entry for entry in entries if entry.source_entry_id not in removed]
        evidence = [item for item in evidence if item.evidence_id not in removed_ids]
        for i, block in enumerate(blocks):
            source_id, evidence_id = f'source-skills-{i}', f'source-skills-evidence-{i}'
            entries.append(ResumeSourceEntry(source_entry_id=source_id, entry_type='skills',
                heading='Skills', evidence_ids=[evidence_id]))
            evidence.append(ResumeEvidence(evidence_id=evidence_id, source_section='Skills',
                source_entry_id=source_id, exact_text=block))
    return analysis.model_copy(update={'evidence': evidence, 'source_entries': entries})


def _repair_unowned_education(analysis, original):
    """Reparse legacy section-wide evidence only when every span has an owner."""
    from eregion.domain.resume_layout import entry_spans
    evidence, entries = list(analysis.evidence), []
    for entry in analysis.source_entries:
        owned = [e for e in evidence if e.evidence_id in entry.evidence_ids]
        if (entry.entry_type != 'education' or entry.organization or entry.start_date
                or entry.end_date or len(owned) != 1 or owned[0].exact_text not in original):
            entries.append(entry)
            continue
        quote = owned[0].exact_text
        spans = entry_spans(quote, 'education')
        if not spans or not all(meta.get('organization') for _, _, meta in spans):
            entries.append(entry)
            continue
        evidence.remove(owned[0])
        for i, (start, end, meta) in enumerate(spans):
            sid, eid = f'{entry.source_entry_id}-education-{i}', f'{owned[0].evidence_id}-education-{i}'
            entries.append(ResumeSourceEntry(source_entry_id=sid, entry_type='education',
                                            evidence_ids=[eid], **meta))
            evidence.append(ResumeEvidence(evidence_id=eid, source_entry_id=sid,
                source_section=owned[0].source_section, exact_text=quote[start:end].strip()))
    return analysis.model_copy(update={'evidence': evidence, 'source_entries': entries})


def _complete_owned_entries(evidence, entries, original):
    """Keep full owned entry context, including units and limitations.

    Source coordinates are found using verbatim metadata. Ambiguous/unanchored
    entries retain their existing evidence; no guessed ownership is introduced.
    Each replacement quote remains a contiguous slice of the original text.
    """
    def occurrences(value):
        if not value:
            return []
        return list(re.finditer(r'\s+'.join(re.escape(p) for p in value.split()), original, re.I))
    anchors = {}
    for index, entry in enumerate(entries):
        found = {key: occurrences(getattr(entry,key)) for key in
                 ('heading','organization','location','start_date','end_date')}
        if entry.entry_type == 'summary' and not any(found.values()):
            section = next((e.source_section for e in evidence if e.evidence_id in entry.evidence_ids), '')
            found['heading'] = occurrences(section)
        candidates = found['organization'] or found['heading']
        if not candidates:
            continue
        anchor = min(candidates,key=lambda candidate:sum(
            min(abs(other.start()-candidate.start()) for other in matches)
            for matches in found.values() if matches))
        near = []
        for key, matches in found.items():
            if matches:
                match = min(matches,key=lambda m:abs(m.start()-anchor.start()))
                if abs(match.start()-anchor.start()) <= 600:
                    near.append(match)
        beginning = min(original.rfind('\n',0,m.start())+1 for m in near)
        anchors[index] = (beginning, near)
    from eregion.domain.resume_headers import SECTIONS, SKILL_SECTIONS, section_name
    boundaries, offset = [], 0
    for line in original.splitlines(keepends=True):
        if section_name(line) in SECTIONS | SKILL_SECTIONS:
            boundaries.append(offset)
        offset += len(line)
    rewritten, removed, updated = [], set(), []
    for index, entry in enumerate(entries):
        anchor = anchors.get(index)
        if entry.entry_type not in {'experience','project','education','summary','other'} or anchor is None:
            updated.append(entry)
            continue
        beginning, metadata = anchor
        ending = min([pos for i,(pos,_) in anchors.items() if i!=index and pos>beginning]
                     + [pos for pos in boundaries if pos>beginning] + [len(original)])
        ends = [original.find('\n',m.end()) for m in metadata if m.start()<ending]
        body_start = max((pos+1 for pos in ends if pos>=0),default=beginning)
        quote = original[beginning if entry.entry_type == 'education' else body_start:ending].strip()
        old = [e for e in evidence if e.evidence_id in entry.evidence_ids]
        # A full owned quote must actually contain the previous body evidence.
        # Metadata-only evidence is already represented by the source fields.
        normalized = re.sub(r'\s+',' ',quote).casefold()
        if not quote or not any(re.sub(r'\s+',' ',e.exact_text).casefold() in normalized for e in old):
            updated.append(entry)
            continue
        identifier = f'source-owned-{index}'
        removed.update(entry.evidence_ids)
        rewritten.append(ResumeEvidence(evidence_id=identifier,source_entry_id=entry.source_entry_id,
            source_section=old[0].source_section if old else entry.entry_type,
            exact_text=quote))
        updated.append(entry.model_copy(update={'evidence_ids':[identifier]}))
    return [e for e in evidence if e.evidence_id not in removed]+rewritten, updated
