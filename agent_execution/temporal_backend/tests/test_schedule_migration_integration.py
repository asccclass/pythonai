"""TP20 migration against an isolated persistent Temporal Service; no provider calls."""
import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime,timezone
import pytest

pytest.importorskip('temporalio')
from temporalio import activity
from temporalio.worker import Worker
from temporalio.service import RPCError,RPCStatusCode
from agent_execution.temporal_backend.tests.test_integration import configured,server
from agent_execution.temporal_backend.schedules import Schedules
from agent_execution.temporal_backend.schedule_migration import Migration,preview,times,migration,utc
from agent_execution.temporal_backend.workflows import ReadOnlyBridge
from agent_execution.temporal_backend.contracts import ACTIVITY_NAME,validate_envelope
from agent_execution.temporal_backend.activities import run_submission
from agent_execution.temporal_backend.config import Settings
from agent_execution.temporal_backend.client import dispatch


def prepare(expression='*/5 * * * *',zone='UTC'):
    s=Schedules()
    key=s.scheduler.add_job('legacy',expression,'watch',{'prompt':'private TP20 prompt'},timezone=zone,max_runs=5)
    m=Migration(s)
    m.stage(key,preview(key)['source_digest'])
    return s,m,key


def state(s,key):
    with closing(s.scheduler.connect()) as conn:
        return dict(migration(conn,key))


def test_real_cutover_execution_and_rollback(server,monkeypatch,configured):
    monkeypatch.setenv('AGENT_CODE_INDEX_DIR',str(configured.parent/(configured.name+'-index')))
    s,m,key=prepare()
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
        await m.apply(key,client)
        handle=client.get_schedule_handle(s.remote_id(s.get(key)))
        assert (await handle.describe()).schedule.state.paused
        m.cutover(key)
        await m.apply(key,client)
        assert state(s,key)['state']=='temporal'
        assert not (await handle.describe()).schedule.state.paused
        with closing(s.scheduler.connect()) as conn:
            assert conn.execute('SELECT is_enabled FROM scheduled_jobs WHERE id=?',(key,)).fetchone()[0]==0
        # Controlled scheduled timestamp tests admission without waiting for wall-clock cron.
        when=times('*/5 * * * *','UTC',state(s,key)['barrier'],1)[0]
        wrapper=s.remote_id(s.get(key))+'-'+when.replace('+00:00','Z')
        with ThreadPoolExecutor(max_workers=1) as executor:
            async with Worker(client,task_queue=s.tasks.queue,workflows=[ReadOnlyBridge],activities=[run],activity_executor=executor):
                occurrence=s.prepare(key,wrapper)
                with closing(s.scheduler.connect()) as conn:
                    record=conn.execute('SELECT * FROM temporal_schedule_occurrences WHERE job_id=?',(key,)).fetchone()
                task=s.tasks.get(record['submission_id'],s.get(key)['owner'])
                await dispatch(s.tasks,task,client)
                await asyncio.wait_for(client.get_workflow_handle(task['workflow_id']).result(),30)
                s.poll(key,occurrence['occurrence_id'])
                assert len(executed)==1 and s.get(key)['run_count']==1
                before=s.tasks.verify_budget_contract(task)
                m.rollback(key)
                await m.apply(key,client)
                assert state(s,key)['state']=='sqlite'
                with pytest.raises(RPCError) as caught:
                    await handle.describe()
                assert caught.value.status==RPCStatusCode.NOT_FOUND
                with closing(s.scheduler.connect()) as conn:
                    row=dict(conn.execute('SELECT * FROM scheduled_jobs WHERE id=?',(key,)).fetchone())
                    assert row['execution_backend']=='sqlite' and row['is_enabled']==1 and row['run_count']==1
                    assert utc(row['next_run_at'])>utc(when)
                assert s.tasks.verify_budget_contract(s.tasks.get(task['id'],task['owner']))==before
                assert s.prepare(key,wrapper)['done']
    asyncio.run(exercise())


@pytest.mark.parametrize('expression,zone,base',[
    ('30 2 * * *','America/New_York','2026-03-07T00:00:00Z'),
    ('30 1 * * *','America/New_York','2026-11-01T00:00:00Z'),
    ('0 9 * * *','Asia/Taipei','2026-10-24T00:00:00Z')])
def test_service_next_ten_matches_dst_preview(server,expression,zone,base):
    s,m,key=prepare(expression,zone)
    async def exercise():
        client=await server.client()
        with closing(s.scheduler.connect()) as conn,conn:
            conn.execute('UPDATE temporal_schedule_bindings SET start_at=?,revision=revision+1 WHERE job_id=?',(base,key))
        await s.sync(key,client)
        await m.verify_times(key,client)
        assert (await client.get_schedule_handle(s.remote_id(s.get(key))).describe()).schedule.state.paused
    asyncio.run(exercise())


def test_offline_cutover_and_rollback_resume_without_enabling_both(server):
    s,m,key=prepare()
    async def exercise():
        client=await server.client()
        await m.apply(key,client)
        m.cutover(key)
        server.stop()
        with pytest.raises(Exception):await m.apply(key,client)
        assert state(s,key)['state']=='cutting_over'
        assert not s.scheduler.get_due_jobs()
        server.start()
        client=await server.client()
        await m.apply(key,client)
        m.rollback(key)
        server.stop()
        with pytest.raises(Exception):await m.apply(key,client)
        assert state(s,key)['state']=='rolling_back'
        assert not s.scheduler.get_due_jobs()
        server.start()
        client=await server.client()
        await m.apply(key,client)
        assert state(s,key)['state']=='sqlite'
    asyncio.run(exercise())


def test_deleted_remote_schedule_before_local_commit_is_restartable(server):
    s,m,key=prepare()
    async def exercise():
        client=await server.client()
        await m.apply(key,client)
        m.cutover(key)
        await m.apply(key,client)
        m.rollback(key)
        with closing(s.scheduler.connect()) as conn,conn:
            conn.execute("""CREATE TRIGGER interrupt_rollback BEFORE UPDATE OF execution_backend ON scheduled_jobs
                WHEN NEW.execution_backend='sqlite' BEGIN SELECT RAISE(ABORT,'injected interruption'); END""")
        with pytest.raises(sqlite3.IntegrityError,match='injected interruption'):
            await m.apply(key,client)
        assert state(s,key)['state']=='rolling_back'
        handle=client.get_schedule_handle(s.remote_id(s.get(key)))
        with pytest.raises(RPCError) as absent:await handle.describe()
        assert absent.value.status==RPCStatusCode.NOT_FOUND
        assert not s.scheduler.get_due_jobs()
        with closing(s.scheduler.connect()) as conn,conn:
            conn.execute('DROP TRIGGER interrupt_rollback')
        await m.apply(key,client)
        assert state(s,key)['state']=='sqlite'
    asyncio.run(exercise())
