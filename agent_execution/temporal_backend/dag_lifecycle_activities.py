"""Lifecycle I/O drains before acknowledging Activity cancellation."""
import asyncio
from temporalio import activity
from temporalio.exceptions import ApplicationError
from .dag_lifecycle import Lifecycle
from .dag_lifecycle_workflow import envelope


@activity.defn(name='coordinator.lifecycle.v1')
async def execute(value: dict) -> dict:
    envelope(value)
    task = asyncio.create_task(asyncio.to_thread(Lifecycle().execute, value['operation_id']))
    try:
        while not task.done():
            activity.heartbeat(value['operation_id'])
            await asyncio.wait({task}, timeout=.2)
        return await task
    except asyncio.CancelledError:
        # A merge may already be applying bytes: fence first, then join its I/O thread.
        life = Lifecycle()
        await asyncio.to_thread(life.cancel_pending,value['operation_id'])
        await asyncio.to_thread(life.graphs.stop,life.get(value['operation_id'])['graph_id'])
        await asyncio.shield(asyncio.gather(task, return_exceptions=True))
        raise
    except Exception as error:
        raise ApplicationError(type(error).__name__, non_retryable=True) from None
