"""Public contracts for Application Packs."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PackModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PackStatus(StrEnum):
    DRAFT = "draft"
    GENERATING = "generating"
    VERIFYING = "verifying"
    NEEDS_REVISION = "needs_revision"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    FAILED = "failed"
    STALE = "stale"


class ItemStatus(StrEnum):
    GENERATING = "generating"
    VERIFYING = "verifying"
    NEEDS_REVISION = "needs_revision"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"


class BlockType(StrEnum):
    FACTUAL = "factual"
    MOTIVATION = "motivation"
    TRANSITION = "transition"
    CLOSING = "closing"


class GroundedBlock(PackModel):
    block_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=5000)
    block_type: BlockType
    evidence_ids: list[str] = Field(default_factory=list)
    evidence_version_ids: list[str] = Field(default_factory=list)


class CoverLetterParagraph(PackModel):
    paragraph_type: Literal["opening", "fit", "evidence", "motivation", "closing"]
    text: str = Field(min_length=1, max_length=5000)
    evidence_ids: list[str] = Field(default_factory=list)
    evidence_version_ids: list[str] = Field(default_factory=list)
    target_requirement_ids: list[str] = Field(default_factory=list)


def upgrade_cover_letter_v1_to_v2(value: dict) -> dict:
    """Upgrade persisted GroundedBlock cover letters without inventing content."""
    if "blocks" not in value:
        raise ValueError("Legacy cover letter is missing blocks.")
    paragraphs = []
    for index, raw in enumerate(value.get("blocks") or []):
        block = raw if isinstance(raw, dict) else raw.model_dump(mode="python")
        block_type = str(block.get("block_type", "factual"))
        paragraph_type = (
            "closing" if block_type == "closing"
            else "motivation" if block_type in {"motivation", "transition"}
            else "evidence"
        )
        paragraphs.append({
            "paragraph_type": paragraph_type, "text": block.get("text", ""),
            "evidence_ids": block.get("evidence_ids") or [],
            "evidence_version_ids": block.get("evidence_version_ids") or [],
            "target_requirement_ids": [],
        })
    return {
        "schema_version": 2,
        "greeting": value.get("greeting") or "Dear Hiring Team,",
        "paragraphs": paragraphs,
        "closing": value.get("closing") or "Sincerely,",
        "signer_name": value.get("signer_name"),
    }


class CoverLetter(PackModel):
    schema_version: Literal[2] = 2
    greeting: str = "Dear Hiring Team,"
    paragraphs: list[CoverLetterParagraph] = Field(min_length=1, max_length=5)
    closing: str = "Sincerely,"
    signer_name: str | None = None

    @model_validator(mode="before")
    @classmethod
    def upgrade_blocks(cls, value):
        if not isinstance(value, dict) or "paragraphs" in value:
            return value
        if "blocks" not in value:
            return value
        return upgrade_cover_letter_v1_to_v2(value)

    @property
    def blocks(self) -> list[GroundedBlock]:
        return [GroundedBlock(
            block_id=f"paragraph-{index}", text=item.text,
            block_type=("factual" if item.paragraph_type in {"fit", "evidence"}
                        else "closing" if item.paragraph_type == "closing"
                        else "motivation"),
            evidence_ids=item.evidence_ids,
            evidence_version_ids=item.evidence_version_ids,
        ) for index, item in enumerate(self.paragraphs, start=1)]


class ApplicationAnswer(PackModel):
    question: str = Field(min_length=1, max_length=5000)
    answer_blocks: list[GroundedBlock]
    character_count: int = Field(ge=0)
    word_count: int = Field(ge=0)

    @model_validator(mode="after")
    def counts_match(self):
        text = " ".join(block.text for block in self.answer_blocks)
        if self.character_count != len(text) or self.word_count != len(text.split()):
            raise ValueError("Application answer counts must match block text.")
        return self


class ClaimIssue(PackModel):
    block_id: str
    unsupported_text: str
    reason: str
    cited_evidence_ids: list[str]
    revision_instruction: str


class ArtifactVerification(PackModel):
    passed: bool
    issues: list[ClaimIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def verdict_matches_issues(self):
        if self.passed == bool(self.issues):
            raise ValueError("Verification verdict and issues disagree.")
        return self


class EvidenceSnapshotItem(PackModel):
    evidence_id: str
    evidence_version_id: str
    content_hash: str
    source_type: str
    selection_reason: str
    associated_requirement_ids: list[str]
    claim_text: str
    exact_quote: str | None = None
    source_section: str | None = None
    category: str | None = None
    employer_or_project: str | None = None
    role: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    status_at_selection: Literal["confirmed"] = "confirmed"


class GenerationEvidenceSnapshot(PackModel):
    generation_snapshot_id: str
    pack_id: str
    application_id: str
    job_snapshot_id: str
    items: list[EvidenceSnapshotItem]
    resume_header_lines: list[str] = Field(default_factory=list)
    # Source-entry metadata of resume-imported evidence, by evidence_version_id: entry_type, heading,
    # organization, location, start_date, end_date and the entry's position in the resume.
    resume_entries: dict[str, dict] = Field(default_factory=dict)
    preference_versions: list[dict]
    prompt_version: str
    model_config_id: str
    model_configuration: dict[str, str | int | float | None] = Field(default_factory=dict)
    effective_tools: list[str] = Field(default_factory=list)
    evidence_set_hash: str
    created_at: datetime


class ApplicationPack(PackModel):
    perf_summary: dict | None = None
    pack_id: str
    workflow_mode: Literal["single_custom", "multi_agent_v1"] = "single_custom"
    application_id: str
    snapshot_id: str
    status: PackStatus
    version: int
    generation_number: int
    evidence_set_hash: str
    preference_snapshot_id: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    error_code: str | None
    stale_reason: str | None = None


class ApplicationPackItem(PackModel):
    pack_item_id: str
    pack_id: str
    artifact_id: str | None
    artifact_type: Literal["tailored_resume", "cover_letter", "application_answer"]
    status: ItemStatus
    source_question: str | None
    max_length: int | None
    version: int
    revision_count: int
    max_revisions: int
    verification: ArtifactVerification | None
    regeneration_feedback: str | None = None
    requires_manual_answer: bool = False
    created_at: datetime
    updated_at: datetime


class PackEvent(PackModel):
    event_id: str
    pack_id: str
    sequence: int
    event_type: str
    payload: dict
    occurred_at: datetime
