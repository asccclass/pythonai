"""SDK client confined to one loop, with restart-safe StartWorkflow dispatch."""
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
import threading
import time
from datetime import timedelta

from .contracts import SCHEMA_VERSION


async def connect(settings):
    try:
        from temporalio.client import Client
    except ImportError as error:
        raise RuntimeError('Install agent_execution/temporal_backend/requirements.txt to enable Temporal') from error
    return await asyncio.wait_for(Client.connect(settings.address, namespace=settings.namespace), timeout=10)


async def dispatch(store, row, client):
    from temporalio.exceptions import WorkflowAlreadyStartedError
    from temporalio.common import WorkflowIDReusePolicy
    from agent_execution.tasks import assert_owner_active
    from contextlib import closing
    with closing(store.tasks.connect()) as conn:
        assert_owner_active(conn, row['owner'])
        task = store.tasks.get(row['owner'], row['task_id'])
        if task['state'] == 'cancelled':
            store.cancel(row['id'], row['owner'])
            raise PermissionError('Task cancelled before dispatch')
    contract = store.verify_budget_contract(row)
    execution_seconds = 600
    if contract is not None:
        execution_seconds = min(600, contract['deadline'] - time.time())
        if execution_seconds <= 0:
            from agent_execution.budgets import BudgetExhausted
            store.fail_budget_before_start(row['id'], row['owner'])
            raise BudgetExhausted('Task deadline exhausted before Workflow dispatch')
    try:
        try:
            await client.start_workflow(
                store.workflow_type(row), {'schema_version': SCHEMA_VERSION, 'submission_id': row['id']},
                id=row['workflow_id'], task_queue=row['queue'],
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                execution_timeout=timedelta(seconds=max(.001, execution_seconds)))
        except WorkflowAlreadyStartedError:
            # Only our persisted opaque workflow ID is used, never a caller-supplied ID.
            pass
    except Exception as error:
        store.dispatched(row['id'], type(error).__name__)
        raise
    store.dispatched(row['id'])
    return client.get_workflow_handle(row['workflow_id'])


class LoopBridge:
    """Daemon loop only for explicit Temporal use; shutdown owned by application exit."""
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name='pythonai-temporal-client', daemon=True)
        self.thread.start()

    def call(self, coroutine, timeout, cancelled=None):
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        import time
        deadline = time.monotonic() + timeout
        while True:
            if cancelled and cancelled():
                future.cancel()
                raise InterruptedError('Temporal wait cancelled; persisted task needs reconciliation')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                future.cancel()
                raise TimeoutError('Temporal wait expired; no fallback execution was started')
            try:
                return future.result(timeout=min(.25, remaining))
            except FutureTimeout:
                if future.done():
                    raise

    def close(self):
        async def shutdown():
            pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        if not self.thread.is_alive():
            return
        future = asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
        try:
            future.result(timeout=5)
        except Exception:
            future.cancel()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        if not self.thread.is_alive():
            self.loop.close()


_bridge = None
_lock = threading.Lock()


def bridge():
    global _bridge
    with _lock:
        if _bridge is None:
            import atexit
            _bridge = LoopBridge()
            atexit.register(_bridge.close)
        return _bridge
