"""In-process authority backed by a live, exact durable dispatcher lease.

Never deserialize this authority from client input. Possessing a task ID, owner
DTO, or runtime request alone does not grant TaskStore execution.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
import time

from agent_execution.policy import current_context
from agent_execution.tasks import RecoveryRequired


@dataclass(frozen=True)
class Admission:
    database: Path
    owner: str
    assignment_id: str
    task_id: str
    staff_id: str
    generation: int
    token: str


_authority = ContextVar('office_admission', default=None)


@contextmanager
def admitted(ticket):
    token = _authority.set(ticket)
    try:
        yield
    finally:
        _authority.reset(token)


def assert_admitted(conn, owner, task_id):
    ticket = _authority.get()
    context = current_context()
    database = Path(conn.execute('PRAGMA database_list').fetchone()[2]).resolve()
    if (ticket is None or ticket.owner != owner or ticket.task_id != task_id
            or database != ticket.database or context.task_database != ticket.database):
        raise RecoveryRequired('Office task requires a trusted dispatcher admission')
    row = conn.execute('''SELECT e.*,a.state AS assignment_state FROM office_executions e
        JOIN office_assignments a ON a.owner=e.owner AND a.id=e.assignment_id
        WHERE e.owner=? AND e.assignment_id=?''', (owner,ticket.assignment_id)).fetchone()
    if (row is None or row['state'] != 'running' or row['assignment_state'] != 'active'
            or row['task_id'] != task_id or row['staff_id'] != ticket.staff_id
            or row['generation'] != ticket.generation or row['token'] != ticket.token
            or min(row['lease_until'],row['deadline']) <= time.time()):
        raise RecoveryRequired('Office dispatcher lease expired or was revoked')
    return ticket
