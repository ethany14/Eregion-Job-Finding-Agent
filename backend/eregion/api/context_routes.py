"""Governed Memory/Skill APIs and privacy-filtered Session context summaries."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Query, status

from eregion.runtime.context.snapshots import ContextSnapshot
from eregion.runtime.memory.policy import LocalOwnerResolver, MemoryPolicy
from eregion.runtime.memory.types import (
    MemoryItem,
    MemoryProvenance,
    MemoryScope,
    MemoryStatus,
)
from eregion.runtime.skills.registry import SkillRegistry
from eregion.runtime.skills.types import SkillVersion
from eregion.api.context_schemas import (
    ContextMemoryUsage,
    ContextSkillUsage,
    CreateMemoryRequest,
    MemoryListResponse,
    MemorySupersedeResponse,
    PublicContextSnapshot,
    PublicMemory,
    PublicSkillSummary,
    PublicSkillVersion,
    SessionContextResponse,
    SkillListResponse,
    SkillVersionsResponse,
    SupersedeMemoryRequest,
    VersionedContextMutation,
)
from eregion.api.session_dependencies import SessionRuntime, get_session_runtime
from eregion.runtime.feedback.types import FeedbackSourceType
from eregion.api.feedback_instrumentation import record_action


router = APIRouter(tags=["context-management"])

_CREDENTIAL_VALUE = re.compile(
    r"(?i)\b(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|secret|authorization)"
    r"(\s*[:=]\s*)([^\s`]+)"
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_WINDOWS_PATH = re.compile(r"(?i)\b[a-z]:\\[^\r\n`]+")
_LOCAL_POSIX_PATH = re.compile(r"(?<!\w)/(?:users|home|tmp|var|etc)/[^\s`]+", re.IGNORECASE)


def _safe_skill_instructions(value: str) -> str:
    cleaned = _CREDENTIAL_VALUE.sub(r"\1\2[REDACTED]", value)
    cleaned = _BEARER_VALUE.sub("Bearer [REDACTED]", cleaned)
    cleaned = _WINDOWS_PATH.sub("[LOCAL_PATH]", cleaned)
    return _LOCAL_POSIX_PATH.sub("[LOCAL_PATH]", cleaned)


def _owner(runtime: SessionRuntime):
    resolver = runtime.owner_resolver or LocalOwnerResolver()
    return resolver.resolve()


def _memory_policy(runtime: SessionRuntime) -> MemoryPolicy:
    return runtime.memory_policy or MemoryPolicy()


def _memory_repository(runtime: SessionRuntime):
    if runtime.memories is None:
        raise RuntimeError("Memory Runtime is not initialized.")
    return runtime.memories


def _skill_registry(runtime: SessionRuntime) -> SkillRegistry:
    if runtime.skills is None:
        raise RuntimeError("Skill Runtime is not initialized.")
    return runtime.skill_registry or SkillRegistry(runtime.skills)


def _scope_id(runtime: SessionRuntime, scope: MemoryScope, session_id: str | None) -> str:
    profile = _owner(runtime)
    if scope == MemoryScope.USER:
        # Stage 1: USER memory is scoped to the owner ID; the profile ID ("default") written by
        # earlier versions stays readable as an alias of the same owner.
        return profile.owner_id
    if scope == MemoryScope.PROJECT:
        return runtime.project_id
    state = runtime.sessions.require(session_id or "")
    if state.user_id not in {None, profile.owner_id}:
        # Local ownership is server-owned; do not disclose whether another owner's
        # Session exists.
        from eregion.runtime.sessions.errors import SessionNotFoundError
        raise SessionNotFoundError("The session was not found.")
    return state.session_id


def _public_memory(item: MemoryItem) -> PublicMemory:
    return PublicMemory(
        memory_id=item.memory_id,
        scope=item.scope,
        session_id=item.scope_id if item.scope == MemoryScope.SESSION else None,
        memory_key=item.memory_key,
        memory_type=item.memory_type,
        display_text=item.display_text,
        content=item.content,
        status=item.status,
        sensitivity=item.sensitivity,
        confidence=item.confidence,
        supersedes_memory_id=item.supersedes_memory_id,
        superseded_by_memory_id=item.superseded_by_memory_id,
        expires_at=item.expires_at,
        version=item.version,
        created_at=item.created_at,
        updated_at=item.updated_at,
        last_used_at=item.last_used_at,
    )


def _public_skill(version: SkillVersion) -> PublicSkillVersion:
    return PublicSkillVersion(
        version_id=version.version_id,
        name=version.name,
        description=version.description,
        version_label=version.version_label,
        status=version.status,
        instructions=_safe_skill_instructions(version.instruction_snapshot),
        license=version.license,
        compatibility=version.compatibility,
        allowed_tools=(None if version.allowed_tools is None else sorted(version.allowed_tools)),
        validation_errors=version.validation_errors,
        validation_warnings=version.validation_warnings,
        version=version.version,
        created_at=version.created_at,
        updated_at=version.updated_at,
    )


@router.get("/memories", response_model=MemoryListResponse)
def list_memories(
    scope: MemoryScope | None = None,
    session_id: str | None = Query(default=None, max_length=128),
    include_deleted: bool = False,
    runtime: SessionRuntime = Depends(get_session_runtime),
) -> MemoryListResponse:
    owner = _owner(runtime)
    # USER listing covers the owner ID and its legacy profile alias (owner_id is the boundary).
    scope_id = (_scope_id(runtime, scope, session_id)
                if scope is not None and scope != MemoryScope.USER else None)
    statuses = set(MemoryStatus)
    if not include_deleted:
        statuses.remove(MemoryStatus.DELETED)
    items = _memory_repository(runtime).list_items(
        owner_id=owner.owner_id, scope=scope, scope_id=scope_id, statuses=statuses
    )
    return MemoryListResponse(memories=[_public_memory(item) for item in items])


@router.get("/memories/{memory_id}", response_model=PublicMemory)
def get_memory(
    memory_id: str,
    runtime: SessionRuntime = Depends(get_session_runtime),
) -> PublicMemory:
    item = _memory_repository(runtime).require(memory_id, owner_id=_owner(runtime).owner_id)
    return _public_memory(item)


@router.post("/memories", response_model=PublicMemory, status_code=status.HTTP_201_CREATED)
def create_memory(
    request: CreateMemoryRequest,
    runtime: SessionRuntime = Depends(get_session_runtime),
) -> PublicMemory:
    owner = _owner(runtime)
    provenance = [MemoryProvenance(
        source_type="explicit_user", actor_type="user", user_confirmed=False
    )]
    decision = _memory_policy(runtime).assess_candidate(
        display_text=request.display_text,
        content=request.content,
        provenance=provenance,
        sensitivity=request.sensitivity,
    )
    if not decision.accepted_as_candidate:
        raise ValueError("The Memory content was rejected by policy.")
    item = _memory_repository(runtime).create_candidate(
        owner_id=owner.owner_id,
        scope=request.scope,
        scope_id=_scope_id(runtime, request.scope, request.session_id),
        memory_key=request.memory_key,
        memory_type=request.memory_type,
        display_text=request.display_text,
        content=request.content,
        provenance=provenance,
        sensitivity=request.sensitivity,
        confidence=request.confidence,
        expires_at=request.expires_at,
    )
    return _public_memory(item)


@router.post("/memories/{memory_id}/confirm", response_model=PublicMemory)
def confirm_memory(memory_id: str, request: VersionedContextMutation,
                   runtime: SessionRuntime = Depends(get_session_runtime)) -> PublicMemory:
    item = _memory_repository(runtime).confirm(
        memory_id, owner_id=_owner(runtime).owner_id,
        expected_version=request.expected_version, confirmed_by_user=True,
    )
    if item.memory_type.value == "preference":
        record_action(runtime, source_type=FeedbackSourceType.EXPLICIT_INSTRUCTION,
            source_action_id=f"memory-confirm:{memory_id}:{request.expected_version}",
            content=item.display_text)
    return _public_memory(item)


@router.post("/memories/{memory_id}/reject", response_model=PublicMemory)
def reject_memory(memory_id: str, request: VersionedContextMutation,
                  runtime: SessionRuntime = Depends(get_session_runtime)) -> PublicMemory:
    item = _memory_repository(runtime).reject(
        memory_id, owner_id=_owner(runtime).owner_id,
        expected_version=request.expected_version,
    )
    record_action(runtime, source_type=FeedbackSourceType.MANUAL_FEEDBACK,
        source_action_id=f"memory-reject:{memory_id}:{request.expected_version}",
        content="User rejected a memory candidate.")
    return _public_memory(item)


@router.post("/memories/{memory_id}/supersede", response_model=MemorySupersedeResponse)
def supersede_memory(memory_id: str, request: SupersedeMemoryRequest,
                     runtime: SessionRuntime = Depends(get_session_runtime)) -> MemorySupersedeResponse:
    repository = _memory_repository(runtime)
    owner_id = _owner(runtime).owner_id
    old = repository.supersede(
        memory_id,
        replacement_memory_id=request.replacement_memory_id,
        owner_id=owner_id,
        expected_version=request.expected_version,
        replacement_expected_version=request.replacement_expected_version,
    )
    replacement = repository.require(request.replacement_memory_id, owner_id=owner_id)
    if replacement.memory_type.value == "preference":
        record_action(runtime, source_type=FeedbackSourceType.EXPLICIT_INSTRUCTION,
            source_action_id=f"memory-supersede:{memory_id}:{request.expected_version}",
            content=replacement.display_text)
    return MemorySupersedeResponse(
        superseded=_public_memory(old), replacement=_public_memory(replacement)
    )


@router.delete("/memories/{memory_id}", response_model=PublicMemory)
def delete_memory(memory_id: str, expected_version: int = Query(ge=0),
                  runtime: SessionRuntime = Depends(get_session_runtime)) -> PublicMemory:
    return _public_memory(_memory_repository(runtime).soft_delete(
        memory_id, owner_id=_owner(runtime).owner_id, expected_version=expected_version,
    ))


@router.get("/skills", response_model=SkillListResponse)
def list_skills(runtime: SessionRuntime = Depends(get_session_runtime)) -> SkillListResponse:
    if runtime.skills is None:
        raise RuntimeError("Skill Runtime is not initialized.")
    grouped: dict[str, list[SkillVersion]] = {}
    for item in runtime.skills.discover():
        grouped.setdefault(item.name, []).append(runtime.skills.require(item.version_id))
    summaries = []
    for name, versions in grouped.items():
        latest = max(versions, key=lambda item: (item.created_at, item.version_id))
        active = runtime.skills.active_by_name(name)
        summaries.append(PublicSkillSummary(
            name=name, description=latest.description,
            active_version_id=None if active is None else active.version_id,
            latest_version=latest.version_label, latest_status=latest.status,
        ))
    return SkillListResponse(skills=sorted(summaries, key=lambda item: item.name))


@router.get("/skills/{skill_name}/versions", response_model=SkillVersionsResponse)
def skill_versions(skill_name: str,
                   runtime: SessionRuntime = Depends(get_session_runtime)) -> SkillVersionsResponse:
    if runtime.skills is None:
        raise RuntimeError("Skill Runtime is not initialized.")
    versions = runtime.skills.versions_by_name(skill_name)
    if not versions:
        from eregion.runtime.skills.errors import SkillNotFoundError
        raise SkillNotFoundError("The Skill was not found.")
    return SkillVersionsResponse(
        name=skill_name, versions=[_public_skill(item) for item in versions]
    )


@router.get("/skill-versions/{version_id}", response_model=PublicSkillVersion)
def get_skill_version(version_id: str,
                      runtime: SessionRuntime = Depends(get_session_runtime)) -> PublicSkillVersion:
    if runtime.skills is None:
        raise RuntimeError("Skill Runtime is not initialized.")
    return _public_skill(runtime.skills.require(version_id))


def _skill_mutation(version_id: str, request: VersionedContextMutation,
                    runtime: SessionRuntime, action: str) -> PublicSkillVersion:
    registry = _skill_registry(runtime)
    method = getattr(registry, action)
    return _public_skill(method(version_id, expected_version=request.expected_version))


@router.post("/skill-versions/{version_id}/approve", response_model=PublicSkillVersion)
def approve_skill(version_id: str, request: VersionedContextMutation,
                  runtime: SessionRuntime = Depends(get_session_runtime)) -> PublicSkillVersion:
    return _skill_mutation(version_id, request, runtime, "approve")


@router.post("/skill-versions/{version_id}/activate", response_model=PublicSkillVersion)
def activate_skill(version_id: str, request: VersionedContextMutation,
                   runtime: SessionRuntime = Depends(get_session_runtime)) -> PublicSkillVersion:
    return _skill_mutation(version_id, request, runtime, "activate")


@router.post("/skill-versions/{version_id}/reject", response_model=PublicSkillVersion)
def reject_skill(version_id: str, request: VersionedContextMutation,
                 runtime: SessionRuntime = Depends(get_session_runtime)) -> PublicSkillVersion:
    return _skill_mutation(version_id, request, runtime, "reject")


@router.post("/skill-versions/{version_id}/retire", response_model=PublicSkillVersion)
def retire_skill(version_id: str, request: VersionedContextMutation,
                 runtime: SessionRuntime = Depends(get_session_runtime)) -> PublicSkillVersion:
    return _skill_mutation(version_id, request, runtime, "retire")


def _public_snapshot(runtime: SessionRuntime, snapshot: ContextSnapshot) -> PublicContextSnapshot:
    if runtime.skills is None or runtime.memories is None:
        raise RuntimeError("Context Runtime is not initialized.")
    owner_id = _owner(runtime).owner_id
    skills = []
    for reference in snapshot.skill_versions:
        version = runtime.skills.require(reference.version_id)
        skills.append(ContextSkillUsage(
            version_id=version.version_id, name=version.name,
            version=version.version_label, description=version.description,
        ))
    memories = []
    for reference in snapshot.memory_versions:
        item = runtime.memories.require(reference.memory_id, owner_id=owner_id)
        memories.append(ContextMemoryUsage(
            memory_id=item.memory_id, version=reference.version,
            memory_key=item.memory_key, display_text=item.display_text,
            memory_type=item.memory_type, sensitivity=item.sensitivity,
        ))
    return PublicContextSnapshot(
        snapshot_id=snapshot.snapshot_id, status=snapshot.status,
        skills=skills, memories=memories,
        effective_tools=sorted(snapshot.effective_tools),
        estimated_input_tokens=snapshot.estimated_input_tokens,
        prepared_at=snapshot.prepared_at, used_at=snapshot.used_at,
        abandoned_at=snapshot.abandoned_at,
    )


@router.get("/sessions/{session_id}/context", response_model=SessionContextResponse)
def session_context(session_id: str,
                    runtime: SessionRuntime = Depends(get_session_runtime)) -> SessionContextResponse:
    state = runtime.sessions.require(session_id)
    if state.task_id is not None:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail={
            "code": "child_session_private", "message": "Child task context is private."})
    profile = _owner(runtime)
    if state.user_id not in {None, profile.owner_id}:
        from eregion.runtime.sessions.errors import SessionNotFoundError
        raise SessionNotFoundError("The session was not found.")
    if runtime.context_snapshots is None:
        raise RuntimeError("Context Runtime is not initialized.")
    ids = list(dict.fromkeys(filter(None, [
        state.current_context_snapshot_id, state.last_context_snapshot_id,
    ])))
    snapshots = [
        _public_snapshot(runtime, runtime.context_snapshots.require(snapshot_id))
        for snapshot_id in ids
    ]
    return SessionContextResponse(
        session_id=state.session_id,
        current_snapshot_id=state.current_context_snapshot_id,
        last_snapshot_id=state.last_context_snapshot_id,
        snapshots=snapshots,
    )
