"""Native Schedule RPC/Worker tests against the isolated persistent development service."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import time
from unittest.mock import patch

import pytest
pytest.importorskip('temporalio')
from temporalio import activity
from temporalio.worker import Worker,Replayer
from temporalio.service import RPCError,RPCStatusCode
from temporalio.client import ScheduleOverlapPolicy

from agent_execution.temporal_backend.tests.test_integration import configured,server
from agent_execution.temporal_backend.schedules import Schedules
from agent_execution.temporal_backend.schedule_workflow import ScheduleOccurrence
from agent_execution.temporal_backend.schedule_activities import ACTIVITIES
from agent_execution.temporal_backend.workflows import ReadOnlyBridge
from agent_execution.temporal_backend.contracts import ACTIVITY_NAME,validate_envelope
from agent_execution.temporal_backend.activities import run_submission
from agent_execution.temporal_backend.config import Settings

async def eventually(predicate,seconds=30):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=predicate()
        if value:
            return value
        await asyncio.sleep(.1)
    raise AssertionError('Condition did not become true')

async def applied(schedules,key,client):
    deadline=time.monotonic()+10
    while True:
        try:
            return await schedules.sync(key,client)
        except RuntimeError as error:
            if 'Schedule RPC is already in progress' not in str(error) or time.monotonic()>=deadline:
                raise
            await asyncio.sleep(.1)

def test_native_schedule_crud_occurrences_limit_and_replay(server,monkeypatch,configured):
    monkeypatch.setenv('AGENT_CODE_INDEX_DIR',str(configured.parent/(configured.name+'-index')))
    schedules=Schedules()
    key=schedules.create('private schedule','0 0 1 1 *','watch',{'prompt':'TP19 private scheduled prompt'},max_runs=2)
    executed=[]

    @activity.defn(name=ACTIVITY_NAME)
    def run(envelope:dict)->dict:
        activity.heartbeat('started')
        def runner(messages,saved):
            executed.append(1)
            return '{"state":"waiting","summary":"unchanged"}'
        return run_submission(validate_envelope(envelope),Settings.load(),runner=runner)

    async def exercise():
        client=await server.client()
        with ThreadPoolExecutor(max_workers=2) as executor:
            async with Worker(client,task_queue=schedules.queue,workflows=[ScheduleOccurrence],activities=ACTIVITIES), Worker(
                client,task_queue=schedules.tasks.queue,workflows=[ReadOnlyBridge],activities=[run],activity_executor=executor):
                await applied(schedules,key,client)
                handle=client.get_schedule_handle(schedules.remote_id(schedules.get(key)))
                description=await handle.describe()
                assert description.schedule.state.paused
                assert description.schedule.spec.time_zone_name=='Asia/Taipei'
                assert description.schedule.policy.overlap==ScheduleOverlapPolicy.SKIP
                assert description.schedule.policy.catchup_window.total_seconds()==10
                schedules.change(key,'update',timezone='UTC')
                await applied(schedules,key,client)
                assert (await handle.describe()).schedule.spec.time_zone_name=='UTC'
                schedules.change(key,'resume')
                await applied(schedules,key,client)
                assert not (await handle.describe()).schedule.state.paused
                await handle.trigger()
                await eventually(lambda:len(executed)==1)
                def ended():
                    with closing(schedules.scheduler.connect()) as conn:
                        return conn.execute("SELECT * FROM temporal_schedule_occurrences WHERE state='waiting'").fetchone()
                first=await eventually(ended)
                wrapper=client.get_workflow_handle(first['workflow_id'])
                assert await asyncio.wait_for(wrapper.result(),30)=={'done':True}
                history=await wrapper.fetch_history()
                assert 'TP19 private scheduled prompt' not in history.to_json()
                await Replayer(workflows=[ScheduleOccurrence]).replay_workflow(history)
                await handle.trigger()
                await eventually(lambda:len(executed)==2)
                await eventually(lambda:bool(schedules.scheduler.notifications()))
                assert schedules.get(key)['run_count']==2
                assert schedules.scheduler.notifications()[0]['state']=='needs_input'
                await applied(schedules,key,client)
                assert (await handle.describe()).schedule.state.paused
                # Native manual trigger cannot bypass the local logical run ceiling.
                await handle.trigger()
                await asyncio.sleep(2)
                assert len(executed)==2 and schedules.get(key)['run_count']==2
                schedules.change(key,'cancel')
                await applied(schedules,key,client)
                with pytest.raises(RPCError) as caught:
                    await handle.describe()
                assert caught.value.status==RPCStatusCode.NOT_FOUND
    asyncio.run(exercise())

def test_schedule_offline_intent_reconciles_without_legacy_execution(server):
    schedules=Schedules()
    key=schedules.create('offline','0 0 1 1 *','agent',{'prompt':'read only'})
    async def exercise():
        client=await server.client()
        await applied(schedules,key,client)
        server.stop()
        schedules.change(key,'resume')
        with pytest.raises(Exception):
            await applied(schedules,key,client)
        row=schedules.get(key)
        assert row['applied']<row['revision']
        assert schedules.scheduler.get_due_jobs()==[]
        server.start()
        client=await server.client()
        await applied(schedules,key,client)
        row=schedules.get(key)
        assert row['applied']==row['revision']
        assert not (await client.get_schedule_handle(schedules.remote_id(row)).describe()).schedule.state.paused
        schedules.change(key,'cancel')
        await applied(schedules,key,client)
    asyncio.run(exercise())


@pytest.mark.parametrize('overlap,expected',[('SKIP',1),('BUFFER_ONE',2)])
def test_native_overlap_is_enforced_while_occurrence_is_active(server,configured,monkeypatch,overlap,expected):
    import threading
    monkeypatch.setenv('AGENT_CODE_INDEX_DIR',str(configured.parent/(configured.name+'-index')))
    schedules=Schedules()
    key=schedules.create('overlap','0 0 1 1 *','agent',{'prompt':'read only'},overlap=overlap,paused=False,max_runs=3)
    entered,release=threading.Event(),threading.Event()
    executed=[]
    @activity.defn(name=ACTIVITY_NAME,no_thread_cancel_exception=True)
    def held(envelope:dict)->dict:
        activity.heartbeat('started')
        def runner(messages,saved):
            executed.append(1)
            entered.set()
            assert release.wait(30)
            return 'completed'
        return run_submission(validate_envelope(envelope),Settings.load(),runner=runner)
    async def exercise():
        client=await server.client()
        with ThreadPoolExecutor(max_workers=2) as executor:
            async with Worker(client,task_queue=schedules.queue,workflows=[ScheduleOccurrence],activities=ACTIVITIES), Worker(
                client,task_queue=schedules.tasks.queue,workflows=[ReadOnlyBridge],activities=[held],activity_executor=executor):
                await applied(schedules,key,client)
                handle=client.get_schedule_handle(schedules.remote_id(schedules.get(key)))
                await handle.trigger()
                try:
                    assert await asyncio.to_thread(entered.wait,20)
                    # Native manual triggers use second-resolution Workflow IDs.
                    # Distinct logical times are required to test overlap, rather
                    # than the bridge's same-occurrence deduplication.
                    await asyncio.sleep(1.1)
                    await handle.trigger()
                    await asyncio.sleep(1.1)
                    await handle.trigger()
                    await asyncio.sleep(.5)
                    assert len(executed)==1 and schedules.get(key)['run_count']==1
                finally:
                    release.set()
                await eventually(lambda:len(executed)==expected)
                await asyncio.sleep(3)
                assert len(executed)==expected and schedules.get(key)['run_count']==expected
                schedules.change(key,'cancel')
                await applied(schedules,key,client)
    asyncio.run(exercise())

def test_session_deletion_erases_native_schedule_and_wrapper_then_keeps_fence(server,configured,monkeypatch):
    from agent_execution.workspace_lifecycle import LifecycleStore
    from agent_execution.workspace_lifecycle.erasure import finalize
    from agent_execution.workspace_lifecycle.operations import resume
    from agent_execution.temporal_backend.tests.test_erasure_integration import resume_until_done
    from agent_execution.temporal_backend.store import digest
    runtime=configured/'.system_generated'
    monkeypatch.setenv('AGENT_TRACE_DB',str(runtime/'traces.db'))
    monkeypatch.setenv('MEMORY_DB_PATH',str(runtime/'memory.db'))
    monkeypatch.setenv('AGENT_CODE_INDEX_DIR',str(configured.parent/(configured.name+'-index')))
    from agent_execution.temporal_backend import erasure
    original_remote=erasure.remote
    failures=[]
    async def diagnostic(*args,**kwargs):
        try:
            return await original_remote(*args,**kwargs)
        except Exception as error:
            failures.append((type(error).__name__,str(error)))
            raise
    monkeypatch.setattr(erasure,'remote',diagnostic)
    life=LifecycleStore()
    life.create('owned','temporary')
    (configured/'owned/note.txt').write_text('private artifact',encoding='utf-8')
    schedules=Schedules()
    key=schedules.create('delete schedule','0 0 1 1 *','watch',{'prompt':'private scheduled prompt'},paused=False)
    @activity.defn(name=ACTIVITY_NAME)
    async def controlled(envelope:dict)->dict:
        return envelope  # No provider call; the original task is explicitly cancelled below.
    async def exercise():
        client=await server.client()
        async with Worker(client,task_queue=schedules.queue,workflows=[ScheduleOccurrence],activities=ACTIVITIES), Worker(
            client,task_queue=schedules.tasks.queue,workflows=[ReadOnlyBridge],activities=[controlled]):
            await applied(schedules,key,client)
            handle=client.get_schedule_handle(schedules.remote_id(schedules.get(key)))
            await handle.trigger()
            def started():
                with closing(schedules.scheduler.connect()) as conn:
                    return conn.execute('SELECT * FROM temporal_schedule_occurrences WHERE submission_id IS NOT NULL').fetchone()
            occurrence=await eventually(started)
            # The mapping commits before StartWorkflow outbox dispatch completes.
            # Wait for admission before freezing the cancellation/deletion preview.
            await eventually(lambda:schedules.tasks.get(occurrence['submission_id'])['state']=='dispatched')
            schedules.tasks.cancel(occurrence['submission_id'],schedules.get(key)['owner'])
            preview=life.preview('delete_session')
            assert key in [r['id'] for r in preview['schedules']]
            result=await asyncio.to_thread(life.confirm,preview['plan_id'])
            deadline=time.monotonic()+40
            while result['state']!='completed' and time.monotonic()<deadline:
                await asyncio.sleep(.2)
                result=await asyncio.to_thread(resume,life,preview['plan_id'])
            assert result['state']=='completed',(result,failures[-3:])
            result=await asyncio.to_thread(finalize,life,preview['plan_id'])
            assert result['state']=='deleted' and not (configured/'owned').exists()
            with pytest.raises(RPCError) as absent:
                await handle.describe()
            assert absent.value.status==RPCStatusCode.NOT_FOUND
            with pytest.raises(RPCError) as absent:
                await client.get_workflow_handle(occurrence['workflow_id']).describe()
            assert absent.value.status==RPCStatusCode.NOT_FOUND
            with closing(schedules.scheduler.connect()) as conn:
                assert conn.execute('SELECT count(*) FROM temporal_schedule_bindings').fetchone()[0]==0
                assert conn.execute('SELECT count(*) FROM temporal_schedule_occurrences').fetchone()[0]==0
                receipt=json.loads(conn.execute('SELECT receipt FROM temporal_erased_schedules WHERE token=?',(digest(key),)).fetchone()[0])
                assert receipt['schedule']=='not_found_verified'
            with pytest.raises(PermissionError):
                schedules.create('revive','* * * * *','watch',{'prompt':'denied'})
    asyncio.run(exercise())
