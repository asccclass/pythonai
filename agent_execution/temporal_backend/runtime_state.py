"""Frontend binding, replay guards and profile extraction ownership."""
from contextlib import closing
import json

from agent_execution.policy import current_context
from agent_execution.tasks import TaskStore, task_owner
from .store import Store, digest
from .publication import binding, end_payload


def existing():
    context = current_context()
    if not context.task_id:
        return None
    tasks = TaskStore()
    with closing(tasks.connect()) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_submissions'").fetchone():
            return None
        row = conn.execute('SELECT * FROM temporal_submissions WHERE owner=? AND task_id=?',(task_owner(context),context.task_id)).fetchone()
    return dict(row) if row else None


def managed():
    row = existing()
    return row if row and binding(row) is not None else None


def prepare_profile(text, extractor):
    from agent_execution.user_profile import prepare_turn
    from .router import backend_for_task
    context = current_context()
    # Commands remain explicit; a legacy binding never gains a Temporal guard.
    if not context.task_id or text.startswith('/profile') or (existing() is None and backend_for_task(context) != 'temporal'):
        return prepare_turn(text,extractor)
    tasks = TaskStore()
    with closing(tasks.connect()) as conn:
        if existing() is None and conn.execute('SELECT 1 FROM tasks WHERE owner=? AND id=?',(task_owner(context),context.task_id)).fetchone():
            return prepare_turn(text,extractor)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='execution_bindings'").fetchone():
            old = conn.execute('SELECT backend FROM execution_bindings WHERE owner=? AND task_id=?',(task_owner(context),context.task_id)).fetchone()
            if old and old[0] != 'temporal':
                return prepare_turn(text,extractor)
    if context.check_cancelled and context.check_cancelled():
        from agent_execution.budgets import BudgetExhausted
        raise BudgetExhausted('Profile input stopped by cancellation/deadline')
    key = digest(text)
    owner = task_owner(context)
    with closing(tasks.connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        from agent_execution.tasks import assert_owner_active
        assert_owner_active(conn,owner)
        conn.execute('CREATE TABLE IF NOT EXISTS temporal_profile_inputs(owner TEXT NOT NULL,task_id TEXT NOT NULL,input_key TEXT NOT NULL,state TEXT NOT NULL,result TEXT,PRIMARY KEY(owner,task_id,input_key))')
        old = conn.execute('SELECT state,result FROM temporal_profile_inputs WHERE owner=? AND task_id=? AND input_key=?',(owner,context.task_id,key)).fetchone()
        if old:
            return tuple(json.loads(old['result'])) if old['state']=='completed' else (None,'Profile 萃取結果尚未確認；未自動重試，可用 /profile 核對。')
        from agent_execution.delivery import positive_limit, CapacityError
        if conn.execute('SELECT count(*) FROM temporal_profile_inputs').fetchone()[0] >= positive_limit('AGENT_TEMPORAL_OPERATION_LIMIT',100000):
            raise CapacityError('Profile input ledger full')
        conn.execute("INSERT INTO temporal_profile_inputs VALUES(?,?,?,'running',NULL)",(owner,context.task_id,key))
    # Reserve before provider I/O. An interrupted/unknown call is never reminted.
    result = prepare_turn(text,extractor)
    with closing(tasks.connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        from .retention import assert_not_erased
        from agent_execution.tasks import assert_owner_active
        assert_owner_active(conn,owner)
        assert_not_erased(conn,owner,context.task_id)
        conn.execute("UPDATE temporal_profile_inputs SET state='completed',result=? WHERE owner=? AND task_id=? AND input_key=?",(json.dumps(result,ensure_ascii=False),owner,context.task_id,key))
    return result


def replay_turn(text, runtime):
    row = managed()
    if row is None:
        return None
    store = Store()
    task = store.tasks.get(row['owner'],row['task_id'])
    original = [item for item in json.loads(row['input']) if item.get('role')=='user']
    if not original or original[-1].get('content') != text:
        raise ValueError('Task ID cannot be reused with different input')
    from agent_runtime import resume_agent_turn
    return resume_agent_turn(task['checkpoint']['messages'],runtime,task['episode'])


def frontend_end(default):
    row = managed()
    if row is None:
        return default
    from agent_execution.tasks import assert_owner_active
    with closing(TaskStore().connect()) as conn:
        assert_owner_active(conn,row['owner'])
    task = TaskStore().get(row['owner'],row['task_id'])
    return (end_payload(Store(),row) or default) if task['state'] in {'completed','failed','suspended','cancelled','uncertain'} else default


def wake_reviews(runtime):
    if runtime.memory_worker is not None and hasattr(runtime.memory_worker,'enqueue_pending_reviews'):
        runtime.memory_worker.enqueue_pending_reviews(runtime.memory,runtime.memory_classifier_factory,
            runtime.semantic_extractor,runtime.procedure_matcher)


def append_reply(runtime, reply):
    if managed() and runtime.messages and runtime.messages[-1].get('role')=='assistant' and runtime.messages[-1].get('content')==reply:
        return
    runtime.messages.append({'role':'assistant','content':reply})
