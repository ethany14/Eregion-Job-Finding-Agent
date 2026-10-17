"""Transactional repository for governed, versioned memory."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from eregion.runtime.memory.errors import (
    InvalidMemoryTransitionError,
    MemoryAlreadyExistsError,
    MemoryConfirmationRequiredError,
    MemoryNotFoundError,
    StaleMemoryError,
)
from eregion.runtime.memory.events import MemoryEvent, MemoryEventType
from eregion.runtime.memory.models import MemoryEventRow, MemoryItemRow
from eregion.runtime.memory.policy import MemoryPolicy
from eregion.runtime.memory.types import (
    MemoryItem,
    MemoryProvenance,
    MemoryScope,
    MemorySensitivity,
    MemoryStatus,
    MemoryType,
)
from eregion.runtime.security import canonical_json, redact_sensitive
from eregion.runtime.sessions.clock import Clock, SystemClock


def ensure_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class MemoryRepository:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Clock | None = None,
        policy: MemoryPolicy | None = None,
        canonical_scope_id=None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or SystemClock()
        self._policy = policy or MemoryPolicy()
        # (scope, scope_id) -> canonical scope_id. Stage 1: the legacy USER profile ID
        # ("default") and the owner ID are one scope for duplicate and supersession checks.
        self._canonical = canonical_scope_id or (lambda scope, scope_id: scope_id)

    def create_candidate(
        self,
        *,
        owner_id: str,
        scope: MemoryScope,
        scope_id: str,
        memory_key: str,
        memory_type: MemoryType,
        display_text: str,
        content: dict[str, Any],
        provenance: Sequence[MemoryProvenance],
        sensitivity: MemorySensitivity = MemorySensitivity.NORMAL,
        confidence: float = 1.0,
        expires_at: datetime | None = None,
        memory_id: str | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        applies_to: dict[str, str] | None = None,
    ) -> MemoryItem:
        now = ensure_utc(self._clock.now())
        decision = self._policy.assess_candidate(
            display_text=display_text,
            content=content,
            provenance=list(provenance),
            sensitivity=sensitivity,
        )
        status = (
            MemoryStatus.CANDIDATE
            if decision.accepted_as_candidate
            else MemoryStatus.REJECTED
        )
        values: dict[str, Any] = {
            "owner_id": owner_id,
            "scope": scope,
            "scope_id": scope_id,
            "memory_key": memory_key,
            "memory_type": memory_type,
            "display_text": display_text,
            "content": content,
            "status": status,
            "provenance": list(provenance),
            "sensitivity": sensitivity,
            "confidence": confidence,
            "expires_at": expires_at,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "applies_to": applies_to or {},
            "event_sequence": 1,
            "created_at": now,
            "updated_at": now,
        }
        if memory_id is not None:
            values["memory_id"] = memory_id
        item = MemoryItem.model_validate(values)
        event_type = (
            MemoryEventType.CANDIDATE_CREATED
            if status == MemoryStatus.CANDIDATE
            else MemoryEventType.CANDIDATE_REJECTED_BY_POLICY
        )
        event = self._make_event(
            item,
            event_type,
            None,
            actor_type="system",
            payload={"reason_code": decision.reason_code},
        )
        try:
            with self._session_factory.begin() as session:
                session.add(self._row_from_item(item))
                session.flush()
                session.add(self._event_row(event))
                session.flush()
        except IntegrityError as exc:
            if self._exists(item.memory_id):
                raise MemoryAlreadyExistsError(
                    f"Memory '{item.memory_id}' already exists."
                ) from exc
            raise
        return item

    def get(self, memory_id: str, *, owner_id: str) -> MemoryItem | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(MemoryItemRow).where(
                    MemoryItemRow.memory_id == memory_id,
                    MemoryItemRow.owner_id == owner_id,
                )
            )
            return None if row is None else self._item(row)

    def require(self, memory_id: str, *, owner_id: str) -> MemoryItem:
        item = self.get(memory_id, owner_id=owner_id)
        if item is None:
            raise MemoryNotFoundError(f"Memory '{memory_id}' was not found.")
        return item

    def list_candidates(
        self, *, owner_id: str, scope: MemoryScope | None = None, scope_id: str | None = None
    ) -> list[MemoryItem]:
        return self._list(owner_id=owner_id, status=MemoryStatus.CANDIDATE, scope=scope, scope_id=scope_id)

    def list_confirmed(
        self, *, owner_id: str, scope: MemoryScope | None = None, scope_id: str | None = None
    ) -> list[MemoryItem]:
        now = ensure_utc(self._clock.now())
        query = self._list_query(owner_id, MemoryStatus.CONFIRMED, scope, scope_id).where(
            or_(MemoryItemRow.expires_at.is_(None), MemoryItemRow.expires_at > now)
        )
        with self._session_factory() as session:
            return [self._item(row) for row in session.scalars(query).all()]

    def confirm(
        self,
        memory_id: str,
        *,
        owner_id: str,
        expected_version: int,
        confirmed_by_user: bool,
    ) -> MemoryItem:
        if not confirmed_by_user:
            raise MemoryConfirmationRequiredError("Memory confirmation requires an explicit user decision.")
        now = ensure_utc(self._clock.now())
        with self._session_factory.begin() as session:
            row, current = self._locked_current(session, memory_id, owner_id, expected_version)
            if current.status != MemoryStatus.CANDIDATE:
                raise InvalidMemoryTransitionError("Only candidate memory may be confirmed.")
            decision = self._policy.assess_candidate(
                display_text=current.display_text,
                content=current.content,
                provenance=current.provenance,
                sensitivity=current.sensitivity,
            )
            if not decision.accepted_as_candidate:
                raise InvalidMemoryTransitionError(
                    "Memory no longer satisfies confirmation policy."
                )
            canonical = self._canonical(current.scope, current.scope_id)
            existing = next((row_ for row_ in session.scalars(
                select(MemoryItemRow).where(
                    MemoryItemRow.owner_id == owner_id,
                    MemoryItemRow.scope_type == current.scope.value,
                    MemoryItemRow.memory_key == current.memory_key,
                    MemoryItemRow.status == MemoryStatus.CONFIRMED.value,
                    MemoryItemRow.memory_id != memory_id,
                )
            ).all() if self._canonical(current.scope, row_.scope_id) == canonical), None)
            if existing is not None:
                raise MemoryAlreadyExistsError(
                    "A confirmed Memory already exists for this scope and key; "
                    "use the explicit supersede operation."
                )
            provenance = list(current.provenance) + [
                MemoryProvenance(source_type="user_confirmation", actor_type="user", user_confirmed=True)
            ]
            confirmed = self._transition_item(
                current,
                status=MemoryStatus.CONFIRMED,
                now=now,
                updates={"provenance": provenance},
            )
            self._persist_transition(
                session,
                row,
                confirmed,
                expected_version,
                MemoryEventType.CONFIRMED,
                from_status=current.status,
                actor_type="user",
                payload={"policy_reason": decision.reason_code},
            )
            session.flush()
        return confirmed

    def list_items(
        self,
        *,
        owner_id: str,
        scope: MemoryScope | None = None,
        scope_id: str | None = None,
        statuses: set[MemoryStatus] | None = None,
    ) -> list[MemoryItem]:
        query = select(MemoryItemRow).where(MemoryItemRow.owner_id == owner_id)
        if scope is not None:
            query = query.where(MemoryItemRow.scope_type == scope.value)
        if scope_id is not None:
            query = query.where(MemoryItemRow.scope_id == scope_id)
        if statuses is not None:
            query = query.where(
                MemoryItemRow.status.in_(sorted(status.value for status in statuses))
            )
        query = query.order_by(MemoryItemRow.updated_at.desc(), MemoryItemRow.memory_id)
        with self._session_factory() as session:
            return [self._item(row) for row in session.scalars(query).all()]

    def reject(self, memory_id: str, *, owner_id: str, expected_version: int) -> MemoryItem:
        return self._simple_transition(
            memory_id, owner_id=owner_id, expected_version=expected_version,
            allowed={MemoryStatus.CANDIDATE}, status=MemoryStatus.REJECTED,
            event_type=MemoryEventType.REJECTED, actor_type="user",
        )

    def soft_delete(self, memory_id: str, *, owner_id: str, expected_version: int) -> MemoryItem:
        return self._simple_transition(
            memory_id, owner_id=owner_id, expected_version=expected_version,
            allowed={MemoryStatus.CANDIDATE, MemoryStatus.CONFIRMED, MemoryStatus.REJECTED,
                     MemoryStatus.SUPERSEDED, MemoryStatus.EXPIRED},
            status=MemoryStatus.DELETED, event_type=MemoryEventType.DELETED,
            actor_type="user",
        )

    def supersede(
        self,
        memory_id: str,
        *,
        replacement_memory_id: str,
        owner_id: str,
        expected_version: int,
        replacement_expected_version: int,
    ) -> MemoryItem:
        now = ensure_utc(self._clock.now())
        with self._session_factory.begin() as session:
            row, current = self._locked_current(session, memory_id, owner_id, expected_version)
            replacement_row, replacement = self._locked_current(
                session, replacement_memory_id, owner_id, replacement_expected_version
            )
            if current.status != MemoryStatus.CONFIRMED or replacement.status != MemoryStatus.CANDIDATE:
                raise InvalidMemoryTransitionError(
                    "Supersession requires a confirmed value and a candidate replacement."
                )
            if (current.scope, self._canonical(current.scope, current.scope_id), current.memory_key) != (
                replacement.scope, self._canonical(replacement.scope, replacement.scope_id), replacement.memory_key
            ):
                raise InvalidMemoryTransitionError("Supersession values must share owner, scope, and memory key.")
            old_next = self._transition_item(current, status=MemoryStatus.SUPERSEDED, now=now,
                                             updates={"superseded_by_memory_id": replacement.memory_id})
            replacement_next = self._transition_item(
                replacement, status=MemoryStatus.CONFIRMED, now=now,
                updates={
                    "supersedes_memory_id": current.memory_id,
                    "provenance": list(replacement.provenance) + [
                        MemoryProvenance(
                            source_type="user_confirmation",
                            actor_type="user",
                            user_confirmed=True,
                        )
                    ],
                }
            )
            self._persist_transition(session, row, old_next, expected_version,
                                     MemoryEventType.SUPERSEDED,
                                     from_status=current.status, actor_type="user")
            self._persist_transition(session, replacement_row, replacement_next,
                                     replacement_expected_version,
                                     MemoryEventType.SUPERSESSION_LINKED,
                                     from_status=replacement.status, actor_type="user")
            session.flush()
        return old_next

    def add_provenance(self, memory_id: str, *, owner_id: str, expected_version: int,
                       provenance: MemoryProvenance) -> MemoryItem:
        """Stage 2: record another source for the same statement (idempotent per source)."""
        now = ensure_utc(self._clock.now())
        with self._session_factory.begin() as session:
            row, current = self._locked_current(session, memory_id, owner_id, expected_version)
            if current.status not in {MemoryStatus.CANDIDATE, MemoryStatus.CONFIRMED}:
                raise InvalidMemoryTransitionError("Only candidate or confirmed memory can gain a source.")
            known = {(p.source_type, p.source_id) for p in current.provenance}
            if (provenance.source_type, provenance.source_id) in known:
                return current
            merged = self._transition_item(current, status=current.status, now=now,
                                           updates={"provenance": [*current.provenance, provenance]})
            self._persist_transition(session, row, merged, expected_version, MemoryEventType.PROVENANCE_MERGED,
                                     from_status=current.status, actor_type="system",
                                     payload={"source_type": provenance.source_type, "source_id": provenance.source_id})
            session.flush()
        return merged

    def expire_due(self, *, owner_id: str | None = None) -> list[MemoryItem]:
        now = ensure_utc(self._clock.now())
        with self._session_factory.begin() as session:
            query = select(MemoryItemRow).where(
                MemoryItemRow.status.in_([MemoryStatus.CANDIDATE.value, MemoryStatus.CONFIRMED.value]),
                MemoryItemRow.expires_at.is_not(None),
                MemoryItemRow.expires_at <= now,
            )
            if owner_id is not None:
                query = query.where(MemoryItemRow.owner_id == owner_id)
            rows = list(session.scalars(query.order_by(MemoryItemRow.memory_id)).all())
            expired: list[MemoryItem] = []
            for row in rows:
                current = self._item(row)
                next_item = self._transition_item(current, status=MemoryStatus.EXPIRED, now=now)
                self._persist_transition(session, row, next_item, current.version,
                                         MemoryEventType.EXPIRED,
                                         from_status=current.status, actor_type="system")
                expired.append(next_item)
            session.flush()
        return expired

    def events(self, memory_id: str, *, owner_id: str) -> list[MemoryEvent]:
        if self.get(memory_id, owner_id=owner_id) is None:
            return []
        with self._session_factory() as session:
            rows = session.scalars(
                select(MemoryEventRow).where(MemoryEventRow.memory_id == memory_id)
                .order_by(MemoryEventRow.sequence)
            ).all()
            return [MemoryEvent(
                event_id=row.event_id, memory_id=row.memory_id, sequence=row.sequence,
                event_type=row.event_type, from_status=row.from_status,
                to_status=row.to_status, actor_type=row.actor_type,
                payload=json.loads(row.payload_json), occurred_at=ensure_utc(row.occurred_at),
            ) for row in rows]

    def _simple_transition(self, memory_id: str, *, owner_id: str, expected_version: int,
                           allowed: set[MemoryStatus], status: MemoryStatus,
                           event_type: MemoryEventType, actor_type: str) -> MemoryItem:
        now = ensure_utc(self._clock.now())
        with self._session_factory.begin() as session:
            row, current = self._locked_current(session, memory_id, owner_id, expected_version)
            if current.status not in allowed:
                raise InvalidMemoryTransitionError(
                    f"Memory in status '{current.status.value}' cannot transition to '{status.value}'."
                )
            next_item = self._transition_item(current, status=status, now=now)
            self._persist_transition(session, row, next_item, expected_version,
                                     event_type, from_status=current.status,
                                     actor_type=actor_type)
            session.flush()
        return next_item

    def _locked_current(self, session: Session, memory_id: str, owner_id: str,
                        expected_version: int) -> tuple[MemoryItemRow, MemoryItem]:
        row = session.scalar(select(MemoryItemRow).where(
            MemoryItemRow.memory_id == memory_id, MemoryItemRow.owner_id == owner_id
        ))
        if row is None:
            raise MemoryNotFoundError(f"Memory '{memory_id}' was not found.")
        if row.version != expected_version:
            raise StaleMemoryError(f"Memory '{memory_id}' has a stale version.")
        return row, self._item(row)

    @staticmethod
    def _transition_item(current: MemoryItem, *, status: MemoryStatus, now: datetime,
                         updates: dict[str, Any] | None = None) -> MemoryItem:
        return MemoryItem.model_validate({
            **current.model_dump(mode="python"), **(updates or {}), "status": status,
            "version": current.version + 1, "event_sequence": current.event_sequence + 1,
            "updated_at": now,
        })

    def _persist_transition(self, session: Session, row: MemoryItemRow, item: MemoryItem,
                            expected_version: int, event_type: MemoryEventType, *,
                            from_status: MemoryStatus, actor_type: str,
                            payload: dict[str, Any] | None = None) -> None:
        changed = session.execute(update(MemoryItemRow).where(
            MemoryItemRow.memory_id == item.memory_id,
            MemoryItemRow.version == expected_version,
        ).values(**self._projection(item)))
        if changed.rowcount != 1:
            raise StaleMemoryError(f"Memory '{item.memory_id}' has a stale version.")
        event = self._make_event(item, event_type, from_status,
                                 actor_type=actor_type, payload=payload)
        session.add(self._event_row(event))

    def _list(self, *, owner_id: str, status: MemoryStatus, scope: MemoryScope | None,
              scope_id: str | None) -> list[MemoryItem]:
        with self._session_factory() as session:
            return [self._item(row) for row in session.scalars(
                self._list_query(owner_id, status, scope, scope_id)
            ).all()]

    @staticmethod
    def _list_query(owner_id: str, status: MemoryStatus, scope: MemoryScope | None,
                    scope_id: str | None):
        query = select(MemoryItemRow).where(
            MemoryItemRow.owner_id == owner_id, MemoryItemRow.status == status.value
        )
        if scope is not None:
            query = query.where(MemoryItemRow.scope_type == scope.value)
        if scope_id is not None:
            query = query.where(MemoryItemRow.scope_id == scope_id)
        return query.order_by(MemoryItemRow.updated_at.desc(), MemoryItemRow.memory_id)

    def _exists(self, memory_id: str) -> bool:
        with self._session_factory() as session:
            return session.get(MemoryItemRow, memory_id) is not None

    @staticmethod
    def _item(row: MemoryItemRow) -> MemoryItem:
        return MemoryItem.model_validate_json(row.item_json)

    @staticmethod
    def _row_from_item(item: MemoryItem) -> MemoryItemRow:
        return MemoryItemRow(**MemoryRepository._projection(item))

    @staticmethod
    def _projection(item: MemoryItem) -> dict[str, Any]:
        return {
            "memory_id": item.memory_id, "schema_version": item.schema_version,
            "owner_id": item.owner_id, "scope_type": item.scope.value,
            "scope_id": item.scope_id, "memory_key": item.memory_key,
            "memory_type": item.memory_type.value, "display_text": item.display_text,
            "content_json": canonical_json(redact_sensitive(item.content)),
            "status": item.status.value,
            "provenance_json": canonical_json([p.model_dump(mode="json") for p in item.provenance]),
            "sensitivity": item.sensitivity.value, "confidence": item.confidence,
            "supersedes_memory_id": item.supersedes_memory_id,
            "superseded_by_memory_id": item.superseded_by_memory_id,
            "expires_at": item.expires_at, "valid_from": item.valid_from, "valid_to": item.valid_to,
            "version": item.version,
            "event_sequence": item.event_sequence, "item_json": item.model_dump_json(),
            "created_at": item.created_at, "updated_at": item.updated_at,
            "last_used_at": item.last_used_at,
        }

    @staticmethod
    def _make_event(item: MemoryItem, event_type: MemoryEventType,
                    from_status: MemoryStatus | None, *, actor_type: str,
                    payload: dict[str, Any] | None = None) -> MemoryEvent:
        return MemoryEvent(memory_id=item.memory_id, sequence=item.event_sequence,
                           event_type=event_type, from_status=from_status,
                           to_status=item.status, actor_type=actor_type,
                           payload=redact_sensitive(payload or {}), occurred_at=item.updated_at)

    @staticmethod
    def _event_row(event: MemoryEvent) -> MemoryEventRow:
        return MemoryEventRow(
            event_id=event.event_id, memory_id=event.memory_id, sequence=event.sequence,
            event_type=event.event_type.value,
            from_status=None if event.from_status is None else event.from_status.value,
            to_status=event.to_status.value, actor_type=event.actor_type,
            payload_json=canonical_json(redact_sensitive(event.payload)),
            occurred_at=event.occurred_at,
        )
