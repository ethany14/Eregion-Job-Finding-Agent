"""Deterministic, application-scoped evidence selection and question gating."""
from __future__ import annotations

import hashlib
import re

from eregion.runtime.application_pack.types import EvidenceSnapshotItem
from eregion.runtime.context.budget import estimate_tokens
from eregion.runtime.evidence.types import (
    CareerEvidence, EvidenceApplicationLink, EvidenceSourceType, EvidenceStatus,
)
from eregion.runtime.security import canonical_json
from eregion.domain.schemas import JobAnalysis
from eregion.domain.domain import normalize_text
from eregion.domain.resume_headers import SKILL_SECTIONS, section_name


def _words(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#./-]{2,}", value.casefold()))


_INSTRUCTION_TEXT = re.compile(
    r"\b(ignore (?:previous|prior|all) instructions|system prompt|developer message|"
    r"override (?:the )?(?:verifier|safety|rules)|do not verify)\b", re.I)


def _same_source_entry(left, right) -> bool:
    """Skills-section extraction may lack entry metadata in older Vault versions.

    Only explicit skill sections in the same resume share this relaxed boundary;
    employment, project, and education ownership remains strict.
    """
    if not left.source_reference or left.source_reference != right.source_reference:
        return False
    if (left.category.value == right.category.value == 'skill'
            and section_name(left.source_section or '') in SKILL_SECTIONS
            and section_name(right.source_section or '') in SKILL_SECTIONS):
        return True
    if (left.category.value == right.category.value == 'skill'
            and section_name(left.source_section or '') != section_name(right.source_section or '')):
        return False
    return (left.category, left.employer_or_project, left.role, left.start_date, left.end_date) == (
        right.category, right.employer_or_project, right.role, right.start_date, right.end_date)


class EvidenceSelectionPolicy:
    def select(self, *, evidence: list[CareerEvidence], links: list[EvidenceApplicationLink],
               job: JobAnalysis, token_budget: int = 1800,
               resume_text: str | None = None) -> list[EvidenceSnapshotItem]:
        # The Vault keeps older resumes for history; they are not additional jobs.
        # Scope before ranking so even an old application link cannot mix versions.
        if resume_text is not None:
            source_ref = hashlib.sha256(normalize_text(resume_text).encode()).hexdigest()
            evidence = [item for item in evidence
                        if item.current.source_type != EvidenceSourceType.RESUME
                        or item.current.source_reference == source_ref]
            structured = {'fit_analysis', 'fit_analysis_import', 'pack_resume_import'}
            if any(item.status == EvidenceStatus.CONFIRMED
                   and item.current.source_type == EvidenceSourceType.RESUME
                   and item.current.created_by in structured for item in evidence):
                evidence = [item for item in evidence
                            if item.current.source_type != EvidenceSourceType.RESUME
                            or item.current.created_by in structured]
        generic_words = {"with", "from", "have", "experience", "experienced", "working",
                         "work", "team", "teams", "role", "candidate", "strong", "skills",
                         "ability", "using", "good", "the", "and", "for", "this", "that"}
        context_words = _words(" ".join([job.title or "", job.summary, *job.responsibilities])) - generic_words
        by_id = {item.evidence_id: item for item in evidence
                 if item.status == EvidenceStatus.CONFIRMED
                 and not _INSTRUCTION_TEXT.search(item.current.claim_text)}
        linked: dict[str, list[EvidenceApplicationLink]] = {}
        for link in links:
            if link.evidence_id in by_id and link.evidence_version_id == by_id[link.evidence_id].current.evidence_version_id:
                linked.setdefault(link.evidence_id, []).append(link)
        # Complete quotes supersede physical-line fragments only within an
        # established source boundary. Keep the Vault/history immutable.
        source_entries = [item for item in by_id.values()
                          if item.current.source_type == EvidenceSourceType.RESUME]
        for item in source_entries:
            quote = normalize_text(item.current.claim_text)
            if quote and any(other.evidence_id != item.evidence_id
                   and _same_source_entry(other.current, item.current)
                   and len(normalize_text(other.current.claim_text)) > len(quote)
                   and quote in normalize_text(other.current.claim_text)
                   for other in source_entries):
                by_id.pop(item.evidence_id, None)
        ranked = []
        for item in by_id.values():
            # An Interviewer answer remains application-scoped until explicitly linked here.
            if item.current.source_type == EvidenceSourceType.INTERVIEW and item.evidence_id not in linked:
                continue
            claim_words = _words(item.current.claim_text)
            required = [req for req in job.requirements if req.level == "required"
                        and claim_words & _words(req.canonical_name + " " + req.atomic_text)]
            preferred = [req for req in job.requirements if req.level == "preferred"
                         and claim_words & _words(req.canonical_name + " " + req.atomic_text)]
            if (item.current.source_type == EvidenceSourceType.RESUME
                    and item.current.category.value in {'skill', 'education'}):
                rank, reason = -1, "resume_foundation"
            elif item.evidence_id in linked:
                rank, reason = 0, "application_link"
            elif required:
                rank, reason = 1, "required_requirement"
            elif preferred:
                rank, reason = 2, "preferred_requirement"
            elif claim_words & context_words:
                rank, reason = 3, "other_relevant_confirmed"
            else:
                continue
            associated = sorted({link.requirement_id for link in linked.get(item.evidence_id, [])
                                 if link.requirement_id} | {req.requirement_id for req in required + preferred})
            metadata_score = sum(bool(value) for value in (
                item.current.employer_or_project,
                item.current.role,
                item.current.start_date,
                item.current.end_date,
            )) + (1 if item.current.category.value != "experience" else 0)
            source_priority = (
                0 if item.current.source_type == EvidenceSourceType.RESUME else 1
            )
            ranked.append((rank, -len(required), -metadata_score, source_priority,
                           item.evidence_id, item, reason, associated))
        ranked.sort()
        selected, remaining = [], token_budget
        selected_claims: set[tuple] = set()
        for _, _, _, _, _, item, reason, associated in ranked:
            version = item.current
            claim_key = (version.source_type, version.source_reference, version.category,
                         version.employer_or_project, version.role, version.start_date,
                         version.end_date, section_name(version.source_section or ''),
                         normalize_text(version.claim_text))
            if claim_key in selected_claims:
                continue
            cost = estimate_tokens({"claim": version.claim_text, "quote": version.exact_quote,
                                    "id": version.evidence_version_id})
            if cost > remaining:
                continue
            selected.append(EvidenceSnapshotItem(
                evidence_id=item.evidence_id, evidence_version_id=version.evidence_version_id,
                content_hash=version.content_hash, source_type=version.source_type.value,
                selection_reason=reason, associated_requirement_ids=associated,
                claim_text=version.claim_text, exact_quote=version.exact_quote,
                source_section=version.source_section,
                category=version.category.value,
                employer_or_project=version.employer_or_project,
                role=version.role,
                start_date=version.start_date,
                end_date=version.end_date,
            ))
            remaining -= cost
            selected_claims.add(claim_key)
        return selected


def evidence_set_hash(items: list[EvidenceSnapshotItem], *, job_snapshot_id: str,
                      preferences: list[dict], prompt_version: str,
                      model_configuration: dict | None = None,
                      resume_header_lines: list[str] | None = None,
                      resume_entries: dict[str, dict] | None = None) -> str:
    payload = {"job_snapshot_id": job_snapshot_id, "prompt_version": prompt_version,
               "evidence": [item.model_dump(mode="json") for item in items],
               "preferences": preferences,
               "resume_header_lines": resume_header_lines or [],
               "model_configuration": model_configuration or {}}
    if resume_entries:
        payload["resume_entries"] = resume_entries
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


_DEMOGRAPHIC = re.compile(r"\b(disability|veteran status|race|ethnicity|gender|sexual orientation|religion|pregnan)\w*\b", re.I)
_LEGAL = re.compile(r"\b(sponsor(?:ship)?|work authori[sz]ation|citizen(?:ship)?|criminal history|background check)\b", re.I)
_LOGISTICS = re.compile(r"\b(salary|compensation|relocat|availab|start date|travel)\w*\b", re.I)


def classify_question(question: str, *, classifier=None) -> str:
    if _DEMOGRAPHIC.search(question): return "demographic"
    if _LEGAL.search(question): return "legal_or_identity"
    if _LOGISTICS.search(question): return "logistics"
    if classifier is not None:
        try:
            category = classifier(question)
            if category in {"experience", "role_fit", "motivation", "company_interest",
                            "logistics", "legal_or_identity", "demographic", "unknown"}:
                return category
        except Exception:
            # A model failure must not accidentally permit unknown personal claims.
            return "unknown"
    lowered = question.casefold()
    if re.search(r"\b(why (?:this|our) company|interested in (?:this|our) company)\b", lowered): return "company_interest"
    if re.search(r"\b(why|motivat|interested)\b", lowered): return "motivation"
    if re.search(r"\b(experience|used|built|worked with|project)\b", lowered): return "experience"
    if re.search(r"\b(fit|qualified|strength)\b", lowered): return "role_fit"
    return "unknown"


def requires_manual_answer(category: str) -> bool:
    return category in {"logistics", "legal_or_identity", "demographic", "unknown"}
