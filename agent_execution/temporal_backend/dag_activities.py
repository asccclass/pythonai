"""DAG Activities reuse the native coordinator subprocess and shared root budget."""
import asyncio
from contextlib import closing
import sys
import threading
import time
from pathlib import Path
from temporalio import activity
from temporalio.exceptions import ApplicationError
from .dag_contracts import SNAPSHOT,RUN,FENCE,STOP,envelope
from .dag import Graphs,ConcurrencyFull


def safe(operation):
    try:return operation()
    except Exception as error:
        raise ApplicationError(type(error).__name__,non_retryable=True) from None


@activity.defn(name=SNAPSHOT)
async def snapshot(value:dict)->dict:
    envelope(value)
    return await asyncio.to_thread(safe,lambda:Graphs().snapshot(value['graph_id']))


@activity.defn(name=FENCE)
async def fence(value:dict)->dict:
    envelope(value,child=True)
    await asyncio.to_thread(safe,lambda:Graphs().node_stopped(value['graph_id'],value['node_id']))
    return value


@activity.defn(name=STOP)
async def stop(value:dict)->dict:
    envelope(value)
    await asyncio.to_thread(safe,lambda:Graphs().stop(value['graph_id']))
    return value


async def execute(value,graphs=None,worker_command=None):
    envelope(value,child=True)
    graphs=graphs or Graphs()
    def outcome():
        graph=graphs.get(value['graph_id'])
        node=graphs.coordinator.get(graph['owner'],value['node_id'])
        return {**value,'cancelled':bool(graph['state'] in {'cancelling','cancelled'} or graph['stop_reason'] or node['state']=='cancelled')}
    while True:
        try:
            node=await asyncio.to_thread(graphs.claim,value['graph_id'],value['node_id'])
            break
        except ConcurrencyFull:
            activity.heartbeat(value['node_id'])
            await asyncio.sleep(.2)
        except Exception as error:
            result=outcome()
            if result['cancelled']:return result
            raise ApplicationError(type(error).__name__,non_retryable=True) from None
    if node is None:return outcome()
    stopped=threading.Event()
    from agent_execution.process import run_process
    command=[*(worker_command or [sys.executable,'-m','agent_execution.coordinator_worker']),
        '--db',str(graphs.path),'--node',node['id'],'--lease',node['lease']]
    def launch():
        try:
            # coordinator_worker restores role, root budget, depth and persona tools.
            return run_process(command,cwd=Path(__file__).resolve().parents[2],
                timeout=max(.1,node['deadline']-time.time()),capture_output=True,
                check_cancelled=lambda:stopped.is_set() or graphs.coordinator.cancelled(node,node['lease']))
        finally:
            graphs.process_done(value['graph_id'],node)
    process=asyncio.create_task(asyncio.to_thread(launch))
    try:
        while not process.done():
            activity.heartbeat(value['node_id'])
            if not graphs.coordinator.renew(node):
                stopped.set()
            await asyncio.wait({process},timeout=.2)
        await process
    except BaseException:
        stopped.set()
        graphs.node_stopped(value['graph_id'],value['node_id'])
        # A cancellation acknowledgement must not race an active child process.
        try:await asyncio.shield(process)
        except BaseException:pass
        raise
    return outcome()


@activity.defn(name=RUN)
async def run(value:dict)->dict:
    try:return await execute(value)
    except asyncio.CancelledError:raise
    except Exception as error:
        raise ApplicationError(type(error).__name__,non_retryable=True) from None


CONTROL_ACTIVITIES=[snapshot,fence,stop]
IO_ACTIVITIES=[run]
ACTIVITIES=CONTROL_ACTIVITIES+IO_ACTIVITIES
