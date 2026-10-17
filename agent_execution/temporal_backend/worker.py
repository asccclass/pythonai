"""Explicit optional Worker process; never spawned during a request or import."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from datetime import timedelta
from pathlib import Path

from .config import Settings
from .store import Store


async def serve():
    from temporalio.worker import Worker
    from temporalio.client import WorkflowExecutionStatus
    from .activities import execute_readonly
    from .workflows import ReadOnlyBridge
    from .client import connect, dispatch
    from agent_execution.turn_control.environment import host_environment
    settings, store = Settings.load(), Store()
    client = await connect(settings)
    async def reconcile():
        while True:
            for row in store.pending():
                if (row['address'], row['namespace'], row['queue']) != (settings.address, settings.namespace, store.queue):
                    continue
                try:
                    await dispatch(store, row, client)
                except Exception as error:
                    print(json.dumps({'temporal': 'dispatch_pending', 'error_type': type(error).__name__}), flush=True)
            for row in store.active():
                if (row['address'], row['namespace'], row['queue']) != (settings.address, settings.namespace, store.queue):
                    continue
                try:
                    task = store.tasks.get(row['owner'], row['task_id'])
                    if task['state'] == 'cancelled':
                        store.cancel(row['id'], row['owner'])
                        await client.get_workflow_handle(row['workflow_id']).cancel()
                        continue
                    status = (await client.get_workflow_handle(row['workflow_id']).describe()).status
                    if status in {WorkflowExecutionStatus.FAILED, WorkflowExecutionStatus.TIMED_OUT,
                                  WorkflowExecutionStatus.CANCELED, WorkflowExecutionStatus.TERMINATED}:
                        store.workflow_failed(row['id'], row['owner'], status.name)
                except Exception as error:
                    print(json.dumps({'temporal': 'reconcile_pending', 'error_type': type(error).__name__}), flush=True)
            await asyncio.sleep(5)
    with ThreadPoolExecutor(max_workers=settings.concurrency) as executor:
        worker = Worker(client, task_queue=store.queue, workflows=[ReadOnlyBridge],
                        activities=[execute_readonly], activity_executor=executor,
                        max_concurrent_activities=settings.concurrency,
                        graceful_shutdown_timeout=timedelta(seconds=15))
        print(json.dumps({'temporal': 'worker_configured', 'namespace': settings.namespace,
                          'queue': store.queue, 'os': host_environment()['os'], 'mode': 'read_only_bridge'}), flush=True)
        reconciler = asyncio.create_task(reconcile())
        try:
            await worker.run()
        finally:
            reconciler.cancel()
            await asyncio.gather(reconciler, return_exceptions=True)


def main():
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / '.env')
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
