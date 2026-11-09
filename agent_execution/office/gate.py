"""Opt-in policy context. This is application authority, never client JSON.

OfficeWait is a control transfer, not a tool error: legacy tool/Skill catch-all
Exception handlers must not swallow it or execute the next nested step.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import threading


class OfficeWait(BaseException):
    def __init__(self, call_id):
        self.call_id = call_id
        super().__init__('Office approval requires durable continuation')


class OfficeUncertain(BaseException):
    pass


@dataclass
class Actor:
    service: object
    ticket: object
    session_id: str
    turn_id: str
    tools: frozenset
    model: str | None = None
    frames: list = field(default_factory=list)
    thread_id: int = field(default_factory=threading.get_ident)


_actor = ContextVar('office_policy_actor',default=None)
_provider_call = ContextVar('office_provider_call',default=None)
_target_authority = ContextVar('office_target_authority',default=None)


@contextmanager
def actor_scope(service,ticket,session_id,turn_id,tools,model=None):
    token = _actor.set(Actor(service,ticket,session_id,turn_id,tools,model))
    try:
        yield
    finally:
        _actor.reset(token)


@contextmanager
def call_scope(call_id):
    token = _provider_call.set(call_id)
    try:
        yield
    finally:
        _provider_call.reset(token)


def active_actor():
    return _actor.get()


def intercept(name,arguments,function,execute):
    actor = _actor.get()
    if actor.thread_id != threading.get_ident():
        raise PermissionError('Office tools cannot execute in detached threads')
    return actor.service.invoke(actor,_provider_call.get(),name,arguments,function,execute)


def forbid_detached(kind):
    if _actor.get() is not None:
        raise PermissionError(f'Office {kind} requires a managed, approved plan; detached execution is disabled')


def parked(conn,owner,assignment,task):
    from .persistence import digest
    row = conn.execute('''SELECT c.*,t.state AS task_state,t.phase,t.checkpoint FROM office_continuations c
        JOIN tasks t ON t.owner=c.owner AND t.id=? WHERE c.owner=? AND c.assignment_id=?''',
        (task,owner,assignment)).fetchone()
    return bool(row and row['state'] in {'waiting','ready'} and row['task_state']=='office_wait'
        and row['phase']=='office_wait' and row['checkpoint_digest']==digest(row['checkpoint'])
        and not conn.execute("SELECT 1 FROM office_calls WHERE owner=? AND assignment_id=? AND state IN ('consuming','started','uncertain')",(owner,assignment)).fetchone())


@contextmanager
def target_authority(path,approval_id,action):
    token = _target_authority.set((str(path.resolve()),approval_id,action))
    try:
        yield
    finally:
        _target_authority.reset(token)


def require_target_authority(store,conn,approval_id,action):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='office_bridge_managed'").fetchone():
        return
    if conn.execute('SELECT 1 FROM office_bridge_managed WHERE approval_id=?',(approval_id,)).fetchone():
        if _target_authority.get() != (str(store.path.resolve()),approval_id,action):
            raise PermissionError('Managed Office approvals require the human/dispatcher service')
