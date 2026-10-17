"""Server-resolved memory access: who is asking, for which scopes, and for what purpose.

Every reader (chat context, application materials, sub-tasks, the API) builds one
MemoryAccessContext here and checks items with `usable`. Callers never pass their own owner or
scope filters, so the rules cannot drift between paths.

Scope conventions (memory upgrade stage 1):
- USER memory uses the owner ID as scope_id. Earlier API writes used the local profile ID
  ("default"); those remain readable as an alias of the same owner until migrated.
- Application exceptions are stored as PROJECT + "application:<id>" (legacy format). They apply
  only when the request concerns that application and are never treated as a project.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from eregion.runtime.memory.policy import LocalOwnerResolver
from eregion.runtime.memory.types import MemoryItem, MemoryScope, MemorySensitivity, MemoryStatus

APPLICATION_SCOPE_PREFIX = "application:"


class MemoryPurpose(StrEnum):
    CONVERSATION = "conversation"            # the owner's own job-search chat
    APPLICATION_MATERIALS = "application_materials"  # resume / cover letter / pack generation
    SUBTASK = "subtask"                      # memory IDs a parent task hands to a child task
    MANAGEMENT = "management"                # the owner reviewing their own memory (API)


# Personal preferences are the owner's own facts used to serve the owner. Sensitive memory is
# never placed in model context by these purposes.
_PERSONAL_ALLOWED = frozenset({MemoryPurpose.CONVERSATION, MemoryPurpose.APPLICATION_MATERIALS,
                               MemoryPurpose.SUBTASK, MemoryPurpose.MANAGEMENT})


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def validity_reason(item: MemoryItem, now: datetime) -> str | None:
    """None when the stated fact holds now; unknown bounds (None) never exclude."""
    current = _utc(now)
    if item.valid_from is not None and _utc(item.valid_from) > current:
        return "not_yet_valid"
    if item.valid_to is not None and _utc(item.valid_to) <= current:
        return "no_longer_valid"
    return None


def application_scope_id(application_id: str) -> str:
    return APPLICATION_SCOPE_PREFIX + application_id


def legacy_application_id(item: MemoryItem) -> str | None:
    if item.scope == MemoryScope.PROJECT and item.scope_id.startswith(APPLICATION_SCOPE_PREFIX):
        return item.scope_id[len(APPLICATION_SCOPE_PREFIX):] or None
    return None


@dataclass(frozen=True)
class MemoryAccessContext:
    owner_id: str
    purpose: MemoryPurpose
    project_id: str | None = None
    application_id: str | None = None
    session_id: str | None = None
    # Accepted USER scope IDs: the owner ID plus legacy aliases of the same local owner.
    user_scope_ids: frozenset[str] = field(default_factory=frozenset)

    @property
    def allow_personal(self) -> bool:
        return self.purpose in _PERSONAL_ALLOWED

    def in_scope(self, item: MemoryItem) -> bool:
        if item.scope == MemoryScope.USER:
            return item.scope_id in self.user_scope_ids
        if item.scope == MemoryScope.PROJECT:
            application = legacy_application_id(item)
            if application is not None:
                return application == self.application_id
            return self.project_id is not None and item.scope_id == self.project_id
        if item.scope == MemoryScope.SESSION:
            return self.session_id is not None and item.scope_id == self.session_id
        if item.scope == MemoryScope.APPLICATION:
            return self.application_id is not None and item.scope_id == self.application_id
        return False

    def specificity(self, item: MemoryItem) -> int:
        """More specific values override broader ones for the same key."""
        if item.scope == MemoryScope.SESSION:
            return 4
        if item.scope == MemoryScope.APPLICATION or legacy_application_id(item) is not None:
            return 3
        return 2 if item.scope == MemoryScope.PROJECT else 1

    def usable(self, item: MemoryItem, *, now: datetime) -> tuple[bool, str]:
        """(usable, reason). Only confirmed, unexpired, in-scope items of this owner pass."""
        if item.owner_id != self.owner_id:
            return False, "other_owner"
        if item.status != MemoryStatus.CONFIRMED:
            return False, f"status_{item.status.value}"
        expires = item.expires_at
        if expires is not None:
            expires = expires.replace(tzinfo=UTC) if expires.tzinfo is None else expires.astimezone(UTC)
            current = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
            if expires <= current:
                return False, "expired"
        validity = validity_reason(item, now)
        if validity:
            return False, validity
        if not self.in_scope(item):
            return False, "out_of_scope"
        if item.sensitivity == MemorySensitivity.SENSITIVE:
            return False, "sensitive"
        if item.sensitivity == MemorySensitivity.PERSONAL and not self.allow_personal:
            return False, "personal_not_allowed"
        return True, "usable"


class MemoryAccessResolver:
    """Builds access contexts from server state only (owner resolver, configured project).
    A session that belongs to another owner gets no memory at all."""

    def __init__(self, owner_resolver: LocalOwnerResolver | None = None, *, project_id: str | None = None) -> None:
        self._owner_resolver = owner_resolver or LocalOwnerResolver()
        self._project_id = project_id

    @property
    def owner_id(self) -> str:
        return self._owner_resolver.resolve().owner_id

    def user_scope_ids(self) -> frozenset[str]:
        profile = self._owner_resolver.resolve()
        return frozenset({profile.owner_id, profile.profile_id})

    def canonical_scope_id(self, scope: MemoryScope, scope_id: str) -> str:
        """The scope ID new writes should use (USER: the owner ID)."""
        if scope == MemoryScope.USER and scope_id in self.user_scope_ids():
            return self.owner_id
        return scope_id

    def context(self, purpose: MemoryPurpose, *, session_owner: str | None = None,
                session_id: str | None = None, application_id: str | None = None) -> MemoryAccessContext | None:
        owner = self.owner_id
        if session_owner not in {None, owner}:
            return None
        return MemoryAccessContext(owner_id=owner, purpose=purpose, project_id=self._project_id,
                                   application_id=application_id, session_id=session_id,
                                   user_scope_ids=self.user_scope_ids())
