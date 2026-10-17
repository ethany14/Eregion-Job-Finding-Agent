"""Observe completed turns and route grounded learning into governed stores."""
from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from eregion.runtime.feedback.service import FeedbackService
from eregion.runtime.feedback.types import FeedbackSourceType
from eregion.runtime.learning.analyzer import ConversationLearningAnalyzer
from eregion.runtime.learning.repository import ConversationLearningRepository
from eregion.runtime.learning.types import (
    ConversationExperience,
    ExperienceSignal,
    ExperienceSignalType,
    LearningObservation,
    LearningPatternStatus,
)
from eregion.runtime.memory.errors import MemoryAlreadyExistsError
from eregion.runtime.memory.repository import MemoryRepository
from eregion.runtime.memory.types import (
    MemoryProvenance,
    MemoryScope,
    MemorySensitivity,
    MemoryType,
)
from eregion.runtime.sessions.repository import SessionRepository


_SUSPICIOUS = (
    "ignore previous instructions",
    "ignore all instructions",
    "reveal system prompt",
    "call this tool",
    "execute this instruction",
)


class ConversationLearningService:
    """Best-effort observer; learning failures never fail the user turn."""

    def __init__(
        self,
        *,
        sessions: SessionRepository,
        repository: ConversationLearningRepository,
        analyzer: ConversationLearningAnalyzer,
        memories: MemoryRepository,
        feedback: FeedbackService,
        owner_id: str,
        application_for_session=None,
    ) -> None:
        self._sessions = sessions
        self.repository = repository
        self._analyzer = analyzer
        self._memories = memories
        self._feedback = feedback
        self._owner_id = owner_id
        self._application_for_session = application_for_session or (lambda _: None)

    def observe_completed_turn(
        self, *, session_id: str, assistant_message_id: str
    ) -> ConversationExperience | None:
        messages = self._sessions.messages(session_id)
        assistant_index = next((index for index, item in enumerate(messages)
            if item.message.message_id == assistant_message_id), None)
        if assistant_index is None:
            return None
        user = next((item.message for item in reversed(messages[:assistant_index])
                     if item.message.role == "user"), None)
        assistant = messages[assistant_index].message
        if user is None or assistant.role != "assistant":
            return None
        try:
            application_id = self._application_for_session(session_id)
        except Exception:
            application_id = None
        existing = self.repository.for_turn(
            session_id, user.message_id, assistant_message_id
        )
        if existing is not None:
            if existing.observation is not None:
                self._apply_observation_safely(existing)
            return existing
        try:
            raw = self._analyze(user, assistant, getattr(messages[assistant_index], "created_at", None))
            observation = self._validate_observation(raw, user.content)
        except Exception:
            experience, _ = self.repository.record(
                owner_id=self._owner_id,
                session_id=session_id,
                application_id=application_id,
                user_message_id=user.message_id,
                assistant_message_id=assistant_message_id,
                observation=None,
                error_code="conversation_observation_failed",
            )
            return experience
        experience, created = self.repository.record(
            owner_id=self._owner_id,
            session_id=session_id,
            application_id=application_id,
            user_message_id=user.message_id,
            assistant_message_id=assistant_message_id,
            observation=observation,
        )
        if not created:
            return experience
        self._apply_observation_safely(experience)
        return experience

    def _analyze(self, user, assistant, created_at):
        import inspect
        try:
            accepts_date = "message_date" in inspect.signature(self._analyzer.analyze).parameters
        except (TypeError, ValueError):
            accepts_date = False
        if accepts_date:
            return self._analyzer.analyze(user_message=user, assistant_message=assistant,
                                          message_date=created_at.date().isoformat() if created_at else None)
        return self._analyzer.analyze(user_message=user, assistant_message=assistant)

    def _apply_observation_safely(self, experience: ConversationExperience) -> None:
        try:
            self._apply_observation(experience)
        except Exception:
            self.repository.record_event(
                experience.experience_id,
                "post_processing_failed",
                {"error_code": "conversation_learning_post_processing_failed"},
            )

    def _apply_observation(self, experience: ConversationExperience) -> None:
        observation = experience.observation
        if observation is None:
            return
        for index, signal in enumerate(observation.signals):
            if signal.signal_type == ExperienceSignalType.CAREER_FACT:
                # Stage 2: career facts become career-evidence candidates; ordinary memory never
                # turns itself into resume evidence.
                self._create_evidence_candidate(experience, signal, index)
            elif signal.signal_type == ExperienceSignalType.PREFERENCE:
                self._create_memory_candidate(experience, signal, index)
            elif signal.reusable_across_sessions and signal.confidence >= 0.7:
                pattern = self.repository.aggregate_procedural_signal(experience, signal)
                if pattern.status == LearningPatternStatus.READY_FOR_REVIEW:
                    event, _ = self._feedback.record(
                        owner_id=self._owner_id,
                        source_type=FeedbackSourceType.CONVERSATION_PATTERN,
                        source_action_id=f"conversation-pattern:{pattern.pattern_id}",
                        original_content=pattern.proposed_instruction,
                        session_id=experience.session_id,
                        application_id=experience.application_id,
                        context_metadata_json={
                            "canonical_key": pattern.canonical_key,
                            "experience_count": pattern.occurrence_count,
                            "session_count": pattern.session_count,
                            "experience_ids": pattern.experience_ids,
                            "provenance": "conversation_trajectory",
                        },
                    )
                    self.repository.mark_candidate_emitted(
                        pattern.pattern_id,
                        expected_version=pattern.version,
                        feedback_event_id=event.feedback_event_id,
                    )

    @staticmethod
    def _validate_observation(
        observation: LearningObservation, user_text: str
    ) -> LearningObservation:
        accepted: list[ExperienceSignal] = []
        lowered = user_text.casefold()
        for signal in observation.signals:
            if signal.signal_type == ExperienceSignalType.NONE:
                continue
            searchable = f"{signal.summary}\n{signal.evidence_quote or ''}".casefold()
            if any(phrase in searchable for phrase in _SUSPICIOUS):
                continue
            # Every durable signal must point to words the user actually
            # supplied. Assistant/tool/JD text is context, never learning
            # evidence by itself.
            quote = (signal.evidence_quote or "").strip()
            if not quote or quote.casefold() not in lowered:
                continue
            if not signal.canonical_key or not signal.summary.strip():
                continue
            # Stage 2: words the user quoted from a posting, recruiter or document describe
            # someone else; they are never learned as the user's own fact or preference.
            if signal.attribution != "user_self" and signal.signal_type in {
                    ExperienceSignalType.PREFERENCE, ExperienceSignalType.CAREER_FACT}:
                continue
            accepted.append(signal)
        return LearningObservation(signals=accepted, turn_outcome=observation.turn_outcome)

    def _scope_for(self, experience: ConversationExperience, signal: ExperienceSignal) -> tuple[MemoryScope, str]:
        """The extractor proposes; the server decides what the conversation can support."""
        if signal.scope_hint == "application" and experience.application_id:
            return MemoryScope.APPLICATION, experience.application_id
        if signal.scope_hint in {"application", "session"} or signal.durability == "temporary":
            # An application-only or temporary statement never becomes a long-term preference;
            # without a known application it stays with this conversation.
            return MemoryScope.SESSION, experience.session_id
        return MemoryScope.USER, self._owner_id

    def _create_memory_candidate(
        self, experience: ConversationExperience, signal: ExperienceSignal, index: int
    ) -> None:
        from eregion.runtime.memory.consolidation import write_statement
        from eregion.runtime.memory.normalization import normalize_key, parse_validity_date
        key = normalize_key(signal.canonical_key)
        if not key:
            return
        scope, scope_id = self._scope_for(experience, signal)
        valid_from, valid_to = parse_validity_date(signal.valid_from), parse_validity_date(signal.valid_to)
        if valid_from and valid_to and valid_to <= valid_from:
            valid_from = valid_to = None   # contradictory dates are unknown, not guessed
        memory_id = str(uuid5(
            NAMESPACE_URL, f"conversation:{experience.experience_id}:{index}:memory"
        ))
        try:
            write_statement(
                self._memories, owner_id=self._owner_id, scope=scope, scope_id=scope_id, key=key,
                value=signal.normalized_value, display_text=signal.summary,
                memory_type=MemoryType.PREFERENCE, memory_id=memory_id,
                extra_content={"source_quote": signal.evidence_quote},
                provenance=MemoryProvenance(
                    source_type="conversation_observation",
                    source_id=experience.experience_id,
                    actor_type="user",
                    user_confirmed=False,
                    metadata={
                        "session_id": experience.session_id,
                        "user_message_id": experience.user_message_id,
                        "source_quote": signal.evidence_quote,
                    },
                ),
                sensitivity=(MemorySensitivity.SENSITIVE if signal.sensitive
                             else MemorySensitivity.PERSONAL),
                confidence=signal.confidence, valid_from=valid_from, valid_to=valid_to,
                applies_to=dict(signal.applies_to),
                canonical_scope_id=getattr(self._memories, "_canonical", None),
            )
        except MemoryAlreadyExistsError:
            return

    def _create_evidence_candidate(
        self, experience: ConversationExperience, signal: ExperienceSignal, index: int
    ) -> None:
        evidence = getattr(self._feedback, "evidence", None)
        quote = (signal.evidence_quote or "").strip()
        if evidence is None or not quote:
            return
        evidence_id = str(uuid5(NAMESPACE_URL, f"conversation:{experience.experience_id}:{index}:evidence"))
        from eregion.runtime.evidence.errors import EvidenceNotFoundError
        try:
            evidence.get(evidence_id)
            return
        except EvidenceNotFoundError:
            pass
        evidence.create_candidate(category="experience", claim_text=quote, source_type="user_attested",
            exact_quote=quote, created_by=self._owner_id,
            source_reference=f"conversation:{experience.experience_id}",
            tags=["extraction:conversation-v2"], evidence_id=evidence_id)
