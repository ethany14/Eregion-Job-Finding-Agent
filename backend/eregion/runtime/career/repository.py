"""Durable conversations and linked career work, separate from browser attempts."""
import json
from datetime import UTC, datetime, timedelta
from threading import RLock
from uuid import uuid4

from sqlalchemy import Column, Integer, String, Text, select, update
from eregion.api.db import Base

TRASH_RETENTION = timedelta(days=30)
META_FIELDS = ('title','group_id','pinned_at','archived_at','trashed_at')


class CareerTaskRow(Base):
    __tablename__ = 'career_tasks'
    task_id = Column(String(40), primary_key=True)
    request_key = Column(String(100), unique=True, nullable=False)
    version = Column(Integer, nullable=False)
    state_json = Column(Text, nullable=False)
    # Organisation lives outside the state blob so renaming or archiving never
    # bumps the task version that in-flight actions are fenced on.
    title = Column(String(120))
    group_id = Column(String(40))
    pinned_at = Column(String(40))
    archived_at = Column(String(40))
    trashed_at = Column(String(40), index=True)


class CareerTaskGroupRow(Base):
    __tablename__ = 'career_task_groups'
    group_id = Column(String(40), primary_key=True)
    name = Column(String(80), nullable=False)
    position = Column(Integer, nullable=False)
    created_at = Column(String(40), nullable=False)


def now():
    return datetime.now(UTC).isoformat()


