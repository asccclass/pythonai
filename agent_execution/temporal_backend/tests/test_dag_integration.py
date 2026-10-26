"""Real Service/Worker Child Workflow, replay and process-fault validation for TP21."""
import asyncio
from contextlib import closing,asynccontextmanager
from datetime import timedelta
import json
from pathlib import Path
import sys
import time
from unittest.mock import AsyncMock
import pytest

pytest.importorskip('temporalio')
from temporalio import activity
from temporalio.worker import Worker,Replayer
from temporalio.client import WorkflowFailureError,WorkflowExecutionStatus
from agent_execution.temporal_backend.tests.test_integration import configured,server
from agent_execution.temporal_backend.tests.test_dag import setup,group,submit,other_root
from agent_execution.temporal_backend.dag import Graphs,reconcile
from agent_execution.temporal_backend.dag_activities import snapshot,fence,stop,execute
from agent_execution.temporal_backend.dag_contracts import RUN
from agent_execution.temporal_backend.dag_workflow import CoordinatorDAG,CoordinatorNode
from agent_execution.coordinator import merge_node,node_task_owner
from agent_execution.temporal_backend.references import process_stamp
from agent_execution.worktrees import WorktreeStore,git


@activity.defn(name=RUN)
async def controlled(value:dict)->dict:
    return await execute(value,worker_command=[sys.executable,'-m','agent_execution.temporal_backend.tests.dag_fixture'])


@asynccontextmanager
async def worker(client,s):
    async with Worker(client,task_queue=s.graphs.queue,workflows=[CoordinatorDAG,CoordinatorNode],
            activities=[snapshot,fence,stop],graceful_shutdown_timeout=timedelta(seconds=10)),Worker(
            client,task_queue=s.graphs.queue+'-io',activities=[controlled],max_concurrent_activities=2,
            graceful_shutdown_timeout=timedelta(seconds=10)):
        yield


async def eventually(predicate,seconds=45):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        value=predicate()
        if value:return value
        await asyncio.sleep(.1)
    raise AssertionError('DAG condition timed out')


def configure_index(s,monkeypatch):
    monkeypatch.setenv('AGENT_CODE_INDEX_DIR',str(s.root/'code-index'))


def test_real_dag_planner_coder_reviewer_shared_budget_replay_and_guarded_merge(server,setup,monkeypatch):
    s=setup
    configure_index(s,monkeypatch)
    planner=submit(s,'planner','private TP21 plan')
    coder=submit(s,'coder','Create result',[planner])
    follower=submit(s,'planner','after reviewed coder',[coder])
    key=group(s)['id']
    s.graphs.seal(key)
    original_head=git(s.repo,'rev-parse','HEAD')
    async def exercise():
        client=await server.client()
        async with worker(client,s):
            handle=await s.graphs.dispatch(key,client)
            assert await asyncio.wait_for(handle.result(),90)=={'graph_id':key}
            assert group(s)['state']=='completed'
            assert s.store.get(s.owner,coder)['state']=='reviewed'
            assert s.store.get(s.owner,follower)['state']=='completed'
            assert not (s.repo/'result.txt').exists()
            assert s.budget.snapshot()['children']==4 and s.budget.snapshot()['requests']>=4
            with closing(s.store.connect()) as conn:
                nodes=s.graphs.nodes(conn,group(s))
                assert all(n['attempts']==1 for n in nodes)
                assert conn.execute("SELECT count(*) FROM task_budgets WHERE id LIKE 'subagent:%'").fetchone()[0]==0
            history=await handle.fetch_history()
            encoded=history.to_json()
            assert 'private TP21 plan' not in encoded and str(s.repo) not in encoded and 'private result' not in encoded
            starts=[e.start_child_workflow_execution_initiated_event_attributes for e in history.events
                if e.HasField('start_child_workflow_execution_initiated_event_attributes')]
            assert len(starts)==4 and all(e.parent_close_policy==3 for e in starts)
            await Replayer(workflows=[CoordinatorDAG,CoordinatorNode]).replay_workflow(history)
            for node in nodes:
                child=client.get_workflow_handle(s.graphs.child_id(key,node['id']))
                child_history=await child.fetch_history()
                assert str(s.repo) not in child_history.to_json()
                await Replayer(workflows=[CoordinatorDAG,CoordinatorNode]).replay_workflow(child_history)
        merged=merge_node(s.store,s.owner,coder)
        assert merged['agent']==coder and (s.repo/'result.txt').read_text()=='result'
        assert git(s.repo,'rev-parse','HEAD')==original_head
    asyncio.run(exercise())


