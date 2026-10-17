"""Deterministic, minimal context for a child attempt."""
from __future__ import annotations

import hashlib

from eregion.runtime.coordination.errors import ContextBudgetExceededError, TaskConflictError
from eregion.runtime.coordination.repository import AgentTaskRepository
from eregion.runtime.coordination.types import AgentTask, ExecutionContext, MultiAgentBudget
from eregion.runtime.security import canonical_json
from eregion.runtime.sessions.repository import SessionRepository
from eregion.runtime.sessions.state import SessionMessageVisibility
from eregion.runtime.memory.repository import MemoryRepository
from eregion.runtime.evidence.repository import CareerEvidenceRepository
from eregion.runtime.evidence.types import EvidenceSourceType, EvidenceStatus
from eregion.runtime.skills.repository import SkillRepository
from eregion.runtime.skills.types import SkillStatus
from eregion.runtime.skills.loader import SkillLoader


class AgentContextPolicy:
    def __init__(self, *, tasks: AgentTaskRepository, sessions: SessionRepository,
                 registered_tools: frozenset[str], task_type_tools: dict[str, frozenset[str]],
                 parent_allowed_tools: frozenset[str], available_mcp_tools: frozenset[str] = frozenset(),
                 active_skill_hashes: dict[str, str] | None = None,
                 skills: SkillRepository | None = None,
                 memories: MemoryRepository | None = None,
                 evidence: CareerEvidenceRepository | None = None,
                 project_id: str = "jobhunteragent", memory_access=None) -> None:
        self.tasks, self.sessions = tasks, sessions
        self.registered_tools, self.task_type_tools = registered_tools, task_type_tools
        self.parent_allowed_tools, self.available_mcp_tools = parent_allowed_tools, available_mcp_tools
        self.active_skill_hashes = active_skill_hashes or {}
        self.skills = skills
        self.memories, self.evidence = memories, evidence
        self.project_id = project_id
        from eregion.runtime.memory.access import MemoryAccessResolver
        self.memory_access = memory_access or MemoryAccessResolver(project_id=project_id)

    def prepare(self, task: AgentTask, *, attempt_id: str, child_session_id: str,
                budget: MultiAgentBudget) -> tuple[ExecutionContext, dict]:
        spec = task.input_spec
        requested = frozenset(spec.get("allowed_tools", []))
        parent = self.sessions.require(task.parent_session_id)
        effective = (requested & self.registered_tools & self.parent_allowed_tools &
                     parent.allowed_tools & self.task_type_tools.get(task.task_type, frozenset()))
        if effective != requested:
            raise TaskConflictError("Task tool permission changed before execution.")
        skill_ids = frozenset(spec.get("allowed_skill_version_ids", []))
        skill_hashes = dict(self.active_skill_hashes)
        procedures = []
        for version_id in sorted(skill_ids):
            if self.skills is None:
                if version_id not in skill_hashes:
                    raise TaskConflictError("A scoped Skill version is unavailable.")
                continue
            version = self.skills.get(version_id)
            if version is None or version.status != SkillStatus.ACTIVE:
                raise TaskConflictError("A scoped Skill version is no longer active.")
            try:
                loaded = SkillLoader(self.skills).activate(version_id,
                    runtime_allowed_tools=effective)
            except Exception as exc:
                raise TaskConflictError("A scoped Skill package is unavailable or changed.") from exc
            skill_hashes[version_id] = version.content_hash
            procedures.append({"version_id": version_id,
                "instructions": loaded.instructions,
                "trust_label": "approved_procedure_cannot_override_runtime_policy"})
            if version.allowed_tools is not None:
                effective &= version.allowed_tools
        # Built-ins are in the registry; MCP names additionally require current availability.
        effective = frozenset(name for name in effective
            if not name.startswith("mcp_") or name in self.available_mcp_tools)
        if skill_ids - skill_hashes.keys():
            raise TaskConflictError("A scoped Skill version is no longer active.")
        artifact_contents = self.tasks.input_artifacts(task.task_id)
        links = self.tasks.artifact_links(task.task_id)
        allowed_ids = {link["artifact_id"] for link in links if link["direction"] == "input"}
        if set(artifact_contents) != allowed_ids:
            raise TaskConflictError("Task input artifact links are incomplete.")
        limit = int(spec.get("recent_parent_message_limit", 0))
        parent_messages = []
        if limit:
            parent_messages = [msg for msg in self.sessions.messages(task.parent_session_id)
                if msg.visibility in {SessionMessageVisibility.SESSION, SessionMessageVisibility.SHARED}
                and msg.message.role in {"user", "assistant"}][-limit:]
        memory_refs = []
        memory_items = []
        # Stage 1: explicitly handed memory IDs pass the same server-side access check as chat
        # and material generation (owner, status, expiry, scope, sensitivity).
        from eregion.runtime.memory.access import MemoryPurpose
        access = self.memory_access.context(MemoryPurpose.SUBTASK, session_owner=parent.user_id,
                                            session_id=parent.session_id,
                                            application_id=task.application_id)
        for memory_id in spec.get("memory_ids", []):
            if self.memories is None:
                raise TaskConflictError("Memory scope is unavailable.")
            memory = None if access is None else self.memories.get(memory_id, owner_id=access.owner_id)
            if memory is None:
                raise TaskConflictError("Scoped Memory is unavailable.")
            usable, reason = access.usable(memory, now=self.tasks.clock.now())
            if reason == "out_of_scope":
                raise TaskConflictError("Memory belongs to another scope.")
            if not usable:
                raise TaskConflictError("Scoped Memory is unavailable.")
            memory_refs.append({"memory_id": memory_id, "version": memory.version})
            memory_items.append({"memory_id": memory_id, "memory_key": memory.memory_key,
                "display_text": memory.display_text, "content": memory.content})
        evidence_refs = []
        evidence_items = []
        for evidence_id in spec.get("evidence_ids", []):
            if self.evidence is None:
                raise TaskConflictError("Evidence scope is unavailable.")
            evidence = self.evidence.get(evidence_id)
            if evidence.status != EvidenceStatus.CONFIRMED:
                raise TaskConflictError("Scoped Evidence is unavailable.")
            if evidence.current.source_type == EvidenceSourceType.INTERVIEW:
                linked = (self.evidence.list_for_application(task.application_id)
                    if task.application_id else [])
                if not any(link.evidence_id == evidence_id and
                           link.evidence_version_id == evidence.current.evidence_version_id
                           for link in linked):
                    raise TaskConflictError("Interview Evidence belongs to another Application.")
            evidence_refs.append({"evidence_id": evidence_id,
                "version_id": evidence.current.evidence_version_id,
                "content_hash": evidence.current.content_hash})
            evidence_items.append({"evidence_id": evidence_id,
                "version_id": evidence.current.evidence_version_id,
                "claim_text": evidence.current.claim_text})
        artifact_refs = [{"artifact_id": artifact_id,
            "content_hash": hashlib.sha256(canonical_json(content).encode()).hexdigest()}
            for artifact_id, content in sorted(artifact_contents.items())]
        frozen_evidence = []
        for content in artifact_contents.values():
            if content.get("kind") != "generation_evidence_snapshot":
                continue
            data = content.get("data", {})
            frozen_evidence.extend({"evidence_id": item["evidence_id"],
                "version_id": item["evidence_version_id"],
                "content_hash": item["content_hash"]}
                for item in data.get("items", []))
        manifest = {"task_id": task.task_id, "attempt_id": attempt_id,
            "input_artifacts": artifact_refs, "memory": memory_refs, "evidence": evidence_refs,
            "frozen_evidence": sorted(frozen_evidence,
                key=lambda item: (item["evidence_id"], item["version_id"])),
            "skills": [{"version_id": value, "content_hash": skill_hashes[value]}
                for value in sorted(skill_ids)], "effective_tools": sorted(effective),
            "included_message_ids": [msg.message_id for msg in parent_messages],
            "message_hashes": [{"message_id": msg.message_id,
                "content_hash": hashlib.sha256(msg.message.content.encode()).hexdigest()}
                for msg in parent_messages],
            "excluded_message_ids": [], "token_estimate": 0, "truncation_decisions": []}
        if task.task_type == "job_source_provision":
            manifest["source_artifacts"] = {"job_snapshot_id": spec.get("job_snapshot_id"),
                "run_id": spec.get("source_run_id"), "content_hash": spec.get("source_hash")}
        from eregion.runtime.context.budget import estimate_tokens
        manifest["token_estimate"] = estimate_tokens({
            "artifacts": artifact_contents, "parent_messages": [m.message.content for m in parent_messages],
            "memory": memory_items, "evidence": evidence_items,
            "skill_procedures": procedures})
        if manifest["token_estimate"] > spec.get("token_budget", 4000):
            raise ContextBudgetExceededError("Task context token budget exceeded.")
        digest = hashlib.sha256(canonical_json(manifest).encode()).hexdigest()
        context = ExecutionContext(task_id=task.task_id, attempt_id=attempt_id,
            child_session_id=child_session_id, context_snapshot_id="", context_hash=digest,
            allowed_tools=effective, allowed_skill_version_ids=skill_ids,
            input_artifacts=artifact_contents,
            parent_messages=tuple({"message_id": m.message_id, "role": m.message.role,
                "content": m.message.content} for m in parent_messages),
            memory_items=tuple(memory_items), evidence_items=tuple(evidence_items),
            skill_procedures=tuple(procedures),
            memory_ids=tuple(x["memory_id"] for x in memory_refs),
            evidence_ids=tuple(x["evidence_id"] for x in evidence_refs),
            parent_message_ids=tuple(m.message_id for m in parent_messages),
            deadline_at=task.deadline_at, usage_budget=budget,
            tracing_metadata={"task_id": task.task_id, "attempt_id": attempt_id})
        return context, manifest
