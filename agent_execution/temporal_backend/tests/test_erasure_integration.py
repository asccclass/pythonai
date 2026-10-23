"""Real Service lifecycle erasure, active local references, History and offline recovery."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import threading
import time

import pytest
pytest.importorskip('temporalio')
from temporalio import activity
from temporalio.worker import Worker
from temporalio.service import RPCError,RPCStatusCode

from agent_execution.policy import current_context
from agent_execution.tasks import task_owner
from agent_execution.temporal_backend.tests.test_integration import configured,server
from agent_execution.temporal_backend.store import Store
from agent_execution.temporal_backend.config import Settings
from agent_execution.temporal_backend.contracts import ACTIVITY_NAME,READ_TOOLS
from agent_execution.temporal_backend.workflows import ReadOnlyBridge
from agent_execution.temporal_backend.client import dispatch
from agent_execution.temporal_backend.context import execution_context
from agent_execution.temporal_backend.retention import prepare_session
from agent_execution.temporal_backend.erasure import remote,purge
from agent_execution.workspace_lifecycle import LifecycleStore
from agent_execution.workspace_lifecycle.erasure import finalize
from agent_execution.workspace_lifecycle.operations import resume
from agent_execution.workspace_lifecycle.store import session_key


def prepare(configured,monkeypatch):
    runtime = configured/'.system_generated'
    for key,name in {'SCHEDULER_DB_PATH':'scheduler.db',
                     'AGENT_TRACE_DB':'traces.db','MEMORY_DB_PATH':'memory.db'}.items():
        monkeypatch.setenv(key,str(runtime/name))
    monkeypatch.setenv('AGENT_CODE_INDEX_DIR',str(configured.parent/(configured.name+'-index')))
    life = LifecycleStore()
    life.create('owned','temporary')
    life.create('keep','retained')
    (configured/'owned/note.txt').write_text('TP18 private user artifact',encoding='utf-8')
    (configured/'keep/note.txt').write_text('retained user artifact',encoding='utf-8')
    store = Store()
    context = current_context()
    owner = task_owner(context)
    store.bind(owner,context.task_id,'temporal')
    scope = {k:getattr(context,k) for k in ('source','identity','session','project','conversation')}
    scope.update(workspace=str(configured),allowed_tools=sorted(READ_TOOLS))
    row = store.submit(owner,context.task_id,[{'role':'user','content':'TP18 private input'}],scope,'admin',Settings.load())
    return life,store,row


async def resume_until_done(life,key):
    deadline = time.monotonic()+40
    while time.monotonic()<deadline:
        result = await asyncio.to_thread(resume,life,key)
        if result['state']=='completed':
            return result
        await asyncio.sleep(.2)
    raise AssertionError('Durable erasure did not converge')


def test_session_erasure_waits_for_real_local_activity_then_deletes_history(server,configured,monkeypatch):
    life,store,row = prepare(configured,monkeypatch)
    entered,release,exited = threading.Event(),threading.Event(),threading.Event()
    late = []
    @activity.defn(name=ACTIVITY_NAME,no_thread_cancel_exception=True)
    def held(payload:dict)->dict:
        saved = store.claim(payload['submission_id'],Settings.load())
        try:
            with execution_context(store,saved) as ledger:
                ledger.run('read','tool',{'path':'owned/note.txt'},lambda:{'content':'TP18 private ledger data'})
                entered.set()
                assert release.wait(45),'Test Activity was not released'
                try:
                    store.finish(saved['id'],saved['fence'],'completed',{'reply':'late resurrected output'})
                except PermissionError:
                    late.append('fenced')
        finally:
            exited.set()
        return {'submission_id':saved['id']}

    async def exercise():
        client = await server.client()
        with ThreadPoolExecutor(max_workers=2) as executor:
            async with Worker(client,task_queue=store.queue,workflows=[ReadOnlyBridge],
                              activities=[held],activity_executor=executor):
                handle = await dispatch(store,row,client)
                assert await asyncio.to_thread(entered.wait,15)
                store.tasks.cancel(row['owner'],row['task_id'])
                preview = life.preview('delete_session')
                assert preview['temporal'][0]['id']==row['id']
                try:
                    blocked = await asyncio.to_thread(life.confirm,preview['plan_id'])
                    assert blocked['state']=='blocked'
                    assert (configured/'owned/note.txt').exists()
                    assert store.get(row['id'])['state']=='erasing'
                    with closing(store.tasks.connect()) as conn:
                        assert conn.execute('SELECT state FROM wl_sessions WHERE id=?',(session_key(),)).fetchone()[0]=='deleting'
                        assert conn.execute('SELECT count(*) FROM temporal_artifacts').fetchone()[0]>0
                finally:
                    release.set()
                assert await asyncio.to_thread(exited.wait,10)
                await resume_until_done(life,preview['plan_id'])
                result = await asyncio.to_thread(finalize,life,preview['plan_id'])
                assert result['state']=='deleted'
                assert not (configured/'owned').exists()
                assert (configured/'keep/note.txt').read_text()=='retained user artifact'
                assert late==['fenced']
                with pytest.raises(RPCError) as error:
                    await handle.describe()
                assert error.value.status==RPCStatusCode.NOT_FOUND
                assert [v async for v in client.list_workflows(query="WorkflowId = '"+row['workflow_id']+"'")]==[]
                with closing(store.tasks.connect()) as conn:
                    assert conn.execute('SELECT count(*) FROM temporal_artifacts').fetchone()[0]==0
                    assert conn.execute('SELECT count(*) FROM temporal_submissions').fetchone()[0]==0
                    receipt = json.loads(conn.execute('SELECT receipt FROM temporal_erased_tasks').fetchone()[0])
                    assert receipt['history']==receipt['visibility']=='not_found_verified'
                # Process/store restart and repeated confirmation do not restore deleted data.
                assert await asyncio.to_thread(finalize,LifecycleStore(),preview['plan_id'])==result
                with pytest.raises(PermissionError):
                    Store().bind(row['owner'],row['task_id'],'temporal')
    try:
        asyncio.run(exercise())
    finally:
        release.set()




def test_expired_retention_cli_erases_closed_history_but_keeps_program_files(server,configured,monkeypatch):
    import os
    import subprocess
    import sys
    from agent_execution.temporal_backend.admission import owner_key
    life,store,row = prepare(configured,monkeypatch)
    @activity.defn(name=ACTIVITY_NAME,no_thread_cancel_exception=True)
    def complete(payload:dict)->dict:
        saved = store.claim(payload['submission_id'],Settings.load())
        with execution_context(store,saved) as ledger:
            ledger.run('read','tool',{'path':'owned/note.txt'},lambda:{'content':'TP18 private GC artifact'})
            with closing(store.tasks.connect()) as conn,conn:
                conn.execute("UPDATE tasks SET state='completed' WHERE owner=? AND id=?",(saved['owner'],saved['task_id']))
            store.finish(saved['id'],saved['fence'],'completed',{'reply':'TP18 private final output','messages':[]})
        return {'submission_id':saved['id']}
    async def exercise():
        client = await server.client()
        with ThreadPoolExecutor(max_workers=1) as executor:
            async with Worker(client,task_queue=store.queue,workflows=[ReadOnlyBridge],
                              activities=[complete],activity_executor=executor):
                handle = await dispatch(store,row,client)
                assert await handle.result()=={'submission_id':row['id']}
        with closing(store.tasks.connect()) as conn,conn:
            conn.execute('UPDATE temporal_submissions SET updated=? WHERE id=?',(time.time()-40*86400,row['id']))
        deadline = time.monotonic()+40
        while time.monotonic()<deadline:
            result = await asyncio.to_thread(subprocess.run,[sys.executable,'-m','agent_execution.temporal_backend.retention_cli',
                '--owner-hash',owner_key(row['owner']),'--days','30','--limit','1','--apply'],
                capture_output=True,text=True,timeout=45,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            assert result.returncode==0,result.stderr
            if json.loads(result.stdout)['results']==[{'id':row['id'],'state':'local_done'}]:
                break
            await asyncio.sleep(.2)
        else:
            raise AssertionError(result.stdout)
        with pytest.raises(RPCError) as error:
            await handle.describe()
        assert error.value.status==RPCStatusCode.NOT_FOUND
        assert (configured/'owned/note.txt').exists() and (configured/'keep/note.txt').exists()
        assert Store().bind(row['owner'],'new-task-after-retention','temporal')=='temporal'
        with closing(store.tasks.connect()) as conn:
            assert conn.execute('SELECT count(*) FROM temporal_artifacts').fetchone()[0]==0
            assert conn.execute('SELECT state FROM wl_sessions WHERE id=?',(session_key(),)).fetchone()[0]=='active'
    asyncio.run(exercise())


def test_offline_service_keeps_frozen_files_and_resumes_without_starting_task(server,configured,monkeypatch):
    life,store,row = prepare(configured,monkeypatch)
    asyncio.run(server.client())
    store.tasks.cancel(row['owner'],row['task_id'])
    preview = life.preview('delete_session')
    server.stop()
    result = life.confirm(preview['plan_id'])
    assert result['state']=='blocked'
    assert (configured/'owned/note.txt').exists()
    assert store.get(row['id'])['state']=='erasing'
    server.start()
    async def exercise():
        client = await server.client()
        await resume_until_done(life,preview['plan_id'])
        result = await asyncio.to_thread(finalize,life,preview['plan_id'])
        assert result['state']=='deleted'
        assert not (configured/'owned').exists()
        assert (configured/'keep/note.txt').exists()
        assert [v async for v in client.list_workflows()]==[]
    asyncio.run(exercise())