def test_real_reviewer_rejection_keeps_dependent_blocked(server,setup,monkeypatch):
    s=setup
    configure_index(s,monkeypatch)
    coder=submit(s,'coder','Create result reject')
    follower=submit(s,'planner','blocked',[coder])
    key=group(s)['id']
    s.graphs.seal(key)
    async def exercise():
        client=await server.client()
        async with worker(client,s):
            handle=await s.graphs.dispatch(key,client)
            await asyncio.wait_for(handle.result(),60)
        assert group(s)['state']=='needs_input'
        assert s.store.get(s.owner,coder)['state']=='rejected'
        assert s.store.get(s.owner,follower)['state']=='failed'
        with pytest.raises(ValueError):merge_node(s.store,s.owner,coder)
    asyncio.run(exercise())


@pytest.mark.parametrize('goal',['Create result crash','Create result crash-before'])
def test_unknown_process_completion_never_replays_under_native_dag(server,setup,monkeypatch,goal):
    s=setup
    configure_index(s,monkeypatch)
    coder=submit(s,'coder',goal)
    key=group(s)['id']
    s.graphs.seal(key)
    async def exercise():
        client=await server.client()
        async with worker(client,s):
            handle=await s.graphs.dispatch(key,client)
            await asyncio.wait_for(handle.result(),60)
        assert group(s)['state']=='needs_input'
        assert s.store.get(s.owner,coder)['state']=='needs_resolution'
        s.store.recover()
        async with worker(client,s):
            assert await s.graphs.dispatch(key,client) is None
            assert await client.get_workflow_handle(s.graphs.workflow_id(key)).result()=={'graph_id':key}
        assert s.store.get(s.owner,coder)['attempts']==1
        effects=s.root/'worktrees'/(coder+'.effects.log')
        count=effects.read_text().count('hit') if effects.exists() else 0
        assert count==(0 if 'crash-before' in goal else 1)
        assert not (s.repo/'result.txt').exists()
    asyncio.run(exercise())


def test_worker_restart_preserves_dynamic_dag_and_stable_children(server,setup,monkeypatch):
    s=setup
    configure_index(s,monkeypatch)
    planner=submit(s)
    key=group(s)['id']
    async def exercise():
        client=await server.client()
        async with worker(client,s):
            handle=await s.graphs.dispatch(key,client)
            await eventually(lambda:s.store.get(s.owner,planner)['state']=='completed')
        coder=submit(s,'coder','Create result',[planner])
        s.graphs.seal(key)
        async with worker(client,s):
            await asyncio.wait_for(handle.result(),60)
        assert s.store.get(s.owner,coder)['state']=='reviewed'
        assert s.store.get(s.owner,planner)['attempts']==1
        assert s.budget.snapshot()['children']==3
        history=await handle.fetch_history()
        await Replayer(workflows=[CoordinatorDAG,CoordinatorNode]).replay_workflow(history)
    asyncio.run(exercise())


def test_lost_start_reply_reconciles_same_parent_and_children(server,setup,monkeypatch):
    s=setup
    configure_index(s,monkeypatch)
    planner=submit(s)
    key=group(s)['id']
    s.graphs.seal(key)
    async def exercise():
        client=await server.client()
        async def lost(*args,**kwargs):
            await client.start_workflow(*args,**kwargs)
            raise TimeoutError('lost reply')
        proxy=type('ClientProxy',(),{'namespace':client.namespace,'start_workflow':staticmethod(lost)})()
        with pytest.raises(TimeoutError):await s.graphs.dispatch(key,proxy)
        assert not group(s)['dispatched']
        async with worker(client,s):
            handle=await s.graphs.dispatch(key,client)
            await asyncio.wait_for(handle.result(),60)
        assert s.store.get(s.owner,planner)['attempts']==1
        assert s.budget.snapshot()['children']==1
    asyncio.run(exercise())


