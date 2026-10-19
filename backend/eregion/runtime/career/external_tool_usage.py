"""Durable spend records for external paid tools in this deployment (P4c). No tool writes them since
Treg was removed; the table and reader stay for existing records.

Separate from the model usage ledger, joined by task/run/revision for reporting. Amounts
are integer micro-USD. A logical operation is reserved atomically against the task cap,
the deployment's UTC-day cap and the attempt cap BEFORE any network I/O. Unknown outcomes
keep their reservation; nothing is assumed free. This is spend control for the user's own
deployment, not a billing system.
"""
from datetime import UTC, datetime, timedelta
import hashlib
import json
from uuid import uuid4

from sqlalchemy import Column, Integer, String, Text, select

from eregion.api.db import Base

OPEN = {'reserved', 'in_flight', 'unknown'}
TERMINAL = {'succeeded', 'failed', 'cancelled'}


class BudgetExceeded(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class ExternalToolOperation(Base):
    __tablename__ = 'external_tool_operations'
    operation_id = Column(String(40), primary_key=True)
    scope = Column(String(80), nullable=False, index=True)       # task id or live batch id
    task_id = Column(String(40), index=True)
    run_id = Column(String(40))
    revision = Column(String(40))
    endpoint = Column(String(80), nullable=False)
    request_hash = Column(String(64), nullable=False)
    idempotency_key = Column(String(80), nullable=False)
    status = Column(String(20), nullable=False, index=True)
    reserved_micro = Column(Integer, nullable=False)
    charged_micro = Column(Integer)                              # None = not confirmed
    original_cost_micro = Column(Integer)                        # replay: what the original cost
    attempts = Column(Integer, nullable=False, default=0)
    billing_day = Column(String(10), nullable=False, index=True)
    call_id = Column(String(80))
    claim_owner = Column(String(40))
    claim_expires = Column(String(40))
    created_at = Column(String(40), nullable=False)
    updated_at = Column(String(40), nullable=False)
    details_json = Column(Text, nullable=False, default='{}')


def request_hash(endpoint, body):
    return hashlib.sha256(json.dumps([endpoint, body], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _now():
    return datetime.now(UTC)


def _committed(row):
    """Spend that counts against budgets: confirmed charge, else the held reservation."""
    if row.status in OPEN:
        return row.reserved_micro if row.charged_micro is None else max(row.charged_micro, row.reserved_micro)
    return row.charged_micro or 0


class ExternalToolLedger:
    def __init__(self, sessions, *, deployment='local'):
        self.sessions = sessions
        self.deployment = deployment

    def _begin(self, db):
        if db.bind.dialect.name == 'sqlite':
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')

    def reserve(self, *, scope, endpoint, body, reserve_micro, task_max_micro, daily_max_micro, max_attempts,
                task_id=None, run_id=None, revision=None):
        """Atomically reserve one logical operation; raises BudgetExceeded before any I/O."""
        now = _now()
        day = now.date().isoformat()
        operation_id = str(uuid4())
        with self.sessions.begin() as db:
            self._begin(db)
            scoped = list(db.scalars(select(ExternalToolOperation).where(ExternalToolOperation.scope == scope)))
            if sum(_committed(r) for r in scoped) + reserve_micro > task_max_micro:
                raise BudgetExceeded('task_cost_cap')
            if sum(r.attempts for r in scoped) + 1 > max_attempts:
                raise BudgetExceeded('attempt_cap')
            daily = list(db.scalars(select(ExternalToolOperation).where(ExternalToolOperation.billing_day == day)))
            if sum(_committed(r) for r in daily) + reserve_micro > daily_max_micro:
                raise BudgetExceeded('daily_cost_cap')
            db.add(ExternalToolOperation(operation_id=operation_id, scope=scope, task_id=task_id, run_id=run_id,
                revision=revision, endpoint=endpoint, request_hash=request_hash(endpoint, body),
                idempotency_key='jha-' + uuid4().hex, status='reserved', reserved_micro=reserve_micro,
                attempts=0, billing_day=day, created_at=now.isoformat(), updated_at=now.isoformat(),
                details_json='{}'))
        return operation_id

    def claim_attempt(self, operation_id, *, worker, max_attempts, lease_seconds=120):
        """Claim the operation for one network attempt (counts toward the attempt cap)."""
        now = _now()
        with self.sessions.begin() as db:
            self._begin(db)
            row = db.get(ExternalToolOperation, operation_id)
            if row is None or row.status in TERMINAL:
                raise BudgetExceeded('operation_closed')
            if row.claim_owner and row.claim_owner != worker and row.claim_expires and row.claim_expires > now.isoformat():
                raise BudgetExceeded('claimed_by_other_worker')
            scoped = list(db.scalars(select(ExternalToolOperation).where(ExternalToolOperation.scope == row.scope)))
            if sum(r.attempts for r in scoped) + 1 > max_attempts:
                raise BudgetExceeded('attempt_cap')
            row.attempts += 1
            row.status = 'in_flight'
            row.claim_owner = worker
            row.claim_expires = (now + timedelta(seconds=lease_seconds)).isoformat()
            row.updated_at = now.isoformat()
            return row.idempotency_key, row.attempts

    def settle(self, operation_id, *, status, charged_micro=None, original_cost_micro=None, call_id=None, details=None):
        """Record an outcome. charged_micro=None keeps the reservation (unknown charge)."""
        with self.sessions.begin() as db:
            self._begin(db)
            row = db.get(ExternalToolOperation, operation_id)
            if charged_micro is not None:
                row.charged_micro = charged_micro
            if original_cost_micro is not None:
                row.original_cost_micro = original_cost_micro
            if call_id:
                row.call_id = call_id
            # Without a confirmed charge the outcome stays open (unknown) and keeps its
            # reservation; a status is final only with a known charge (0 included).
            row.status = status if row.charged_micro is not None else 'unknown'
            row.claim_owner = row.claim_expires = None
            merged = {**json.loads(row.details_json or '{}'), **(details or {})}
            row.details_json = json.dumps(merged)
            row.updated_at = _now().isoformat()
            return row.status

    def open_operations(self, scope=None):
        with self.sessions() as db:
            query = select(ExternalToolOperation).where(ExternalToolOperation.status.in_(OPEN))
            if scope:
                query = query.where(ExternalToolOperation.scope == scope)
            return [self._view(r) for r in db.scalars(query)]

    def summary(self, scope):
        with self.sessions() as db:
            rows = list(db.scalars(select(ExternalToolOperation).where(ExternalToolOperation.scope == scope)))
        confirmed = sum(r.charged_micro or 0 for r in rows if r.charged_micro is not None)
        held = sum(r.reserved_micro for r in rows if r.status in OPEN and r.charged_micro is None)
        return {'operations': len(rows), 'attempts': sum(r.attempts for r in rows),
                'confirmed_micro': confirmed, 'unknown_held_micro': held,
                'complete': held == 0, 'by_status': {s: sum(r.status == s for r in rows) for s in {r.status for r in rows}}}

    def rows(self, scope):
        with self.sessions() as db:
            return [self._view(r) for r in db.scalars(select(ExternalToolOperation)
                    .where(ExternalToolOperation.scope == scope).order_by(ExternalToolOperation.created_at))]

    @staticmethod
    def _view(row):
        return {k: getattr(row, k) for k in ('operation_id', 'scope', 'task_id', 'endpoint', 'status', 'reserved_micro',
                'charged_micro', 'original_cost_micro', 'attempts', 'billing_day', 'call_id', 'idempotency_key')} | {
                'details': json.loads(row.details_json or '{}')}
