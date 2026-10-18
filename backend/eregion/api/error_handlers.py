"""Stable, non-sensitive HTTP mappings for session runtime errors."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from eregion.runtime.errors import (
    ApprovalBindingError,
    IdempotencyConflictError,
    InvalidToolCallTransitionError,
    StaleToolCallError,
    ToolCallNotFoundError,
    UnknownToolError,
)
from eregion.runtime.sessions.errors import (
    InvalidSessionTransitionError,
    MessageConflictError,
    SessionAlreadyExistsError,
    SessionClaimConflictError,
    SessionClaimNotOwnedError,
    SessionNotFoundError,
    SessionTerminalError,
    StaleSessionError,
)
from eregion.api.services.run_service import RunNotFoundError
from eregion.runtime.context.snapshots import ContextSnapshotUnavailableError
from eregion.runtime.memory.errors import (
    InvalidMemoryTransitionError,
    MemoryAlreadyExistsError,
    MemoryConfirmationRequiredError,
    MemoryNotFoundError,
    MemoryReplacementNotValidError,
    MemoryOwnerMismatchError,
    StaleMemoryError,
)
from eregion.runtime.skills.errors import (
    InvalidSkillTransitionError,
    SkillContentChangedError,
    SkillNotFoundError,
    SkillSecurityError,
    SkillValidationError,
    SkillVersionConflictError,
    StaleSkillVersionError,
)
from eregion.runtime.workspace.errors import (
    ApplicationNotFoundError,
    ArtifactNotFoundError,
    ArtifactValidationError,
    InvalidApplicationTransitionError,
    JobNotFoundError,
    SnapshotNotFoundError,
    StaleApplicationError,
    WorkspaceAssociationError,
)
from eregion.runtime.evidence.errors import (
    EvidenceNotFoundError, StaleEvidenceError, EvidenceProvenanceError,
    InvalidEvidenceTransitionError,
)
from eregion.runtime.interviewer.errors import (
    InterviewNotFoundError, InterviewConflictError, InterviewValidationError,
    InterviewStaleVersionError,
)
from eregion.runtime.application_pack.errors import (
    PackNotFoundError, PackConflictError, PackValidationError,
)
from eregion.runtime.coordination.errors import (
    TaskNotFoundError, TaskConflictError, InvalidPlanError, BudgetExceededError,
)
from eregion.runtime.mock_interview.errors import (
    MockInterviewNotFound, MockInterviewConflict, MockInterviewValidation,
)
from eregion.runtime.feedback.errors import (
    FeedbackNotFoundError, LearningCandidateNotFoundError,
    FeedbackConflictError, FeedbackValidationError,
)
from eregion.runtime.assistant.actions import (
    AssistantActionConflictError, AssistantActionValidationError,
)

logger = logging.getLogger(__name__)


def _response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": {"code": code, "message": message}},
    )


_SAFE_MOCK_INTERVIEW_VALIDATION_MESSAGES = frozenset({
    "Interview limits are out of range.",
    "An approved Pack for the current Job is required.",
    "Pinned Career Evidence is unavailable or changed.",
    "Analyze this Job before starting a mock interview.",
    "Answer must contain 1 to 20,000 characters.",
    "Question count must be between 3 and 12.",
    "Current Job analysis has no requirements.",
})

_SAFE_EVIDENCE_INTERVIEW_VALIDATION_MESSAGES = frozenset({
    "Analyze the current Job snapshot before interviewing.",
    "Interview limits are out of range.",
    "Answer must contain 1 to 20,000 characters.",
    "The answer needs a concrete supporting quote.",
    "A supporting quote was not present in the answer.",
    "The proposed claim must use the user's own wording.",
    "The proposed claim must equal an exact supporting quote.",
    "The answer contains an instruction rather than verifiable experience.",
    "Interview context exceeds its token budget.",
})


def _safe_validation_message(error: Exception, allowed: frozenset[str],
                             fallback: str) -> str:
    message = str(error)
    return message if message in allowed else fallback


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AssistantActionConflictError)
    async def assistant_action_conflict(_: Request, __: AssistantActionConflictError) -> JSONResponse:
        return _response(409, "assistant_action_conflict",
                         "Assistant state changed; refresh before retrying the action.")

    @app.exception_handler(AssistantActionValidationError)
    async def assistant_action_invalid(_: Request, __: AssistantActionValidationError) -> JSONResponse:
        return _response(422, "invalid_assistant_action",
                         "This action is unavailable or incomplete in the current Workspace.")

    @app.exception_handler(FeedbackNotFoundError)
    @app.exception_handler(LearningCandidateNotFoundError)
    async def feedback_missing(_: Request, __: Exception) -> JSONResponse:
        return _response(404, "feedback_not_found", "The feedback record was not found.")

    @app.exception_handler(FeedbackConflictError)
    async def feedback_conflict(_: Request, __: FeedbackConflictError) -> JSONResponse:
        return _response(409, "feedback_conflict", "Feedback state changed; refresh and try again.")

    @app.exception_handler(FeedbackValidationError)
    async def feedback_invalid(_: Request, __: FeedbackValidationError) -> JSONResponse:
        return _response(422, "invalid_feedback", "Feedback input or action is invalid.")
    @app.exception_handler(MockInterviewNotFound)
    async def mock_missing(_: Request, __: MockInterviewNotFound) -> JSONResponse:
        return _response(404, "mock_interview_not_found", "The mock interview was not found.")

    @app.exception_handler(MockInterviewConflict)
    async def mock_conflict(_: Request, __: MockInterviewConflict) -> JSONResponse:
        return _response(409, "mock_interview_conflict", "Interview state changed; refresh and try again.")

    @app.exception_handler(MockInterviewValidation)
    async def mock_invalid(_: Request, error: MockInterviewValidation) -> JSONResponse:
        return _response(422, "invalid_mock_interview", _safe_validation_message(
            error, _SAFE_MOCK_INTERVIEW_VALIDATION_MESSAGES,
            "Interview input or source is invalid."))

    @app.exception_handler(TaskNotFoundError)
    async def task_missing(_: Request, __: TaskNotFoundError) -> JSONResponse:
        return _response(404, "task_not_found", "The task was not found.")

    @app.exception_handler(InvalidPlanError)
    async def invalid_plan(_: Request, __: InvalidPlanError) -> JSONResponse:
        return _response(422, "invalid_plan", "The server-approved plan is invalid or unavailable.")

    @app.exception_handler((TaskConflictError))
    async def task_conflict(_: Request, __: TaskConflictError) -> JSONResponse:
        return _response(409, "task_conflict", "Task state changed; refresh and try again.")

    @app.exception_handler(BudgetExceededError)
    async def task_budget(_: Request, __: BudgetExceededError) -> JSONResponse:
        return _response(409, "task_budget_exceeded", "The task budget was reached.")

    @app.exception_handler(PackNotFoundError)
    async def pack_missing(_: Request, __: PackNotFoundError) -> JSONResponse:
        return _response(404, "pack_not_found", "The Pack, item, or Application was not found.")

    @app.exception_handler(PackConflictError)
    async def pack_conflict(_: Request, __: PackConflictError) -> JSONResponse:
        return _response(409, "pack_conflict", "Pack state changed; refresh and try again.")

    @app.exception_handler(PackValidationError)
    async def pack_invalid(_: Request, __: PackValidationError) -> JSONResponse:
        return _response(422, "invalid_pack_input", "Pack inputs or source are invalid.")
    @app.exception_handler(InterviewNotFoundError)
    async def interview_missing(_: Request, __: InterviewNotFoundError) -> JSONResponse:
        return _response(404, "interview_not_found", "The interview or Application was not found.")

    @app.exception_handler(InterviewConflictError)
    async def interview_conflict(_: Request, __: InterviewConflictError) -> JSONResponse:
        return _response(409, "interview_conflict", "Interview state changed; refresh and try again.")

    @app.exception_handler(InterviewStaleVersionError)
    async def interview_stale(_: Request, __: InterviewStaleVersionError) -> JSONResponse:
        return _response(409, "stale_interview", "Interview version changed; refresh and try again.")

    @app.exception_handler(InterviewValidationError)
    async def interview_invalid(_: Request, error: InterviewValidationError) -> JSONResponse:
        return _response(422, "invalid_interview_input", _safe_validation_message(
            error, _SAFE_EVIDENCE_INTERVIEW_VALIDATION_MESSAGES,
            "Interview input or source is invalid."))
    @app.exception_handler(EvidenceNotFoundError)
    async def evidence_missing(_: Request, __: EvidenceNotFoundError) -> JSONResponse:
        return _response(404, "evidence_not_found", "The evidence record was not found.")

    @app.exception_handler(StaleEvidenceError)
    async def evidence_stale(_: Request, __: StaleEvidenceError) -> JSONResponse:
        return _response(409, "stale_evidence", "The evidence record changed; refresh it.")

    @app.exception_handler(InvalidEvidenceTransitionError)
    async def evidence_transition(_: Request, __: InvalidEvidenceTransitionError) -> JSONResponse:
        return _response(409, "invalid_evidence_transition", "This evidence action is not allowed now.")

    @app.exception_handler(EvidenceProvenanceError)
    async def evidence_provenance(_: Request, __: EvidenceProvenanceError) -> JSONResponse:
        return _response(422, "invalid_evidence_provenance", "Evidence source or wording is invalid.")
    @app.exception_handler(SessionNotFoundError)
    async def session_not_found(_: Request, __: SessionNotFoundError) -> JSONResponse:
        return _response(404, "session_not_found", "The session was not found.")

    @app.exception_handler(ToolCallNotFoundError)
    async def tool_call_not_found(_: Request, __: ToolCallNotFoundError) -> JSONResponse:
        return _response(404, "tool_call_not_found", "The tool call was not found.")

    @app.exception_handler(RunNotFoundError)
    async def run_not_found(_: Request, __: RunNotFoundError) -> JSONResponse:
        return _response(404, "run_not_found", "The associated run was not found.")

    @app.exception_handler(MemoryNotFoundError)
    async def memory_not_found(_: Request, __: MemoryNotFoundError) -> JSONResponse:
        return _response(404, "memory_not_found", "The Memory item was not found.")

    @app.exception_handler(SkillNotFoundError)
    async def skill_not_found(_: Request, __: SkillNotFoundError) -> JSONResponse:
        return _response(404, "skill_not_found", "The Skill version was not found.")

    async def workspace_not_found(_: Request, exc: Exception) -> JSONResponse:
        code = (
            "job_not_found" if isinstance(exc, JobNotFoundError)
            else "snapshot_not_found" if isinstance(exc, SnapshotNotFoundError)
            else "artifact_not_found" if isinstance(exc, ArtifactNotFoundError)
            else "application_not_found"
        )
        return _response(404, code, "The requested workspace record was not found.")

    for error_type in (JobNotFoundError, SnapshotNotFoundError,
                       ApplicationNotFoundError, ArtifactNotFoundError):
        app.add_exception_handler(error_type, workspace_not_found)

    conflict_errors = (
        SessionAlreadyExistsError,
        StaleSessionError,
        MessageConflictError,
        InvalidSessionTransitionError,
        SessionTerminalError,
        SessionClaimConflictError,
        SessionClaimNotOwnedError,
        StaleToolCallError,
        IdempotencyConflictError,
        ApprovalBindingError,
        InvalidToolCallTransitionError,
        MemoryAlreadyExistsError,
        StaleMemoryError,
        InvalidMemoryTransitionError,
        StaleSkillVersionError,
        SkillVersionConflictError,
        InvalidSkillTransitionError,
        SkillContentChangedError,
        ContextSnapshotUnavailableError,
        StaleApplicationError,
        InvalidApplicationTransitionError,
    )

    async def conflict(_: Request, exc: Exception) -> JSONResponse:
        codes = {
            StaleSessionError: "stale_session_version",
            MessageConflictError: "message_conflict",
            SessionClaimConflictError: "session_claim_conflict",
            SessionClaimNotOwnedError: "session_claim_not_owned",
            StaleMemoryError: "stale_memory_version",
            MemoryAlreadyExistsError: "memory_value_conflict",
            StaleSkillVersionError: "stale_skill_version",
            SkillContentChangedError: "skill_content_changed",
            ContextSnapshotUnavailableError: "context_snapshot_unavailable",
        }
        code = next((value for kind, value in codes.items() if isinstance(exc, kind)), "invalid_state")
        return _response(409, code, "The request conflicts with the current persisted state.")

    for error_type in conflict_errors:
        app.add_exception_handler(error_type, conflict)

    @app.exception_handler(UnknownToolError)
    async def forbidden_tool(_: Request, __: UnknownToolError) -> JSONResponse:
        return _response(403, "tool_not_allowed", "The requested capability is not allowed.")

    @app.exception_handler(MemoryOwnerMismatchError)
    async def forbidden_memory(_: Request, __: MemoryOwnerMismatchError) -> JSONResponse:
        return _response(403, "memory_not_allowed", "The Memory item is not accessible.")

    @app.exception_handler(SkillSecurityError)
    async def forbidden_skill(_: Request, __: SkillSecurityError) -> JSONResponse:
        return _response(403, "skill_not_allowed", "The Skill package is not allowed.")

    @app.exception_handler(SkillValidationError)
    async def invalid_skill(_: Request, __: SkillValidationError) -> JSONResponse:
        return _response(422, "skill_validation_failed", "The Skill failed validation.")

    @app.exception_handler(MemoryReplacementNotValidError)
    async def replacement_not_valid(_: Request, __: MemoryReplacementNotValidError) -> JSONResponse:
        return _response(409, "memory_replacement_not_valid",
                         "The replacement is not currently valid. The current memory was kept; "
                         "supersede only during the replacement's validity period.")

    @app.exception_handler(MemoryConfirmationRequiredError)
    async def confirmation_required(_: Request, __: MemoryConfirmationRequiredError) -> JSONResponse:
        return _response(422, "memory_confirmation_required", "Explicit confirmation is required.")

    @app.exception_handler(ArtifactValidationError)
    async def invalid_artifact(_: Request, __: ArtifactValidationError) -> JSONResponse:
        return _response(422, "invalid_artifact", "The artifact content is invalid.")

    @app.exception_handler(WorkspaceAssociationError)
    async def invalid_association(_: Request, __: WorkspaceAssociationError) -> JSONResponse:
        return _response(422, "invalid_workspace_association", "The referenced record is invalid.")

    @app.exception_handler(ValueError)
    async def invalid_domain_input(_: Request, __: ValueError) -> JSONResponse:
        return _response(422, "invalid_request", "The request is invalid.")

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled Session API error", exc_info=exc)
        return _response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "The request could not be completed.",
        )