@pytest.mark.parametrize('kind',['cancel','terminate'])
def test_parent_close_policy_cancels_children_and_joins_real_process_tree(server,setup,monkeypatch,kind):
    s=setup
    configure_index(s,monkeypatch)
    coder=submit(s,'coder','long process')
    key=group(s)['id']
    s.graphs.seal(key)
    async def exercise():
        client=await server.client()
        async with worker(client,s):
            handle=await s.graphs.dispatch(key,client)
            node=s.store.get(s.owner,coder)
            trees=WorktreeStore(node['payload']['repository'],node['payload']['storage'])
            workspace=trees.location(node_task_owner(node['payload']),'subagent:'+coder)
            await eventually(lambda:(workspace/'worker.pid').exists(),60)
            pid=int((workspace/'worker.pid').read_text())
            if kind=='cancel':await handle.cancel()
            else:await handle.terminate('isolated parent close test')
            with pytest.raises(WorkflowFailureError):await asyncio.wait_for(handle.result(),30)
            await eventually(lambda:process_stamp(pid) is None,30)
            child=client.get_workflow_handle(s.graphs.child_id(key,coder))
            end=time.monotonic()+30
            while (await child.describe()).status==WorkflowExecutionStatus.RUNNING and time.monotonic()<end:
                await asyncio.sleep(.2)
            assert (await child.describe()).status==WorkflowExecutionStatus.CANCELED
            await reconcile(s.graphs,client)
            assert s.store.get(s.owner,coder)['state'] in {'cancelled','needs_resolution'}
            assert not (s.repo/'result.txt').exists()
            with closing(s.store.connect()) as conn:
                assert conn.execute("SELECT state FROM temporal_agent_runs WHERE node_id=?",(coder,)).fetchone()[0]=='completed'
    asyncio.run(exercise())


def test_children_cannot_refill_root_request_budget(server,setup,monkeypatch):
    s=setup
    configure_index(s,monkeypatch)
    limits=json.loads(s.budget.snapshot()['limits']);limits['requests']=1
    with closing(s.store.connect()) as conn,conn:
        conn.execute('UPDATE task_budgets SET limits=? WHERE owner=? AND id=?',(json.dumps(limits),s.owner,'root'))
    s.budget.max_requests=1
    planner=submit(s,'planner','plan once')
    coder=submit(s,'coder','Create result',[planner])
    key=group(s)['id']
    s.graphs.seal(key)
    async def exercise():
        client=await server.client()
        async with worker(client,s):
            handle=await s.graphs.dispatch(key,client)
            await asyncio.wait_for(handle.result(),60)
        assert s.budget.snapshot()['requests']==1
        assert s.store.get(s.owner,coder)['state'] in {'needs_input','failed'}
        assert not (s.repo/'result.txt').exists()
        assert not (s.root/'worktrees'/(coder+'.effects.log')).exists()
    asyncio.run(exercise())


def test_service_offline_dispatch_stays_native_and_resumes_once(server,setup,monkeypatch):
    s=setup
    configure_index(s,monkeypatch)
    planner=submit(s)
    key=group(s)['id']
    s.graphs.seal(key)
    async def exercise():
        client=await server.client()
        server.stop()
        with pytest.raises(Exception):await s.graphs.dispatch(key,client)
        assert not group(s)['dispatched'] and s.store.claim() is None
        assert s.budget.snapshot()['children']==1
        server.start()
        client=await server.client()
        async with worker(client,s):
            handle=await s.graphs.dispatch(key,client)
            await asyncio.wait_for(handle.result(),60)
        assert s.store.get(s.owner,planner)['attempts']==1
    asyncio.run(exercise())


