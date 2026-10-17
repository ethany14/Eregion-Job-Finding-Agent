"""Explicit feedback ingestion and governed candidate review."""
from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5
import hashlib

from eregion.runtime.feedback.errors import FeedbackConflictError, FeedbackValidationError
from eregion.runtime.evidence.errors import EvidenceNotFoundError
from eregion.runtime.feedback.policy import LearningThresholds, redact_secrets, redact_skill_pii, signal_strength, structured_edit_diff
from eregion.runtime.feedback.processor import FeedbackProcessor
from eregion.runtime.feedback.repository import FeedbackRepository
from eregion.runtime.feedback.types import CandidateStatus, CandidateType, FeedbackEvent, FeedbackSourceType
from eregion.runtime.memory.types import MemoryProvenance, MemoryScope, MemorySensitivity, MemoryType
from eregion.runtime.memory.proposals import extract_memory_proposals
from eregion.runtime.evidence.extraction import career_fact_candidates, extract_evidence_candidates


class FeedbackService:
    def __init__(self, repository: FeedbackRepository, *, memories=None, evidence=None,
                 thresholds: LearningThresholds | None = None):
        self.repository = repository
        self.memories = memories
        self.evidence = evidence
        self.processor = FeedbackProcessor(repository, thresholds=thresholds)
        self.thresholds = thresholds or LearningThresholds()

    def record(self, *, owner_id: str, source_type: FeedbackSourceType,
               source_action_id: str, original_content: str | None = None,
               before_content: str | None = None, after_content: str | None = None,
               application_id: str | None = None, session_id: str | None = None,
               task_id: str | None = None, artifact_id: str | None = None,
               artifact_version: int | None = None, context_metadata_json: dict | None = None):
        original = redact_secrets(original_content)
        before = redact_secrets(before_content)
        after = redact_secrets(after_content)
        event = FeedbackEvent(owner_id=owner_id, source_type=source_type,
            source_action_id=source_action_id, original_content=original,
            before_content=before, after_content=after,
            application_id=application_id, session_id=session_id, task_id=task_id,
            artifact_id=artifact_id, artifact_version=artifact_version,
            structured_diff_json=(structured_edit_diff(before, after,
                (context_metadata_json or {}).get("unsupported_claims"))
                if source_type == FeedbackSourceType.ARTIFACT_EDITED and before is not None
                and after is not None else None),
            context_metadata_json=context_metadata_json or {},
            signal_strength=signal_strength(source_type, original))
        stored = self.repository.ingest(event)
        candidate = self.processor.process(stored.feedback_event_id, owner_id=owner_id)
        return stored, candidate

    def review(self, candidate_id: str, *, owner_id: str, action: str,
               expected_version: int, idempotency_key: str, content: str | None = None):
        candidate = self.repository.candidate(candidate_id, owner_id=owner_id)
        if candidate.review_action == f"{action}:{hashlib.sha256(idempotency_key.encode()).hexdigest()}":
            return candidate
        if candidate.version != expected_version:
            raise FeedbackConflictError("Learning candidate version changed.")
        if action == "edit":
            if not content or not content.strip():
                raise FeedbackValidationError("Edited content is required.")
            proposed = redact_secrets(content.strip())
            if candidate.candidate_type == CandidateType.SKILL:
                proposed = redact_skill_pii(proposed)
            edited_status = (CandidateStatus.COLLECTING
                if candidate.candidate_type == CandidateType.SKILL
                and candidate.status == CandidateStatus.COLLECTING
                else CandidateStatus.READY_FOR_REVIEW)
            return self.repository.review(candidate_id, owner_id=owner_id,
                expected_version=expected_version, idempotency_key=idempotency_key,
                status=edited_status, action=action,
                proposed_content=proposed)
        if action == "reject":
            return self.repository.review(candidate_id, owner_id=owner_id,
                expected_version=expected_version, idempotency_key=idempotency_key,
                status=CandidateStatus.REJECTED, action=action)
        if action == "keep-collecting":
            return self.repository.review(candidate_id, owner_id=owner_id,
                expected_version=expected_version, idempotency_key=idempotency_key,
                status=CandidateStatus.COLLECTING, action=action)
        if action == "approve-for-evaluation":
            if candidate.candidate_type != CandidateType.SKILL:
                raise FeedbackValidationError("Only Skill candidates support evaluation approval.")
            if candidate.status != CandidateStatus.READY_FOR_REVIEW:
                raise FeedbackConflictError("Skill candidate is not ready for evaluation review.")
            return self.repository.review(candidate_id, owner_id=owner_id,
                expected_version=expected_version, idempotency_key=idempotency_key,
                status=CandidateStatus.CONFIRMED, action=action)
        if action != "confirm":
            raise FeedbackValidationError("Unknown candidate action.")
        if candidate.status != CandidateStatus.READY_FOR_REVIEW:
            raise FeedbackConflictError("Candidate is not ready for review.")
        if candidate.candidate_type == CandidateType.PREFERENCE_MEMORY:
            if self.memories is None:
                raise FeedbackValidationError("Memory repository is unavailable.")
            memory_id = str(uuid5(NAMESPACE_URL, f"feedback:{candidate_id}:memory"))
            memory = self.memories.get(memory_id, owner_id=owner_id)
            if memory is None:
                proposals = [item for item in extract_memory_proposals(candidate.proposed_content)
                             if item.key == candidate.proposed_key_or_name]
                proposal = proposals[0] if len(proposals) == 1 else None
                value = proposal.value if proposal else candidate.proposed_content
                display_text = (f"{candidate.proposed_key_or_name} = {value}"
                                if proposal else candidate.proposed_content[:2000])
                # Stage 2: application feedback uses the APPLICATION scope (no longer
                # PROJECT + "application:<id>") and the shared consolidation write path.
                scope = MemoryScope.USER if candidate.scope.value == "user" else MemoryScope.APPLICATION
                scope_id = owner_id if scope == MemoryScope.USER else candidate.scope_id
                from eregion.runtime.memory.consolidation import write_statement
                memory, _ = write_statement(self.memories, owner_id=owner_id, scope=scope, scope_id=scope_id,
                    key=candidate.proposed_key_or_name, value=value, display_text=display_text,
                    memory_type=MemoryType.PREFERENCE, memory_id=memory_id,
                    provenance=MemoryProvenance(source_type="explicit_user_feedback",
                        source_id=candidate_id, actor_type="user", user_confirmed=True),
                    sensitivity=MemorySensitivity.PERSONAL, confidence=candidate.confidence,
                    canonical_scope_id=getattr(self.memories, "_canonical", None))
            if memory.status.value == "candidate":
                replaces = memory.content.get("replaces_memory_id")
                if replaces:
                    # The user explicitly confirmed a value that differs from the confirmed one:
                    # replace it; the old value stays in the history as superseded.
                    old = self.memories.require(replaces, owner_id=owner_id)
                    self.memories.supersede(replaces, replacement_memory_id=memory.memory_id, owner_id=owner_id,
                        expected_version=old.version, replacement_expected_version=memory.version)
                else:
                    self.memories.confirm(memory.memory_id, owner_id=owner_id,
                        expected_version=memory.version, confirmed_by_user=True)
            linked = {"linked_memory_id": memory.memory_id}
        elif candidate.candidate_type == CandidateType.CAREER_EVIDENCE:
            if self.evidence is None:
                raise FeedbackValidationError("Evidence repository is unavailable.")
            extracted = career_fact_candidates(extract_evidence_candidates(candidate.proposed_content))
            if len(extracted) > 1:
                raise FeedbackValidationError(
                    "Feedback contains multiple career facts; review them as separate candidates."
                )
            proposed_fact = extracted[0].source_quote if extracted else candidate.proposed_content
            evidence_id = candidate.linked_evidence_id or str(uuid5(
                NAMESPACE_URL, f"feedback:{candidate_id}:evidence"))
            try:
                record = self.evidence.get(evidence_id)
            except EvidenceNotFoundError:
                record = self.evidence.create_candidate(category="experience",
                    claim_text=proposed_fact, source_type="user_attested",
                    exact_quote=proposed_fact, created_by="local-user",
                    source_reference=candidate_id,
                    tags=["extraction:evidence-candidate-v1"],
                    evidence_id=evidence_id)
            if record.current.claim_text != proposed_fact:
                raise FeedbackConflictError("Evidence wording changed before confirmation.")
            if record.status.value == "candidate":
                self.evidence.confirm(record.evidence_id, record.version)
            linked = {"linked_evidence_id": record.evidence_id}
        elif candidate.candidate_type == CandidateType.PRODUCT_POLICY:
            linked = {}
        else:
            raise FeedbackValidationError("This candidate cannot be confirmed.")
        return self.repository.review(candidate_id, owner_id=owner_id,
            expected_version=expected_version, idempotency_key=idempotency_key,
            status=CandidateStatus.CONFIRMED, action=action, **linked)
