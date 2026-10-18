"""Rebuild current authority for each Activity; no SDK imports."""
from contextlib import contextmanager, ExitStack
import json
from pathlib import Path
import threading

_slots = None
_slot_lock = threading.Lock()


@contextmanager
def host_slot(cancelled):
    """Both protocol queues share the same host execution ceiling."""
    global _slots
    with _slot_lock:
        if _slots is None:
            from .config import Settings
            _slots = threading.BoundedSemaphore(Settings.load().concurrency)
    while not _slots.acquire(timeout=.1):
        if cancelled():
            raise InterruptedError('Activity cancelled while waiting for host capacity')
    try:
        if cancelled():
            raise InterruptedError('Activity cancelled before execution')
        yield
    finally:
        _slots.release()


@contextmanager
def execution_context(store, row, cancelled=None):
    from agent_execution import execution_scope
    from agent_execution.policy import current_context
    from agent_execution.tasks import task_owner
    from agent_execution.sessions import project_workspace
    from agent_execution.budgets import budget_scope
    from agent_execution.workspace_lifecycle import LifecycleStore
    from permissions import get_user_role, set_current_role
    from .router import activity_scope
    from .contracts import READ_TOOLS
    from .ledger import Ledger
    from .operations import operation_scope
    scope = json.loads(row['context'])
    store.verify_budget_contract(row)
    role = row['role']
    if scope['identity'] == 'local':
        if scope['source'] != 'local':
            raise PermissionError('Remote input cannot claim local authority')
    else:
        levels = {'viewer': 0, 'operator': 1, 'admin': 2}
        role = min((role, get_user_role(scope['identity'])), key=lambda value: levels[value])
    with execution_scope(workspace=None, repository_workspace=None):
        configured = project_workspace(scope['project']).resolve()
    if Path(scope['workspace']).resolve() != configured:
        raise PermissionError('Workspace configuration changed; relocation refused')
    scope['workspace'] = configured
    scope['allowed_tools'] = frozenset(scope['allowed_tools']) & READ_TOOLS
    if scope.get('communication_db'):
        scope['communication_db'] = Path(scope['communication_db'])
    scope['task_id'] = row['task_id']
    scope['check_cancelled'] = lambda: bool(cancelled and cancelled()) or store.get(row['id'])['state'] == 'cancelled'
    with ExitStack() as stack:
        stack.enter_context(set_current_role(role))
        stack.enter_context(execution_scope(**scope))
        if task_owner(current_context()) != row['owner']:
            raise PermissionError('Invalid stored task scope')
        stack.enter_context(activity_scope(row['owner'], row['task_id']))
        LifecycleStore().ensure_session()
        stack.enter_context(budget_scope())
        ledger = Ledger(store, row['id'], row['fence'])
        stack.enter_context(operation_scope(ledger))
        yield ledger
