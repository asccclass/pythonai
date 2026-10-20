"""Central admission boundary; no Temporal SDK import on the legacy path."""
from contextlib import contextmanager
from contextvars import ContextVar

_activity = ContextVar('temporal_activity_task', default=None)


@contextmanager
def activity_scope(owner, task):
    token = _activity.set((owner, task))
    try:
        yield
    finally:
        _activity.reset(token)


def assert_claim_backend(conn, owner, task):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='execution_bindings'").fetchone():
        row = conn.execute('SELECT backend FROM execution_bindings WHERE owner=? AND task_id=?', (owner, task)).fetchone()
        if row and row[0] == 'temporal' and _activity.get() != (owner, task):
            raise PermissionError('Temporal task cannot be claimed by a legacy executor')


def route(messages, *, resume=False, skill=False):
    from agent_execution.policy import current_context
    from agent_execution.tasks import TaskStore, task_owner
    from .config import selected_backend
    from .store import Store
    context = current_context()
    if context.task_id is None:
        if selected_backend() == 'temporal':
            raise ValueError('Temporal requires a durable task ID; use a managed entrypoint')
        return None
    owner, task = task_owner(context), context.task_id
    if _activity.get() == (owner, task):
        return None
    requested = selected_backend()
    if requested == 'sqlite':
        from contextlib import closing
        # Disabled installations must not migrate optional schemas or create bindings.
        # A pre-existing legacy task is pinned by its TaskStore row when enabling later.
        with closing(TaskStore().connect()) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='execution_bindings'").fetchone():
                return None
            existing = conn.execute('SELECT backend FROM execution_bindings WHERE owner=? AND task_id=?', (owner, task)).fetchone()
            if existing is None or existing[0] == 'sqlite':
                return None
    store = Store()
    backend = store.bind(owner, task, requested)
    if backend == 'sqlite':
        return None
    if resume:
        previous = store.for_task(owner, task)
        if previous and previous['state'] in {'completed', 'failed'} and previous['result']:
            from .backend import restore_result
            return restore_result(previous, messages)
        from .contracts import HUMAN_WORKFLOW_NAME, APPROVAL_WORKFLOW_NAME
        if previous and store.workflow_type(previous) in {HUMAN_WORKFLOW_NAME, APPROVAL_WORKFLOW_NAME} and not skill:
            from .backend import invoke
            return invoke(store, messages, resume=True)
    if skill or resume:
        raise ValueError('M0 Temporal supports new read-only Agent tasks; skill/manual resume is not enabled')
    from .backend import invoke
    return invoke(store, messages)