class TaskRepository:
    def __init__(self, sessions, *, task_memory=None):
        self.sessions, self.lock = sessions, RLock()
        self.task_memory = task_memory

    def create(self, text, key, *, visual_fallback=False, context=None):
        with self.lock, self.sessions.begin() as db:
            existing = db.scalar(select(CareerTaskRow).where(CareerTaskRow.request_key == key))
            if existing:
                state = self.decode(existing)
                if state['goal'] != text or state.get('initial_context',{}) != (context or {}):
                    raise ValueError('This request key belongs to another goal or saved job.')
                return state, False
            state = {'task_id':str(uuid4()), 'goal':text, 'title':text[:90], 'status':'queued',
                'messages':[{'id':str(uuid4()),'role':'user','text':text,'at':now()}],
                'chat_revision':0, 'plan':[], 'history':[], 'actions':[], 'jobs':[], 'packs':[],
                'application_runs':[], 'interviews':[], 'active_application_run':None,
                'input_request':None, 'wait':None, 'model_calls':0, 'budget_base':0,
                'visual_fallback':visual_fallback, 'pause_requested':False,
                'created_at':now(), 'updated_at':now()}
            from .search_policy import update_constraints
            state['search_constraints']=update_constraints({},text,state['messages'][0]['id'])
            state['user_turn_id']=state['messages'][0]['id']
            if context:
                state['initial_context'] = context
                state['messages'][0]['context'] = context
                if context.get('application_id'):state['jobs'] = [context['application_id']]
            if self.task_memory is not None and self.task_memory.config.mode != 'off':
                state['task_memory_owner_id'] = self.task_memory.owner_id
            row = CareerTaskRow(task_id=state['task_id'],request_key=key,version=1,state_json=json.dumps(state))
            db.add(row)
            if self.task_memory is not None:
                db.flush()
                self.task_memory.capture(db, state, 1)
            return {**state,'version':1}, True

    @staticmethod
    def decode(row):
        return {**json.loads(row.state_json),'version':row.version}

    def get(self, task_id):
        with self.sessions() as db:
            row = db.get(CareerTaskRow,task_id)
            if row is None:raise LookupError('Conversation not found.')
            return self.decode(row)

    def list(self):
        with self.sessions() as db:
            return sorted((self.decode(row) for row in db.scalars(select(CareerTaskRow))),
                key=lambda s:s['updated_at'],reverse=True)

    def change(self, task_id, mutation=None, **changes):
        # Service, harness and restored workers can have different repository
        # instances. An instance lock alone cannot protect their shared JSON
        # blob. Retry a pure mutation against the latest state after a CAS miss.
        with self.lock:
            for attempt in range(32):
                with self.sessions.begin() as db:
                    row = db.get(CareerTaskRow,task_id)
                    if row is None:raise LookupError('Conversation not found.')
                    version=row.version
                    state=self.decode(row)
                    state.setdefault('user_turn_id',next((m['id'] for m in reversed(state.get('messages',[])) if m.get('role')=='user'),None))
                    delta={**(mutation(state) if mutation else {}),**changes}
                    if 'messages' in delta:
                        from .search_policy import constraints, update_constraints
                        policy=constraints(state)
                        existing={m['id'] for m in state.get('messages',[])}
                        for message in delta['messages']:
                            if message.get('role')=='user' and message['id'] not in existing:
                                policy=update_constraints(policy,message.get('text',''),message['id'])
                                delta['user_turn_id']=message['id']
                        delta['search_constraints']=policy
                    # Archive the old window before applying an already-trimmed delta.
                    if self.task_memory is not None:
                        self.task_memory.capture(db, state, version)
                    state.update(delta,updated_at=now())
                    state.pop('version',None)
                    changed=db.execute(update(CareerTaskRow).where(
                        CareerTaskRow.task_id==task_id,CareerTaskRow.version==version
                    ).values(version=version+1,state_json=json.dumps(state)))
                    if changed.rowcount==1:
                        if self.task_memory is not None:
                            self.task_memory.capture(db, state, version+1)
                        return {**state,'version':version+1}
                    db.rollback()  # do not commit source records from a failed CAS attempt
            raise RuntimeError('Conversation update could not acquire its current version.')

    def append(self, task_id, **message):
        value = {'id':str(uuid4()),'at':now(),**message}
        return self.change(task_id,lambda s:{'messages':[*s['messages'],value][-150:]})

    # ---- Organisation: titles, pins, groups, archive and trash ----

    @staticmethod
    def meta_of(row):
        return {field:getattr(row,field) for field in META_FIELDS}

    def metadata(self, task_id):
        with self.sessions() as db:
            row = db.get(CareerTaskRow,task_id)
            if row is None:raise LookupError('Conversation not found.')
            return self.meta_of(row)

    def list_view(self, view='active'):
        """Task states for one sidebar view, newest first."""
        query = select(CareerTaskRow)
        if view=='active':query = query.where(CareerTaskRow.archived_at.is_(None),CareerTaskRow.trashed_at.is_(None))
        elif view=='archived':query = query.where(CareerTaskRow.archived_at.is_not(None),CareerTaskRow.trashed_at.is_(None))
        elif view=='trash':query = query.where(CareerTaskRow.trashed_at.is_not(None))
        else:raise ValueError('Unknown conversation view.')
        with self.sessions() as db:
            return sorted((self.decode(row) for row in db.scalars(query)),key=lambda s:s['updated_at'],reverse=True)

    def update_meta(self, task_id, **changes):
        unknown = set(changes)-set(META_FIELDS)
        if unknown:raise ValueError(f'Unsupported conversation fields: {", ".join(sorted(unknown))}.')
        with self.lock, self.sessions.begin() as db:
            row = db.get(CareerTaskRow,task_id)
            if row is None:raise LookupError('Conversation not found.')
            if changes.get('group_id') and db.get(CareerTaskGroupRow,changes['group_id']) is None:
                raise LookupError('Group not found.')
            for field,value in changes.items():setattr(row,field,value)
            return self.meta_of(row)

    def delete(self, task_id):
        """Permanently remove a conversation. Only conversations already in the trash can be deleted."""
        with self.lock, self.sessions.begin() as db:
            row = db.get(CareerTaskRow,task_id)
            if row is None:raise LookupError('Conversation not found.')
            if row.trashed_at is None:raise ValueError('Move this conversation to Trash before deleting it permanently.')
            db.delete(row)

    def purge_trash(self, *, older_than=None):
        """Delete trashed conversations; with ``older_than``, only those trashed before that moment."""
        with self.lock, self.sessions.begin() as db:
            query = select(CareerTaskRow).where(CareerTaskRow.trashed_at.is_not(None))
            if older_than is not None:query = query.where(CareerTaskRow.trashed_at<older_than.isoformat())
            rows = list(db.scalars(query))
            for row in rows:db.delete(row)
            return [row.task_id for row in rows]

    def purge_expired_trash(self, moment=None):
        return self.purge_trash(older_than=(moment or datetime.now(UTC))-TRASH_RETENTION)

    def groups(self):
        with self.sessions() as db:
            rows = db.scalars(select(CareerTaskGroupRow).order_by(CareerTaskGroupRow.position,CareerTaskGroupRow.created_at))
            return [{'group_id':g.group_id,'name':g.name,'position':g.position} for g in rows]

    def create_group(self, name):
        with self.lock, self.sessions.begin() as db:
            position = len(list(db.scalars(select(CareerTaskGroupRow.group_id))))
            row = CareerTaskGroupRow(group_id=str(uuid4()),name=name,position=position,created_at=now())
            db.add(row)
            return {'group_id':row.group_id,'name':row.name,'position':row.position}

    def rename_group(self, group_id, name):
        with self.lock, self.sessions.begin() as db:
            row = db.get(CareerTaskGroupRow,group_id)
            if row is None:raise LookupError('Group not found.')
            row.name = name
            return {'group_id':row.group_id,'name':row.name,'position':row.position}

    def delete_group(self, group_id):
        """Remove a group; its conversations go back to the ungrouped list."""
        with self.lock, self.sessions.begin() as db:
            row = db.get(CareerTaskGroupRow,group_id)
            if row is None:raise LookupError('Group not found.')
            db.execute(update(CareerTaskRow).where(CareerTaskRow.group_id==group_id).values(group_id=None))
            db.delete(row)

    def link(self, task_id, collection, identifier, **changes):
        return self.change(task_id,lambda s:{collection:list(dict.fromkeys([*s.get(collection,[]),identifier]))},**changes)
