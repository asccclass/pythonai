"""Explicit optional Worker process; never spawned during a request or import."""
import asyncio
from contextlib import AsyncExitStack
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
    from .workflows import ReadOnlyBridge, AgentTaskWorkflowV1, AgentTaskWorkflowV2, AgentTaskWorkflowV3
    from .step_activities import ACTIVITIES
    from .client import connect, dispatch
    from agent_execution.turn_control.environment import host_environment
    settings, store = Settings.load(), Store()
    from .decisions import Decisions
    decisions = Decisions(store)
    from .approval_gate import ApprovalGate
    from .approval_outbox import pending as approval_events
    from agent_execution.approvals import ApprovalStore
    gate, approvals = ApprovalGate(store), ApprovalStore()
    client = await connect(settings)
    queues = {store.queue, store.queue_for('steps'), store.queue_for('human'), store.queue_for('approval')}
    async def reconcile():
        while True:
            for event in approval_events(approvals, queues=queues):
                try:
                    target = json.loads(event['binding'])
                    row = store.get(target['submission_id'], target['owner'])
                    if (row['address'], row['namespace']) != (settings.address, settings.namespace) or row['queue'] not in queues:
                        continue
                    await gate.dispatch(client, event)
                except Exception as error:
                    print(json.dumps({'temporal': 'approval_decision_pending', 'error_type': type(error).__name__}), flush=True)
            for row in decisions.pending():
                if (row['address'], row['namespace']) != (settings.address, settings.namespace) or row['queue'] not in queues:
                    continue
                try:
                    await decisions.dispatch(client, row)
                except Exception as error:
                    print(json.dumps({'temporal': 'decision_pending', 'error_type': type(error).__name__}), flush=True)
            for row in store.pending():
                if (row['address'], row['namespace']) != (settings.address, settings.namespace) or row['queue'] not in queues:
                    continue
                try:
                    await dispatch(store, row, client)
                except Exception as error:
                    print(json.dumps({'temporal': 'dispatch_pending', 'error_type': type(error).__name__}), flush=True)
            for row in store.active():
                if (row['address'], row['namespace']) != (settings.address, settings.namespace) or row['queue'] not in queues:
                    continue
                try:
                    task = store.tasks.get(row['owner'], row['task_id'])
                    if task['state'] == 'cancelled':
                        store.cancel(row['id'], row['owner'])
                        await client.get_workflow_handle(row['workflow_id']).cancel()
                        continue
                    status = (await client.get_workflow_handle(row['workflow_id']).describe()).status
                    if status == WorkflowExecutionStatus.CANCELED:
                        store.workflow_closed(row['id'], row['owner'], status.name)
                    elif status in {WorkflowExecutionStatus.FAILED, WorkflowExecutionStatus.TIMED_OUT,
                                    WorkflowExecutionStatus.TERMINATED}:
                        store.workflow_closed(row['id'], row['owner'], status.name)
                except Exception as error:
                    print(json.dumps({'temporal': 'reconcile_pending', 'error_type': type(error).__name__}), flush=True)
            await asyncio.sleep(5)
    with ThreadPoolExecutor(max_workers=settings.concurrency*2) as executor:
        bridge_worker = Worker(client, task_queue=store.queue, workflows=[ReadOnlyBridge],
                        activities=[execute_readonly], activity_executor=executor,
                        max_concurrent_activities=settings.concurrency,
                        graceful_shutdown_timeout=timedelta(seconds=15))
        steps_worker = Worker(client, task_queue=store.queue_for('steps'), workflows=[AgentTaskWorkflowV1],
                              activities=ACTIVITIES, activity_executor=executor,
                              max_concurrent_activities=settings.concurrency,
                              graceful_shutdown_timeout=timedelta(seconds=15))
        human_worker = Worker(client, task_queue=store.queue_for('human'), workflows=[AgentTaskWorkflowV2],
                              activities=ACTIVITIES, activity_executor=executor,
                              max_concurrent_activities=settings.concurrency,
                              graceful_shutdown_timeout=timedelta(seconds=15))
        approval_worker = Worker(client, task_queue=store.queue_for('approval'), workflows=[AgentTaskWorkflowV3],
                                 activities=ACTIVITIES, activity_executor=executor,
                                 max_concurrent_activities=settings.concurrency,
                                 graceful_shutdown_timeout=timedelta(seconds=15))
        print(json.dumps({'temporal': 'worker_configured', 'namespace': settings.namespace,
                          'queues': sorted(queues), 'os': host_environment()['os'], 'mode': 'bridge_and_readonly_steps_v1_v2_v3'}), flush=True)
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(bridge_worker)
            await stack.enter_async_context(steps_worker)
            await stack.enter_async_context(human_worker)
            await stack.enter_async_context(approval_worker)
            reconciler = asyncio.create_task(reconcile())
            try:
                await asyncio.Future()
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