@pytest.mark.parametrize('offline',[False,True])
def test_session_erasure_removes_exact_dag_histories_and_preserves_files_until_verified(server,setup,monkeypatch,offline):
    from agent_execution.workspace_lifecycle import LifecycleStore
    from agent_execution.workspace_lifecycle.operations import resume
    from agent_execution.workspace_lifecycle.erasure import finalize
    from agent_execution.temporal_backend.store import digest
    from temporalio.service import RPCError,RPCStatusCode
    s=setup
    configure_index(s,monkeypatch)
    planner=submit(s,'planner','private DAG prompt for erasure')
    key=group(s)['id']
    s.graphs.seal(key)
    async def exercise():
        client=await server.client()
        async with worker(client,s):
            handle=await s.graphs.dispatch(key,client)
            await asyncio.wait_for(handle.result(),60)
            life=LifecycleStore()
            life.create('owned','temporary')
            (s.repo/'owned/private.txt').write_text('private artifact',encoding='utf-8')
            with closing(s.store.connect()) as conn,conn:
                conn.execute("UPDATE tasks SET state='completed',lease_owner=NULL,lease_until=NULL WHERE owner=? AND id='root'",(s.owner,))
            value=life.preview('delete_session')
            assert life.plan(value['plan_id'])['payload']['dags'][0]['nodes']==[planner]
            if offline:server.stop()
            result=await asyncio.to_thread(life.confirm,value['plan_id'])
            if offline:
                assert result['state']=='blocked' and (s.repo/'owned/private.txt').exists()
                server.start()
                await server.client()
            end=time.monotonic()+60
            while result['state']!='completed' and time.monotonic()<end:
                await asyncio.sleep(.2)
                result=await asyncio.to_thread(resume,life,value['plan_id'])
            assert result['state']=='completed',result
            final=await asyncio.to_thread(finalize,life,value['plan_id'])
            assert final['state']=='deleted' and not (s.repo/'owned').exists()
            assert (s.repo/'original.txt').read_text()=='original'
            for identity in [s.graphs.workflow_id(key),s.graphs.child_id(key,planner)]:
                with pytest.raises(RPCError) as absent:await client.get_workflow_handle(identity).describe()
                assert absent.value.status==RPCStatusCode.NOT_FOUND
            with closing(s.store.connect()) as conn:
                assert not conn.execute('SELECT 1 FROM temporal_agent_graphs').fetchone()
                assert not conn.execute('SELECT 1 FROM temporal_agent_runs').fetchone()
                receipt=json.loads(conn.execute('SELECT receipt FROM temporal_erased_dags WHERE token=?',(digest(key),)).fetchone()[0])
                assert receipt['history']=='not_found_verified' and receipt['visibility']=='not_found_verified'
                assert tuple(conn.execute('SELECT goal,payload FROM agent_nodes WHERE id=?',(planner,)).fetchone())==('','{}')
            assert any(item.get('graph_ref')==digest(key) for item in final['temporal'])
    asyncio.run(exercise())


def test_native_io_waits_for_shared_legacy_concurrency_without_recounting(server,setup,monkeypatch):
    s=setup
    configure_index(s,monkeypatch)
    submit(s)
    submit(s)
    key=group(s)['id']
    s.graphs.seal(key)
    legacy=other_root(s,backend='sqlite')
    held=s.store.claim()
    assert held['id']==legacy
    async def exercise():
        client=await server.client()
        async with worker(client,s):
            handle=await s.graphs.dispatch(key,client)
            peak=0
            end=time.monotonic()+60
            while time.monotonic()<end:
                with closing(s.store.connect()) as conn:
                    count=conn.execute("SELECT count(*) FROM agent_nodes WHERE root='root' AND state='running'").fetchone()[0]
                peak=max(peak,count)
                if group(s)['state']=='completed':break
                await asyncio.sleep(.05)
            await asyncio.wait_for(handle.result(),5)
        assert peak==1  # The other global slot belongs to the legacy lease.
        assert s.budget.snapshot()['children']==2 and s.budget.snapshot()['requests']==2
        with closing(s.store.connect()) as conn:
            assert all(n['attempts']==1 for n in s.graphs.nodes(conn,group(s)))
        s.store.fail(held,'cancelled')
    asyncio.run(exercise())
