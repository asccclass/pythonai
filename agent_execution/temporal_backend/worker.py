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
    from .schedules import Schedules, reconcile as schedule_reconcile
    from .schedule_workflow import ScheduleOccurrence
    from .schedule_activities import ACTIVITIES as SCHEDULE_ACTIVITIES
    schedules = Schedules(tasks=store)
    from .dag import Graphs,reconcile as dag_reconcile
    from .dag_workflow import CoordinatorDAG,CoordinatorNode
    from .dag_activities import CONTROL_ACTIVITIES as DAG_CONTROL,IO_ACTIVITIES as DAG_IO
    from agent_execution.coordinator import Coordinator
    graphs=Graphs(Coordinator(store.path))
    from .dag_lifecycle import Lifecycle
    from .dag_lifecycle_workflow import CoordinatorLifecycle
    from .dag_lifecycle_activities import execute as lifecycle_execute
    lifecycle=Lifecycle(graphs)
    queues = {store.queue, store.queue_for('steps'), store.queue_for('human'), store.queue_for('approval')}
    async def reconcile():
        while True:
            from .cancellation import pending as cancellations, dispatch as cancel_workflow
            for row in cancellations(store, settings, queues):
                try:
                    await cancel_workflow(store, row, client)
                except Exception as error:
                    print(json.dumps({'temporal': 'cancellation_pending', 'error_type': type(error).__name__}), flush=True)
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
                        await cancel_workflow(store, row, client)
                        continue
                    status = (await client.get_workflow_handle(row['workflow_id']).describe()).status
                    if status == WorkflowExecutionStatus.CANCELED:
                        store.workflow_closed(row['id'], row['owner'], status.name)
                    elif status in {WorkflowExecutionStatus.FAILED, WorkflowExecutionStatus.TIMED_OUT,
                                    WorkflowExecutionStatus.TERMINATED}:
                        store.workflow_closed(row['id'], row['owner'], status.name)
                except Exception as error:
                    print(json.dumps({'temporal': 'reconcile_pending', 'error_type': type(error).__name__}), flush=True)
            try:
                from .publication import reconcile as publish_pending
                await asyncio.to_thread(publish_pending,store,settings,queues)
            except Exception as error:
                print(json.dumps({'temporal':'publication_pending','error_type':type(error).__name__}),flush=True)
            try:
                from .erasure import reconcile as erase_pending
                await erase_pending(store,settings,queues)
            except Exception as error:
                print(json.dumps({'temporal':'erasure_pending','error_type':type(error).__name__}),flush=True)
            await asyncio.sleep(5)
    async def reconcile_schedules():
        while True:
            try:
                await schedule_reconcile(schedules,client)
                from .schedule_migration import reconcile as migrate_schedules
                await migrate_schedules(schedules,client)
            except Exception as error:
                print(json.dumps({'temporal':'schedule_pending','error_type':type(error).__name__}),flush=True)
            await asyncio.sleep(5)
    async def reconcile_dags():
        while True:
            try:await dag_reconcile(graphs,client)
            except Exception as error:
                print(json.dumps({'temporal':'dag_pending','error_type':type(error).__name__}),flush=True)
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
                          'queues': sorted(queues), 'schedule_queue': schedules.queue,
                          'dag_queue':graphs.queue,'dag_io_queue':graphs.queue+'-io',
                          'lifecycle_queue':lifecycle.queue,
                          'os': host_environment()['os'], 'mode': 'bridge_readonly_steps_and_optin_dag_v1'}), flush=True)
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(Worker(client,task_queue=lifecycle.queue,
                workflows=[CoordinatorLifecycle],activities=[lifecycle_execute],
                max_concurrent_activities=1,max_heartbeat_throttle_interval=timedelta(seconds=2),
                graceful_shutdown_timeout=timedelta(seconds=30)))
            await stack.enter_async_context(Worker(client,task_queue=graphs.queue,
                workflows=[CoordinatorDAG,CoordinatorNode],activities=DAG_CONTROL,
                max_concurrent_activities=settings.concurrency+4,
                graceful_shutdown_timeout=timedelta(seconds=15)))
            await stack.enter_async_context(Worker(client,task_queue=graphs.queue+'-io',
                activities=DAG_IO,
                max_concurrent_activities=settings.concurrency,
                graceful_shutdown_timeout=timedelta(seconds=15)))
            await stack.enter_async_context(Worker(client,task_queue=schedules.queue,
                workflows=[ScheduleOccurrence],activities=SCHEDULE_ACTIVITIES,
                max_concurrent_activities=settings.concurrency,
                graceful_shutdown_timeout=timedelta(seconds=15)))
            await stack.enter_async_context(bridge_worker)
            await stack.enter_async_context(steps_worker)
            await stack.enter_async_context(human_worker)
            await stack.enter_async_context(approval_worker)
            reconciler = asyncio.create_task(reconcile())
            schedule_reconciler = asyncio.create_task(reconcile_schedules())
            dag_reconciler=asyncio.create_task(reconcile_dags())
            try:
                await asyncio.Future()
            finally:
                reconciler.cancel()
                schedule_reconciler.cancel()
                dag_reconciler.cancel()
                await asyncio.gather(reconciler, schedule_reconciler,dag_reconciler, return_exceptions=True)


def main():
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / '.env')
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
