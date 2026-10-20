"""Deterministic resume content-quality validation and cleanup."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from eregion.domain.schemas import (
    ResumeEntry, ResumeHeader, ResumeQualityIssue, ResumeQualityResult,
    ResumeSection, ResumeSourceEntry, SupportedClaim, TailoredResume,
)

_BULLET_PREFIX = re.compile(r"^\s*(?:[-*•◦+|]|\d+[.)])\s*")
_TOKENS = re.compile(r"[a-z0-9+#./]+")
_ACTION_WORDS = frozenset({
    "built", "created", "designed", "developed", "implemented", "improved",
    "increased", "reduced", "delivered", "analyzed", "led", "managed",
})


class DuplicateClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kept_claim_id: str
    removed_claim_id: str
    similarity: float = Field(ge=0, le=1)
    reason: str


def normalize_claim_text(text: str) -> str:
    cleaned = _BULLET_PREFIX.sub("", text).casefold()
    cleaned = re.sub(r"[^\w+#./\s]", " ", cleaned)
    # Keep meaningful internal punctuation (Node.js, CI/CD) but ignore prose punctuation.
    cleaned = re.sub(r"(?<!\w)[./]|[./](?!\w)", " ", cleaned)
    return " ".join(cleaned.split())


def claim_token_set(text: str) -> set[str]:
    return set(_TOKENS.findall(normalize_claim_text(text)))


def claim_similarity(left: str, right: str) -> float:
    left_normalized = normalize_claim_text(left)
    right_normalized = normalize_claim_text(right)
    if left_normalized == right_normalized:
        return 1.0
    left_tokens, right_tokens = claim_token_set(left), claim_token_set(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _strength(claim: SupportedClaim, index: int) -> tuple[int, int, int, int]:
    tokens = claim_token_set(claim.text)
    action = int(bool(tokens & _ACTION_WORDS))
    supported_outcome = int(bool(re.search(r"\d", claim.text)))
    specificity = min(len(tokens), 40)
    coverage = len(claim.target_requirement_ids)
    return action + supported_outcome, specificity, coverage, -index


def find_duplicate_claims(
    claims: Sequence[SupportedClaim], threshold: float = 0.82,
) -> list[DuplicateClaim]:
    duplicates: list[DuplicateClaim] = []
    removed: set[str] = set()
    for left_index, left in enumerate(claims):
        if left.claim_id in removed:
            continue
        for right_index in range(left_index + 1, len(claims)):
            right = claims[right_index]
            if right.claim_id in removed:
                continue
            similarity = claim_similarity(left.text, right.text)
            if similarity < threshold:
                continue
            keep, discard = (left, right)
            if _strength(right, right_index) > _strength(left, left_index):
                keep, discard = right, left
            duplicates.append(DuplicateClaim(
                kept_claim_id=keep.claim_id,
                removed_claim_id=discard.claim_id,
                similarity=round(similarity, 4),
                reason="normalized_exact_match" if similarity == 1 else "lexical_similarity",
            ))
            removed.add(discard.claim_id)
            if discard is left:
                break
    return duplicates


def deduplicate_claims(
    claims: Sequence[SupportedClaim], threshold: float = 0.82,
) -> tuple[list[SupportedClaim], list[DuplicateClaim]]:
    duplicates = find_duplicate_claims(claims, threshold)
    removed = {item.removed_claim_id for item in duplicates}
    return [claim for claim in claims if claim.claim_id not in removed], duplicates


def clean_tailored_resume(
    resume: TailoredResume, *, threshold: float = 0.82,
) -> tuple[TailoredResume, list[DuplicateClaim]]:
    """Remove duplicates within one source entry and normalize Skill aliases.

    Similar wording across distinct employers or projects is not sufficient proof
    that the underlying experience is duplicated, so cross-entry claims are never
    removed here.
    """
    duplicates: list[DuplicateClaim] = []
    sections: list[ResumeSection] = []
    for section in resume.sections:
        entries = []
        skill_names: dict[str, str] = {}
        for entry in section.entries:
            entry_claims, entry_duplicates = deduplicate_claims(
                entry.bullets, threshold
            )
            duplicates.extend(entry_duplicates)
            bullets = []
            for claim in entry_claims:
                if section.section_type == "skills":
                    canonical = normalize_claim_text(claim.text).replace("postgresql", "postgres")
                    if canonical in skill_names:
                        duplicates.append(DuplicateClaim(
                            kept_claim_id=skill_names[canonical],
                            removed_claim_id=claim.claim_id,
                            similarity=1.0,
                            reason="canonical_skill_alias",
                        ))
                        continue
                    skill_names[canonical] = claim.claim_id
                bullets.append(claim)
            entries.append(entry.model_copy(update={"bullets": bullets}))
        sections.append(section.model_copy(update={"entries": entries}))
    adjustments = list(resume.quality_adjustments)
    adjustments.extend(
        f"Removed duplicate claim {item.removed_claim_id}; retained {item.kept_claim_id}."
        for item in duplicates
    )
    cleaned = TailoredResume.model_validate({
        **resume.model_dump(mode="python"),
        "sections": [item.model_dump(mode="python") for item in sections],
        "quality_adjustments": adjustments,
    })
    return cleaned, duplicates


def normalize_generated_resume_metadata(
    resume: TailoredResume,
    source_entries: Sequence[ResumeSourceEntry],
    *,
    source_resume_text: str,
) -> TailoredResume:
    """Restore display metadata from verified sources without repairing claims."""
    sources = {entry.source_entry_id: entry for entry in source_entries}
    allowed_types = {
        "experience": {"experience"},
        "projects": {"project"},
        "education": {"education"},
    }
    sections: list[ResumeSection] = []
    adjustments = list(resume.quality_adjustments)
    for section in resume.sections:
        entries: list[ResumeEntry] = []
        for entry in section.entries:
            normalized = entry
            if section.section_type in allowed_types and entry.bullets:
                source_ids = {claim.source_entry_id for claim in entry.bullets}
                source = sources.get(next(iter(source_ids))) if len(source_ids) == 1 else None
                if source is not None and source.entry_type in allowed_types[section.section_type]:
                    expected = {
                        "entry_id": source.source_entry_id,
                        "heading": source.heading,
                        "subheading": source.organization,
                        "location": source.location,
                        "start_date": source.start_date,
                        "end_date": source.end_date,
                    }
                    actual = entry.model_dump(mode="python", exclude={"bullets"})
                    if any(actual.get(key) != value for key, value in expected.items()):
                        adjustments.append(
                            f"Restored {section.section_type} metadata from source entry "
                            f"{source.source_entry_id}."
                        )
                    normalized = ResumeEntry.model_validate({
                        **entry.model_dump(mode="python"),
                        **expected,
                    })
            entries.append(normalized)
        sections.append(ResumeSection.model_validate({
            **section.model_dump(mode="python"),
            "entries": [item.model_dump(mode="python") for item in entries],
        }))

    normalized_source = normalize_claim_text(source_resume_text)
    from eregion.domain.resume_headers import header_lines
    source_header = header_lines(source_resume_text, resume.header.name)
    name = source_header[0] if source_header else None
    source_contacts = source_header[1:]
    contact_lines = [value for value in resume.header.contact_lines
                    if any(normalize_claim_text(value) in normalize_claim_text(line)
                           for line in source_contacts)]
    if not contact_lines:
        contact_lines = source_contacts
    if name != resume.header.name:
        adjustments.append("Restored candidate name from the source header.")
    if contact_lines != resume.header.contact_lines:
        adjustments.append("Restored resume header metadata from the source resume.")
    return TailoredResume.model_validate({
        **resume.model_dump(mode="python"),
        "header": ResumeHeader(name=name, contact_lines=contact_lines).model_dump(
            mode="python"
        ),
        "sections": [section.model_dump(mode="python") for section in sections],
        "quality_adjustments": list(dict.fromkeys(adjustments)),
    })


def affirmative_skill_clauses(text):
    """Display verbatim affirmative skill clauses; keep limitations in source evidence.

    This is conservative presentation cleanup, never a way of asserting that an
    omitted/negated tool is known. Mixed uncertain clauses are omitted as a whole.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # A standalone group heading before a labelled/bulleted list is structure,
    # not two skills joined by "and". Preserve ordinary single-item skill lines.
    lines = [line for index, line in enumerate(lines) if not (
        index + 1 < len(lines) and ':' not in line and ',' not in line
        and not re.match(r'^[-•]', line) and len(line.split()) <= 6
        and re.search(r'\band\b', line, re.I)
        and re.match(r'^[-•]\s*[^:]{1,50}:', lines[index + 1]))]
    text = '\n'.join(lines)
    text = re.sub(r'\n\s*[-•]\s*', '; ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'^\s*[-•]\s*', '', text)
    # Category labels can contain 'and' (AI and evaluation). They are not skills.
    text = re.sub(r'(?:^|(?<=[.;])\s+)[A-Za-z][A-Za-z /&-]{0,45}:\s*(?=[^.;]*[,|])', ' ', text)
    clauses = re.split(r';+|(?<=[.!?])\s+', text)
    positive = [clause.strip() for clause in clauses if clause.strip()
                and not re.search(r'\b(?:no|not(?! only\b)|never|without|lack(?:ing|s)?|none)\b|不会|没有|不具备', clause, re.I)]
    items = []
    for clause in positive:
        # Keep source wording and qualifiers while representing a source list
        # as individual claims, as required by the shared resume validator.
        for part in skill_items(clause):
            part = part.strip().strip('.')
            if not part:
                continue
            if items and re.match(r'^(?:earned|obtained|issued|valid|since|through)\b', part, re.I):
                items[-1] += ' (' + part + ')'
            else:
                items.append(part)
    return items


def skill_items(text):
    """Split list separators outside qualifiers such as AWS (EKS, RDS)."""
    parts=[];start=0;depth=0
    for match in re.finditer(r'[()]|[,|•;]|\band\b',text):
        token=match.group()
        if token=='(':depth+=1
        elif token==')':depth=max(0,depth-1)
        elif depth==0:parts.append(text[start:match.start()]);start=match.end()
    return [*parts,text[start:]]


def education_header_only(text, source):
    remaining = normalize_claim_text(text)
    for value in sorted((source.heading, source.organization, source.location,
                         source.start_date, source.end_date), key=lambda v: len(v or ''), reverse=True):
        if value:
            remaining = remaining.replace(normalize_claim_text(value), '')
    return not re.search(r'\w', remaining)


def ensure_foundational_resume_sections(
    resume: TailoredResume,
    source_entries: Sequence[ResumeSourceEntry],
    evidence_by_id: dict[str, str],
) -> TailoredResume:
    """Retain source-backed Education and Skills even when the writer omits them."""
    sections = list(resume.sections)
    by_type = {section.section_type: section for section in sections}
    adjustments = list(resume.quality_adjustments)
    for source_type, section_type, title in (
        ("education", "education", "Education"),
        ("skills", "skills", "Skills"),
    ):
        section = by_type.get(section_type)
        entries = list(section.entries) if section is not None else []
        for source in source_entries:
            if source.entry_type != source_type:
                continue
            existing_index = next((index for index, entry in enumerate(entries)
                                   if entry.entry_id == source.source_entry_id), None)
            existing = entries[existing_index] if existing_index is not None else None
            if source_type == 'skills':
                # A citation to a whole skill block does not prove the writer
                # actually retained every skill. Restore the positive source
                # clauses directly, instead of treating its ID as full coverage.
                claims = [SupportedClaim(text=clause, evidence_ids=[key], source_entry_id=source.source_entry_id)
                          for key in source.evidence_ids
                          for clause in affirmative_skill_clauses(evidence_by_id.get(key, ''))]
                if not claims:
                    if existing_index is not None:
                        entries.pop(existing_index)
                    continue
                restored = ResumeEntry(entry_id=source.source_entry_id, heading=source.heading,
                    subheading=source.organization, bullets=claims)
                if existing_index is None:
                    entries.append(restored)
                else:
                    entries[existing_index] = restored
                continue
            if existing is not None:
                existing = existing.model_copy(update={'bullets': [claim for claim in existing.bullets
                    if not education_header_only(claim.text, source)]})
                entries[existing_index] = existing
            cited = {evidence_id for claim in (existing.bullets if existing else [])
                     for evidence_id in claim.evidence_ids}
            claims = []
            for evidence_id in source.evidence_ids:
                if evidence_id in cited:
                    continue
                text = evidence_by_id.get(evidence_id)
                if not text or education_header_only(text, source):
                    continue
                digest = hashlib.sha256(
                    f"{source.source_entry_id}|{evidence_id}|{text}".encode()
                ).hexdigest()[:12]
                claims.append(SupportedClaim(
                    claim_id=f"CLM-{digest}",
                    text=text,
                    evidence_ids=[evidence_id],
                    source_entry_id=source.source_entry_id,
                    target_requirement_ids=[],
                ))
            if not claims and existing is not None:
                continue
            restored = ResumeEntry(
                entry_id=source.source_entry_id,
                heading=source.heading,
                subheading=source.organization,
                location=source.location,
                start_date=source.start_date,
                end_date=source.end_date,
                bullets=[*(existing.bullets if existing else []), *claims],
            )
            if existing_index is None:
                entries.append(restored)
            else:
                entries[existing_index] = restored
            adjustments.append(
                f"Restored source-backed {section_type} entry {source.source_entry_id}."
            )
        if not entries and section is not None and source_type == 'skills':
            sections.remove(section)
            by_type.pop(section_type, None)
        if entries:
            replacement = ResumeSection(
                section_type=section_type,
                title=section.title if section is not None else title,
                entries=entries,
            )
            if section is None:
                sections.append(replacement)
            else:
                sections[sections.index(section)] = replacement
            by_type[section_type] = replacement

    order = {"summary": 0, "education": 1, "experience": 2, "projects": 3, "skills": 4}
    sections.sort(key=lambda item: order[item.section_type])
    return TailoredResume.model_validate({
        **resume.model_dump(mode="python"),
        "sections": [item.model_dump(mode="python") for item in sections],
        "quality_adjustments": list(dict.fromkeys(adjustments)),
    })


def validate_resume_structure(
    resume: TailoredResume,
    source_entries: Sequence[ResumeSourceEntry],
    *,
    known_evidence_ids: set[str] | None = None,
    allow_legacy_source_ids: bool = False,
    source_resume_text: str | None = None,
) -> list[ResumeQualityIssue]:
    issues: list[ResumeQualityIssue] = []
    sources = {entry.source_entry_id: entry for entry in source_entries}
    claims_by_section: dict[str, list[SupportedClaim]] = {}
    claims_by_entry: list[tuple[str, str, list[SupportedClaim]]] = []

    if source_resume_text is not None:
        normalized_resume = normalize_claim_text(source_resume_text)
        header_values = [resume.header.name, *resume.header.contact_lines]
        unsupported_header = [value for value in header_values
                              if value and normalize_claim_text(value) not in normalized_resume]
        if unsupported_header:
            issues.append(ResumeQualityIssue(
                code="unsupported_header_metadata",
                message="Resume header contains text not present in the source resume.",
            ))
        from eregion.domain.resume_layout import DATE_RANGE
        if DATE_RANGE.search(source_resume_text):
            for source in source_entries:
                if source.entry_type in {'experience','education'} and not source.organization and not source.start_date and not source.end_date:
                    if not source.heading or normalize_claim_text(source.heading) in {'experience','education','work experience'}:
                        issues.append(ResumeQualityIssue(code='unresolved_source_structure',
                            message='A dated source section has no resolved entry ownership or dates. Re-extract its structure before generating a resume.',
                            section_type=source.entry_type))

    for section in resume.sections:
        claims = [claim for entry in section.entries for claim in entry.bullets]
        claims_by_section.setdefault(section.section_type, []).extend(claims)
        if not section.entries or not claims:
            issues.append(ResumeQualityIssue(
                code="empty_section", message=f"{section.title} contains no claims.",
                section_type=section.section_type,
            ))
        for entry in section.entries:
            claims_by_entry.append((section.section_type, entry.entry_id, entry.bullets))
            source_bound = section.section_type in {"experience", "projects", "education"}
            entry_source = sources.get(entry.entry_id) if source_bound else None
            if source_bound and entry_source is None:
                issues.append(ResumeQualityIssue(
                    code="unknown_source_entry",
                    message="Resume entry does not identify a known source entry.",
                    claim_ids=[item.claim_id for item in entry.bullets],
                    section_type=section.section_type,
                ))
            if entry_source is not None:
                expected_metadata = {
                    "heading": entry_source.heading,
                    "subheading": entry_source.organization,
                    "location": entry_source.location,
                    "start_date": entry_source.start_date,
                    "end_date": entry_source.end_date,
                }
                actual_metadata = {
                    "heading": entry.heading,
                    "subheading": entry.subheading,
                    "location": entry.location,
                    "start_date": entry.start_date,
                    "end_date": entry.end_date,
                }
                mismatched = [name for name, expected in expected_metadata.items()
                              if normalize_claim_text(actual_metadata[name] or "")
                              != normalize_claim_text(expected or "")]
                if mismatched:
                    issues.append(ResumeQualityIssue(
                        code="source_metadata_mismatch",
                        message=("Resume entry metadata differs from its source entry: "
                                 + ", ".join(mismatched) + "."),
                        claim_ids=[item.claim_id for item in entry.bullets],
                        section_type=section.section_type,
                    ))
            for claim in entry.bullets:
                if not claim.evidence_ids:
                    issues.append(ResumeQualityIssue(
                        code="claim_without_evidence", message="Claim has no resume evidence.",
                        claim_ids=[claim.claim_id], section_type=section.section_type,
                    ))
                unknown = [item for item in claim.evidence_ids
                           if known_evidence_ids is not None and item not in known_evidence_ids]
                if unknown:
                    issues.append(ResumeQualityIssue(
                        code="unknown_evidence_id",
                        message=f"Claim cites unknown evidence IDs: {', '.join(unknown)}.",
                        claim_ids=[claim.claim_id], section_type=section.section_type,
                    ))
                is_legacy = claim.source_entry_id.startswith("legacy:")
                source = sources.get(claim.source_entry_id)
                if is_legacy and not allow_legacy_source_ids:
                    issues.append(ResumeQualityIssue(
                        code="legacy_source_entry",
                        message="New resume claims cannot use legacy source entries.",
                        claim_ids=[claim.claim_id], section_type=section.section_type,
                    ))
                elif source is None and not (allow_legacy_source_ids and is_legacy):
                    issues.append(ResumeQualityIssue(
                        code="unknown_source_entry", message="Claim cites an unknown source entry.",
                        claim_ids=[claim.claim_id], section_type=section.section_type,
                    ))
                if source_bound and source is not None and claim.source_entry_id != entry.entry_id:
                    issues.append(ResumeQualityIssue(
                        code="entry_source_mismatch",
                        message="Claim is attached to a different resume source entry.",
                        claim_ids=[claim.claim_id], section_type=section.section_type,
                    ))
                if source and not set(claim.evidence_ids).issubset(source.evidence_ids):
                    issues.append(ResumeQualityIssue(
                        code="invalid_section_membership",
                        message="Claim cites evidence owned by another source entry.",
                        claim_ids=[claim.claim_id], section_type=section.section_type,
                    ))
                allowed = {
                    "summary": {"summary", "experience", "project", "skills", "other"},
                    "experience": {"experience"}, "projects": {"project"},
                    "education": {"education"}, "skills": {"skills", "experience", "project", "other"},
                }[section.section_type]
                # An 'other' entry with its own heading and an organization or dates is a role
                # (an activity, volunteering); the writer files it under experience. Loose facts are not.
                role_like = bool(source and source.entry_type == "other" and source.heading
                                 and (source.organization or source.start_date))
                if section.section_type == "experience" and role_like:
                    allowed = allowed | {"other"}
                if source and source.entry_type not in allowed:
                    issues.append(ResumeQualityIssue(
                        code="invalid_section_membership",
                        message=f"{source.entry_type} source cannot appear in {section.section_type}.",
                        claim_ids=[claim.claim_id], section_type=section.section_type,
                    ))
                if section.section_type == "skills" and len(skill_items(claim.text))>1:
                    issues.append(ResumeQualityIssue(
                        code="unformatted_skill_block",
                        message="Skills must be separate normalized items.",
                        claim_ids=[claim.claim_id], section_type="skills",
                    ))

    summary = claims_by_section.get("summary", [])
    summary_text = " ".join(claim.text for claim in summary)
    sentence_count = len([item for item in re.split(r"(?<=[.!?])\s+", summary_text.strip()) if item])
    if sentence_count > 2:
        issues.append(ResumeQualityIssue(
            code="summary_too_long", message="Professional summary must be at most two sentences.",
            claim_ids=[item.claim_id for item in summary], section_type="summary",
        ))
    non_summary = [claim for kind, claims in claims_by_section.items() if kind != "summary" for claim in claims]
    for summary_claim in summary:
        for other in non_summary:
            if claim_similarity(summary_claim.text, other.text) >= .82:
                issues.append(ResumeQualityIssue(
                    code="summary_repeats_bullet",
                    message="Professional summary substantially repeats another resume bullet.",
                    claim_ids=[summary_claim.claim_id, other.claim_id], section_type="summary",
                ))
    for section_type, _, claims in claims_by_entry:
        for duplicate in find_duplicate_claims(claims):
            issues.append(ResumeQualityIssue(
                code="duplicate_claim",
                message="A resume source entry contains substantially duplicate claims.",
                claim_ids=[duplicate.kept_claim_id, duplicate.removed_claim_id],
                section_type=section_type,
            ))
    for left_index, (left_section, left_entry, left_claims) in enumerate(claims_by_entry):
        for right_section, right_entry, right_claims in claims_by_entry[left_index + 1:]:
            if left_entry == right_entry:
                continue
            for left in left_claims:
                for right in right_claims:
                    if claim_similarity(left.text, right.text) >= .82:
                        issues.append(ResumeQualityIssue(
                            code="cross_source_similarity",
                            message=("Similar wording appears under different source entries; "
                                     "review it without automatically deleting either fact."),
                            claim_ids=[left.claim_id, right.claim_id],
                            section_type=(left_section if left_section == right_section else None),
                            severity="warning",
                        ))
    skills = claims_by_section.get("skills", [])
    seen: dict[str, str] = {}
    for claim in skills:
        name = normalize_claim_text(claim.text).replace("postgresql", "postgres")
        if name in seen:
            issues.append(ResumeQualityIssue(
                code="duplicate_skill", message="Skill appears more than once.",
                claim_ids=[seen[name], claim.claim_id], section_type="skills",
            ))
        seen[name] = claim.claim_id
    return issues


def quality_result(issues: Sequence[ResumeQualityIssue]) -> ResumeQualityResult:
    errors = [item for item in issues if item.severity == "error"]
    unique_feedback = list(dict.fromkeys(item.message for item in errors))
    return ResumeQualityResult(
        passed=not errors, issues=list(issues), revision_feedback=unique_feedback,
    )

