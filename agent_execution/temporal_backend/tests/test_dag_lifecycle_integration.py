"""Isolated real Service lifecycle execution, restart, replay and erasure targets."""
import asyncio
from contextlib import closing
from datetime import timedelta
import os
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import pytest

pytest.importorskip('temporalio')
from temporalio.worker import Worker,Replayer
from temporalio import activity
from temporalio.client import WorkflowFailureError,WorkflowExecutionStatus
from agent_execution.temporal_backend.tests.test_integration import configured,server
from agent_execution.temporal_backend.tests.test_dag import setup,group
from agent_execution.temporal_backend.tests.test_dag_lifecycle import reviewed
from agent_execution.temporal_backend.dag_lifecycle import Lifecycle
from agent_execution.temporal_backend.dag_lifecycle_workflow import CoordinatorLifecycle
from agent_execution.temporal_backend.dag_lifecycle_activities import execute
from agent_execution.worktrees import WorktreeStore


@pytest.mark.parametrize('action',['merge','archive'])
def test_service_lifecycle_worker_restart_replay_and_idempotent_result(server,setup,action):
    s=setup
    node,path=reviewed(s)
    if action=='archive':
        s.store.cancel(s.owner,node)
        s.graphs.seal(group(s)['id']);s.graphs.snapshot(group(s)['id'])
    life=Lifecycle(s.graphs)
    key=life.request(group(s)['id'],node,action)
    budget=s.budget.snapshot()
    async def exercise():
        client=await server.client()
        # The intent and remote Workflow exist before a Worker starts.
        handle=await life.dispatch(key,client)
        assert (await life.dispatch(key,client)).id==handle.id
        async with Worker(client,task_queue=life.queue,workflows=[CoordinatorLifecycle],activities=[execute]):
            assert await asyncio.wait_for(handle.result(),45)=={'operation_id':key}
        assert life.get(key)['state']=='completed'
        assert (s.repo/'result.txt').exists() if action=='merge' else not path.exists()
        history=await handle.fetch_history()
        assert 'private' not in history.to_json() and str(s.repo) not in history.to_json()
        await Replayer(workflows=[CoordinatorLifecycle]).replay_workflow(history)
        scheduled=[e.activity_task_scheduled_event_attributes for e in history.events if e.HasField('activity_task_scheduled_event_attributes')]
        assert scheduled and all(e.retry_policy.maximum_attempts==1 for e in scheduled)
        if action=='archive':WorktreeStore(s.repo,s.root/'worktrees').restore(s.owner,node)
        with patch.object(Lifecycle,'execute',side_effect=AssertionError('Completed lifecycle must never redo I/O')):
            async with Worker(client,task_queue=life.queue,workflows=[CoordinatorLifecycle],activities=[execute],
                              max_heartbeat_throttle_interval=timedelta(seconds=2)):
                again=await life.dispatch(key,client)
                assert await again.result()=={'operation_id':key}
        if action=='archive':assert path.exists()
        assert s.budget.snapshot()==budget
    asyncio.run(exercise())


def test_unknown_start_reply_reconciles_same_lifecycle_workflow(server,setup):
    s=setup;node,_=reviewed(s);life=Lifecycle(s.graphs)
    key=life.request(group(s)['id'],node,'merge')
    async def exercise():
        client=await server.client()
        async def lost(*args,**kwargs):
            await client.start_workflow(*args,**kwargs)
            raise ConnectionError('reply lost')
        with pytest.raises(ConnectionError):await life.dispatch(key,SimpleNamespace(namespace=client.namespace,start_workflow=lost))
        async with Worker(client,task_queue=life.queue,workflows=[CoordinatorLifecycle],activities=[execute]):
            handle=await life.dispatch(key,client)
            await asyncio.wait_for(handle.result(),45)
        assert life.get(key)['state']=='completed'
        assert len([w async for w in client.list_workflows(query="WorkflowId = '"+handle.id+"'")])==1
    asyncio.run(exercise())


def test_lifecycle_history_participates_in_session_erasure(server,setup):
    from agent_execution.workspace_lifecycle import LifecycleStore
    from agent_execution.temporal_backend.dag_erasure import erase
    from agent_execution.temporal_backend.erasure import ErasurePending
    from temporalio.service import RPCError,RPCStatusCode
    s=setup;node,_=reviewed(s);life=Lifecycle(s.graphs)
    key=life.request(group(s)['id'],node,'merge')
    async def exercise():
        client=await server.client()
        async with Worker(client,task_queue=life.queue,workflows=[CoordinatorLifecycle],activities=[execute]):
            handle=await life.dispatch(key,client);await asyncio.wait_for(handle.result(),45)
        lifecycle=LifecycleStore();session=lifecycle.ensure_session()
        with closing(s.store.connect()) as conn,conn:
            conn.execute("UPDATE wl_sessions SET state='deleting' WHERE id=?",(session,))
        for attempt in range(100):
            try:
                await erase(s.graphs,session)
                break
            except ErasurePending:
                if attempt==99:raise
                await asyncio.sleep(.2)
        with pytest.raises(RPCError) as caught:await handle.describe()
        assert caught.value.status==RPCStatusCode.NOT_FOUND
        assert not [w async for w in client.list_workflows(query="WorkflowId = '"+handle.id+"'")]
        assert (s.repo/'result.txt').exists(), 'Remote deletion cannot erase parent source files'
    asyncio.run(exercise())


@pytest.mark.parametrize('close_mode',['cancel','terminate'])
def test_remote_cancellation_waits_for_merge_thread_and_rolls_back_written_bytes(server,setup,close_mode):
    s=setup;node,_=reviewed(s);life=Lifecycle(s.graphs)
    key=life.request(group(s)['id'],node,'merge')
    written,release=threading.Event(),threading.Event()
    original=os.replace
    def replace(source,target):
        original(source,target)
        if str(target)==str(s.repo/'result.txt'):
            written.set()
            assert release.wait(60),'Test release timed out'
    async def exercise():
        client=await server.client()
        with patch('agent_execution.coordinator.os.replace',side_effect=replace):
            async with Worker(client,task_queue=life.queue,workflows=[CoordinatorLifecycle],activities=[execute],
                              max_heartbeat_throttle_interval=timedelta(seconds=2)):
                handle=await life.dispatch(key,client)
                try:
                    assert await asyncio.to_thread(written.wait,15)
                    if close_mode=='cancel':await handle.cancel()
                    else:
                        await handle.terminate('Test terminal parent fence')
                        await asyncio.to_thread(life.observe,key,'TERMINATED')
                    for _ in range(200):
                        if s.graphs.get(group(s)['id'])['stop_reason']:break
                        await asyncio.sleep(.1)
                    assert s.graphs.get(group(s)['id'])['stop_reason']
                    if close_mode=='cancel':assert (await handle.describe()).status==WorkflowExecutionStatus.RUNNING
                    from agent_execution.temporal_backend.dag import busy
                    with closing(s.store.connect()) as conn:assert busy(conn,[s.owner])
                finally:release.set()
                with pytest.raises(WorkflowFailureError):await asyncio.wait_for(handle.result(),20)
                for _ in range(100):
                    if life.get(key)['state']!='started':break
                    await asyncio.sleep(.1)
        assert not (s.repo/'result.txt').exists()
        assert s.store.get(s.owner,node)['state']!='merged'
        assert life.get(key)['state']=='uncertain'
    asyncio.run(exercise())
