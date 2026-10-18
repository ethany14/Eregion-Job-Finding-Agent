"""Temporal regression tests use an injected clock and an in-memory database."""
from datetime import UTC, datetime, timedelta
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from eregion.api.db import Base
from eregion.runtime.memory.models import MemoryItemRow, MemoryEventRow
from eregion.runtime.memory.repository import MemoryRepository
from eregion.runtime.memory.consolidation import write_statement, Relation
from eregion.runtime.memory.types import MemoryScope, MemoryType, MemoryProvenance, MemoryStatus
from eregion.runtime.memory.errors import InvalidMemoryTransitionError
from eregion.runtime.memory.access import MemoryAccessContext, MemoryPurpose
from eregion.runtime.memory.service import MemoryService
from eregion.runtime.sessions.clock import FakeClock

NOW = datetime(2026, 10, 17, tzinfo=UTC)

@pytest.fixture
def stack():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine, tables=[MemoryItemRow.__table__, MemoryEventRow.__table__])
    clock = FakeClock(NOW)
    repo = MemoryRepository(sessionmaker(bind=engine), clock=clock)
    ctx = MemoryAccessContext(owner_id='owner', purpose=MemoryPurpose.CONVERSATION,
                             user_scope_ids=frozenset({'owner'}))
    yield repo, clock, MemoryService(repo, clock=clock), ctx
    engine.dispose()

def write(repo, value, source, **kw):
    return write_statement(repo, owner_id='owner', scope=MemoryScope.USER, scope_id='owner',
        key='job.location_mode', value=value, display_text=value, memory_type=MemoryType.PREFERENCE,
        provenance=MemoryProvenance(source_type='user_message', source_id=source), **kw)

def confirm(repo, item):
    return repo.confirm(item.memory_id, owner_id='owner', expected_version=item.version, confirmed_by_user=True)

def replace(repo, old, new):
    return repo.supersede(old.memory_id, replacement_memory_id=new.memory_id, owner_id='owner',
                         expected_version=old.version, replacement_expected_version=new.version)

def values(service, ctx):
    return [x.item.content['value'] for x in service.effective_preferences(ctx, 'job_search').items]

@pytest.mark.parametrize('bound', ['valid_to', 'expires_at'])
def test_expired_value_can_be_reasserted_and_explicitly_replaced(stack, bound):
    repo, clock, service, ctx = stack
    old = confirm(repo, write(repo, 'remote', 'old', **{bound: NOW-timedelta(days=1)})[0])
    new, decision = write(repo, 'remote', 'new', valid_from=NOW)
    assert decision.relation == Relation.REPLACEMENT
    assert new.memory_id != old.memory_id and new.valid_from == NOW
    assert values(service, ctx) == []
    replace(repo, old, new)
    assert values(service, ctx) == ['remote']
    assert repo.require(old.memory_id, owner_id='owner').status == MemoryStatus.SUPERSEDED

@pytest.mark.parametrize('bounds', [dict(valid_from=NOW+timedelta(days=1)),
    dict(valid_to=NOW+timedelta(days=1)), dict(expires_at=NOW+timedelta(days=1)),
    dict(applies_to={'artifact': 'resume'})])
def test_different_constraints_never_merge(stack, bounds):
    repo, *_ = stack
    old, _ = write(repo, 'remote', 'old')
    new, decision = write(repo, 'remote', 'new', **bounds)
    assert new.memory_id != old.memory_id and decision.relation == Relation.NEW

def test_equal_intervals_merge_sources_without_extending_validity(stack):
    repo, *_ = stack
    bounds = dict(valid_from=NOW, valid_to=NOW+timedelta(days=1))
    old, _ = write(repo, 'remote', 'old', **bounds)
    new, decision = write(repo, 'remote', 'new', **bounds)
    assert new.memory_id == old.memory_id and decision.relation == Relation.DUPLICATE
    assert new.valid_to == bounds['valid_to'] and len(new.provenance) == 2

def test_future_replacement_rejected_atomically_then_allowed_at_boundary(stack):
    repo, clock, service, ctx = stack
    old = confirm(repo, write(repo, 'remote', 'old')[0])
    new, _ = write(repo, 'onsite', 'new', valid_from=NOW+timedelta(days=1))
    with pytest.raises(InvalidMemoryTransitionError, match='not currently valid'):
        replace(repo, old, new)
    assert repo.require(old.memory_id, owner_id='owner') == old
    assert repo.require(new.memory_id, owner_id='owner') == new
    assert values(service, ctx) == ['remote']
    clock.advance(seconds=86400)
    replace(repo, old, new)
    assert values(service, ctx) == ['onsite']

@pytest.mark.parametrize('bound', ['valid_to', 'expires_at'])
def test_invalid_replacement_does_not_remove_current_value(stack, bound):
    repo, _, service, ctx = stack
    old = confirm(repo, write(repo, 'remote', 'old')[0])
    new, _ = write(repo, 'onsite', 'new', **{bound: NOW})
    with pytest.raises(InvalidMemoryTransitionError, match='not currently valid'):
        replace(repo, old, new)
    assert values(service, ctx) == ['remote']
