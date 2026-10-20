"""Deterministic planning and grounding checks for mock interview coaching."""
from __future__ import annotations

import re
from uuid import NAMESPACE_URL, uuid5

from eregion.runtime.mock_interview.errors import MockInterviewValidation
from eregion.runtime.mock_interview.types import (
    InterviewMode, MockAnswerEvaluation, MockInterviewPlan, MockInterviewQuestion,
    PlanItem,
)
from eregion.domain.domain import normalize_text
from eregion.runtime.protected_attributes import mentions_protected

PROMPT_VERSION = "mock-interview-v1"
_PROTECTED = re.compile(r"\b(?:age|marital status|religion|disability|nationality|race|gender)\b", re.I)

_COVERAGE = {
    InterviewMode.RECRUITER_SCREEN: ("role_fit", "background", "motivation"),
    InterviewMode.BEHAVIORAL: ("collaboration", "prioritization", "ambiguity", "conflict"),
    InterviewMode.PROJECT_DEEP_DIVE: ("personal_contribution", "approach", "tradeoffs", "validation"),
    InterviewMode.ROLE_SPECIFIC: ("requirement_fit", "responsibilities", "technical_approach"),
    InterviewMode.MIXED: ("role_fit", "collaboration", "requirement_fit", "project_deep_dive", "prioritization"),
}

# Evidence a competency's question may be about, in order of preference. A collaboration or
# project question about a degree or a skills list is not answerable, so those are never pinned.
_PROJECT_FIRST = ("project", "experience")
_EVIDENCE_FOR = {"project_deep_dive": _PROJECT_FIRST, "personal_contribution": _PROJECT_FIRST,
                 "approach": _PROJECT_FIRST, "tradeoffs": _PROJECT_FIRST, "validation": _PROJECT_FIRST,
                 "collaboration": ("experience", "project")}


def build_plan(*, interview_id: str, snapshot_id: str, snapshot_hash: str,
               pack_id: str | None = None, pack_version: int | None = None,
               source_mode: str = "job_analysis", evidence: list[dict],
               requirement_ids: list[str], mode: InterviewMode,
               requirement_texts: dict[str, str] | None = None,
               job_description_excerpt: str = "",
               approved_pack_excerpt: str = "",
               target_count: int) -> MockInterviewPlan:
    if not 3 <= target_count <= 12:
        raise MockInterviewValidation("Question count must be between 3 and 12.")
    if mode == InterviewMode.ROLE_SPECIFIC and not requirement_ids:
        raise MockInterviewValidation("Current Job analysis has no requirements.")
    choices = list(_COVERAGE[mode])
    if mode == InterviewMode.MIXED and not any(item.get("category") == "project" for item in evidence):
        choices.remove("project_deep_dive")
    items = []
    used: dict[str, int] = {}
    for index in range(target_count):
        competency = choices[index % len(choices)]
        requirement_id = (requirement_ids[index % len(requirement_ids)]
                          if requirement_ids and competency in {"requirement_fit", "responsibilities", "technical_approach"}
                          else None)
        related = []
        for category in _EVIDENCE_FOR.get(competency, ()):
            pool = [item for item in evidence if item.get("category") == category]
            if pool:
                related = [pool[used.get(category, 0) % len(pool)]["evidence_id"]]
                used[category] = used.get(category, 0) + 1
                break
        items.append(PlanItem(plan_item_id=str(uuid5(NAMESPACE_URL, f"{interview_id}:item:{index}")),
            sequence=index + 1, competency=competency,
            related_requirement_id=requirement_id,
            related_evidence_ids=related, question_type=competency))
    return MockInterviewPlan(plan_id=str(uuid5(NAMESPACE_URL, f"{interview_id}:plan")),
        mock_interview_id=interview_id, job_snapshot_id=snapshot_id,
        job_snapshot_hash=snapshot_hash, pack_id=pack_id, pack_version=pack_version,
        source_mode=source_mode,
        evidence=evidence, selected_requirement_ids=requirement_ids,
        requirement_texts=requirement_texts or {},
        job_description_excerpt=job_description_excerpt,
        approved_pack_excerpt=approved_pack_excerpt,
        selected_competencies=[item.competency for item in items],
        prompt_version=PROMPT_VERSION, items=items)


def validate_question(question: MockInterviewQuestion, item: PlanItem,
                      known_evidence_ids: set[str], source_text: str = "") -> None:
    if question.plan_item_id != item.plan_item_id or question.competency != item.competency:
        raise MockInterviewValidation("Question does not match its plan item.")
    if question.related_requirement_id != item.related_requirement_id:
        raise MockInterviewValidation("Question refers to another requirement.")
    if set(question.related_evidence_ids) - known_evidence_ids:
        raise MockInterviewValidation("Question references unknown evidence.")
    if mentions_protected(_PROTECTED, question.question_text):
        raise MockInterviewValidation("Question requests protected personal information.")
    question_numbers = set(re.findall(r"\b\d+(?:\.\d+)?%?\b", question.question_text))
    source_numbers = set(re.findall(r"\b\d+(?:\.\d+)?%?\b", source_text))
    if question_numbers - source_numbers:
        raise MockInterviewValidation("Question introduced a number absent from pinned context.")


def without_list_markers(text):
    """Ignore an ordered outline's 1./2./3. labels, not factual quantities.

    Require an actual consecutive sequence starting at one. Standalone numbers,
    years, decimal values, percentages and quantified achievements remain checked.
    """
    markers = list(re.finditer(r'(?<![\w.])([1-9]\d?)[.)]\s+(?=\S)', text))
    if len(markers) < 2 or [int(m[1]) for m in markers] != list(range(1, len(markers) + 1)):
        return text
    for marker in reversed(markers):
        text = text[:marker.start()] + text[marker.end():]
    return text


def validate_evaluation(evaluation: MockAnswerEvaluation, answer_text: str,
                        question: MockInterviewQuestion, permitted_context: str) -> None:
    if any(normalize_text(quote) not in normalize_text(answer_text)
           for quote in evaluation.exact_answer_quotes):
        raise MockInterviewValidation("Evaluation contains a quote absent from the answer.")
    if evaluation.discovered_fact_quote and normalize_text(evaluation.discovered_fact_quote) not in normalize_text(answer_text):
        raise MockInterviewValidation("Discovered fact quote is absent from the answer.")
    # Numerals in coaching must come from the answer or pinned source context.
    feedback = " ".join([*evaluation.strengths, *evaluation.improvement_areas,
        *evaluation.unsupported_or_unclear_claims, *evaluation.missing_answer_elements,
        without_list_markers(evaluation.suggested_structure or ""), evaluation.followup_reason or ""])
    source_numbers = set(re.findall(r"\b\d+(?:\.\d+)?%?\b", answer_text + " " + permitted_context))
    feedback_numbers = set(re.findall(r"\b\d+(?:\.\d+)?%?\b", feedback))
    if feedback_numbers - source_numbers:
        raise MockInterviewValidation("Coaching introduced an unsupported number.")


def should_follow_up(evaluation: MockAnswerEvaluation, *, followups_used: int,
                     maximum: int) -> bool:
    if followups_used >= maximum or not evaluation.followup_needed:
        return False
    return bool(evaluation.unsupported_or_unclear_claims
                or evaluation.missing_answer_elements
                or evaluation.followup_reason and any(term in evaluation.followup_reason.lower()
                    for term in ("ambig", "contribution", "personally", "star", "unclear")))
