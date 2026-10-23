"""Retained references, bounded GC and irreversible erasure fences."""
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,Mock,patch

import pytest
pytest.importorskip('temporalio')
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError,RPCStatusCode

from agent_execution import execution_scope
from agent_execution.policy import current_context
from agent_execution.tasks import TaskStore,task_owner
from agent_execution.temporal_backend.tests.test_backend import isolated,submission
from agent_execution.temporal_backend.ledger import Ledger
from agent_execution.temporal_backend.store import Store,canonical
from agent_execution.temporal_backend.retention import prepare,select,candidates,preview,assert_not_erased,tables
from agent_execution.temporal_backend.erasure import remote,purge,ErasurePending
from agent_execution.temporal_backend.references import reference,blockers,process_stamp
from agent_execution.temporal_backend.admission import owner_key
from agent_execution.temporal_backend.config import Settings
from agent_execution.workspace_lifecycle import LifecycleStore
from agent_execution.workspace_lifecycle.store import session_key


@pytest.fixture
def task(isolated,monkeypatch):
    root = current_context().workspace
    monkeypatch.setenv('AGENT_APPROVAL_DB',str(root/'approvals.db'))
    store,row = submission(messages=[{'role':'user','content':'TP18 secret original input'}])
    row = store.claim(row['id'],Settings.load())
    ledger = Ledger(store,row['id'],row['fence'])
    ledger.run('read','tool',{'path':'owned/note.txt'},lambda:{'text':'TP18 secret tool result'})
    with closing(store.tasks.connect()) as conn,conn:
        conn.execute("UPDATE tasks SET state='completed',reply='TP18 secret final' WHERE owner=? AND id=?",(row['owner'],row['task_id']))
    store.finish(row['id'],row['fence'],'completed',{'reply':'TP18 secret final','messages':[]})
    with closing(store.tasks.connect()) as conn,conn:
        conn.execute('UPDATE temporal_submissions SET updated=? WHERE id=?',(time.time()-40*86400,row['id']))
    LifecycleStore().ensure_session()
    return store,store.get(row['id']),ledger


def fence(task,reason='retention'):
    store,row,_ = task
    with closing(store.tasks.connect()) as conn,conn:
        conn.execute('BEGIN IMMEDIATE')
        prepare(conn,row,reason,session_key() if reason=='session' else None)


def service(status=WorkflowExecutionStatus.COMPLETED,visible=False,unknown=False):
    absent = RPCError('not found',RPCStatusCode.NOT_FOUND,b'')
    handle = NS(describe=AsyncMock(side_effect=[NS(status=status,run_id='known-run'),absent]),
                cancel=AsyncMock(),terminate=AsyncMock())
    async def items(**kwargs):
        if visible:
            yield NS(id='still-visible')
    async def delete(*args,**kwargs):
        if unknown:
            raise RPCError('connection lost',RPCStatusCode.UNAVAILABLE,b'')
    client = NS(namespace=Settings.load().namespace,get_workflow_handle=Mock(return_value=handle),
        workflow_service=NS(describe_namespace=AsyncMock(return_value=NS(config=NS(
            workflow_execution_retention_ttl=NS(seconds=86400),history_archival_state=1,visibility_archival_state=1))),
            delete_workflow_execution=AsyncMock(side_effect=delete)),list_workflows=items)
    return client,handle


def complete_remote(task,**kwargs):
    client,handle = service(**kwargs)
    asyncio.run(remote(task[0],task[1]['id'],client))
    return client,handle


def test_dry_run_is_read_only_and_apply_rechecks_candidates(task):
    store,row,_ = task
    root = current_context().workspace
    (root/'owned').mkdir()
    (root/'owned/note.txt').write_text('keep user file')
    assert select(store,owner_key(row['owner']),time.time()-30*86400)==[row['id']]
    with closing(store.tasks.connect()) as conn:
        assert 'temporal_erasures' not in tables(conn)
    fence(task)
    complete_remote(task)
    purge(store,row['id'])
    assert (root/'owned/note.txt').read_text()=='keep user file'
    with closing(store.tasks.connect()) as conn:
        assert_not = lambda: assert_not_erased(conn,row['owner'],row['task_id'])
        with pytest.raises(PermissionError):
            assert_not()
        for name in tables(conn):
            if name.startswith('temporal_') or name in {'tasks','task_steps','operation_ledger'}:
                assert 'TP18 secret' not in repr([tuple(v) for v in conn.execute('SELECT * FROM '+name)])
        receipt = json.loads(conn.execute('SELECT receipt FROM temporal_erased_tasks').fetchone()[0])
        assert receipt['history']==receipt['visibility']=='not_found_verified'
        assert receipt['backups']==receipt['archival']=='external_policy_required'
    for run in [lambda:store.bind(row['owner'],row['task_id'],'temporal'),
                lambda:TaskStore().claim(row['owner'],row['task_id'],'different input',[])]:
        with pytest.raises(PermissionError):
            run()
    from agent_execution.temporal_backend.router import backend_for_task
    with pytest.raises(PermissionError):
        backend_for_task()
    with pytest.raises((PermissionError,KeyError)):
        store.finish(row['id'],row['fence'],'completed',{'reply':'late result'})
    with pytest.raises((PermissionError,KeyError)):
        task[2].run('read','tool',{'path':'owned/note.txt'},Mock())


@pytest.mark.parametrize('state',['running','uncertain','suspended'])
def test_retention_never_selects_active_unknown_or_waiting_tasks(task,state):
    store,row,_ = task
    with closing(store.tasks.connect()) as conn,conn:
        conn.execute('UPDATE temporal_submissions SET state=? WHERE id=?',(state,row['id']))
    assert select(store,owner_key(row['owner']),time.time()-30*86400,apply=True)==[]
    assert store.get(row['id'])['state']==state


@pytest.mark.parametrize('kind',['operation','publication','reference'])
def test_pending_replay_delivery_or_local_reference_protects_artifacts(task,kind):
    store,row,_ = task
    with closing(store.tasks.connect()) as conn,conn:
        if kind=='operation':
            conn.execute("UPDATE operation_ledger SET state='uncertain'")
        elif kind=='publication':
            from agent_execution.temporal_backend.publication import install
            install(conn)
            conn.execute("INSERT INTO temporal_finalizations VALUES(?,1,'pending')",(row['id'],))
        else:
            from agent_execution.temporal_backend.references import install
            install(conn)
            conn.execute('INSERT INTO temporal_local_references VALUES(?,?,?,?)',('active',row['id'],os.getpid(),process_stamp(os.getpid())))
    assert select(store,owner_key(row['owner']),time.time()-30*86400)==[]


def test_live_reference_blocks_purge_after_remote_workflow_closes(task):
    store,row,_ = task
    with reference(store,row):
        fence(task,'session')
        with pytest.raises(ErasurePending,match='reference'):
            complete_remote(task)
        with closing(store.tasks.connect()) as conn:
            assert conn.execute('SELECT count(*) FROM temporal_artifacts').fetchone()[0]>0
    complete_remote(task)
    purge(store,row['id'])


@pytest.mark.parametrize('fault',['unknown','visible','namespace'])
def test_unknown_remote_outcome_never_erases_local_data(task,fault):
    fence(task)
    client,_ = service(unknown=fault=='unknown',visible=fault=='visible')
    if fault=='namespace':
        client.namespace='another-namespace'
    with pytest.raises((ErasurePending,RPCError,PermissionError)):
        asyncio.run(remote(task[0],task[1]['id'],client))
    with pytest.raises(ErasurePending):
        purge(task[0],task[1]['id'])
    with closing(task[0].tasks.connect()) as conn:
        assert conn.execute('SELECT state FROM temporal_erasures').fetchone()[0]=='pending'
        assert conn.execute('SELECT count(*) FROM temporal_artifacts').fetchone()[0]>0


def test_unknown_delete_reply_recovers_by_query_without_repeating_business_io(task):
    fence(task)
    client,_ = service(unknown=True)
    with pytest.raises(RPCError):
        asyncio.run(remote(task[0],task[1]['id'],client))
    client,handle = service()
    handle.describe.side_effect=RPCError('gone',RPCStatusCode.NOT_FOUND,b'')
    asyncio.run(remote(task[0],task[1]['id'],client))
    client.workflow_service.delete_workflow_execution.assert_not_called()
    purge(task[0],task[1]['id'])


def test_retention_waits_for_remote_running_instead_of_terminating_it(task):
    fence(task)
    client,handle = service(status=WorkflowExecutionStatus.RUNNING)
    with pytest.raises(ErasurePending,match='closure'):
        asyncio.run(remote(task[0],task[1]['id'],client))
    handle.cancel.assert_not_called()
    handle.terminate.assert_not_called()


def test_frozen_session_preview_rejects_added_temporal_work(task):
    store,row,_ = task
    life = LifecycleStore()
    previewed = life.preview('delete_session')
    with closing(store.tasks.connect()) as conn,conn:
        conn.execute('UPDATE temporal_submissions SET updated=updated+1 WHERE id=?',(row['id'],))
    with pytest.raises(ValueError,match='Temporal targets changed'):
        life.confirm(previewed['plan_id'])
    with closing(life.connect()) as conn:
        assert conn.execute('SELECT state FROM wl_sessions').fetchone()[0]=='active'


def test_other_owner_and_nonexpired_rows_are_preserved(task):
    store,row,_ = task
    with execution_scope(session='another-session',task_id='other-task'):
        _,other = submission(store)
    fence(task)
    complete_remote(task)
    purge(store,row['id'])
    assert store.get(other['id'])['input']
    assert select(store,owner_key(other['owner']),time.time()-30*86400)==[]


@pytest.mark.parametrize('owner,before,limit',[('*',1,1),('f'*64,float('nan'),1),('f'*64,1,0),('f'*64,1,101)])
def test_invalid_or_unbounded_retention_is_rejected(task,owner,before,limit):
    with closing(task[0].tasks.connect()) as conn,pytest.raises(ValueError):
        candidates(conn,owner,before,limit)


def test_tombstone_capacity_never_evicts_existing_fences(task,monkeypatch):
    fence(task)
    with execution_scope(task_id='another-task'):
        _,row = submission(task[0])
    monkeypatch.setenv('AGENT_TEMPORAL_OPERATION_LIMIT','1')
    from agent_execution.delivery import CapacityError
    with closing(task[0].tasks.connect()) as conn,conn,pytest.raises(CapacityError):
        prepare(conn,row,'retention')
    with closing(task[0].tasks.connect()) as conn:
        with pytest.raises(PermissionError):
            assert_not_erased(conn,task[1]['owner'],task[1]['task_id'])


def test_actual_process_exit_does_not_leave_a_permanent_gc_reference(task):
    store,row,_ = task
    code = """import os
from agent_execution.temporal_backend.store import Store
from agent_execution.temporal_backend.references import reference
store=Store()
with reference(store,store.get(KEY)):
    os._exit(71)
""".replace('KEY',repr(row['id']))
    result = subprocess.run([sys.executable,'-c',code],capture_output=True,timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    assert result.returncode==71,result.stderr
    assert select(store,owner_key(row['owner']),time.time()-30*86400)==[row['id']]
    fence(task)
    complete_remote(task)
    purge(store,row['id'])




def test_stale_cached_result_and_relay_cannot_publish_after_erasure_fence(task):
    from agent_execution.temporal_backend.backend import restore_result
    from agent_execution.temporal_backend.publication import runtime_scope,relay
    messages = [{'role':'user','content':'current input'}]
    callback = Mock()
    def fetched(*args):
        fence(task)
        return [{'kind':'model.delta','payload':json.dumps({'text':'TP18 secret'}),'seq':1}]
    with runtime_scope(NS(event_callback=callback),None),patch(
            'agent_execution.temporal_backend.publication.events',side_effect=fetched):
        with pytest.raises(PermissionError):
            relay(task[0],task[1])
    callback.assert_not_called()
    with pytest.raises(PermissionError):
        restore_result(task[1],messages)
    assert messages==[{'role':'user','content':'current input'}]


def test_late_profile_preparation_cannot_reinsert_erased_results(task,monkeypatch):
    from agent_execution.temporal_backend.runtime_state import prepare_profile
    monkeypatch.setenv('AGENT_EXECUTION_BACKEND','temporal')
    def extracted(*args):
        fence(task)
        return None,'TP18 secret late profile result'
    with patch('agent_execution.user_profile.prepare_turn',side_effect=extracted),pytest.raises(PermissionError):
        prepare_profile('profile input',None)
    with closing(task[0].tasks.connect()) as conn:
        assert conn.execute('SELECT result FROM temporal_profile_inputs').fetchone()[0] is None


def test_session_erasure_requires_committed_lifecycle_tombstone(task):
    from agent_execution.temporal_backend.retention import prepare_session
    with closing(task[0].tasks.connect()) as conn,conn,pytest.raises(PermissionError):
        prepare_session(conn,session_key())
    assert task[0].get(task[1]['id'])['state']=='completed'


def seed_rich_payloads(task):
    store,row,_ = task
    from agent_execution.approvals import ApprovalStore
    from agent_execution.temporal_backend.approval_outbox import binding_data
    from agent_execution.temporal_backend.approval_gate import ApprovalGate
    from agent_execution.temporal_backend.step_store import StepStore
    from agent_execution.temporal_backend.publication import install as events_schema
    from agent_execution.temporal_backend.file_writes import install as writes_schema
    from agent_execution.temporal_backend.contracts import APPROVAL_WORKFLOW_NAME
    approvals = ApprovalStore()
    StepStore(store)
    ApprovalGate(store)
    binding = {'submission_id':row['id'],'queue':store.queue_for('approval'),'operation_id':'tool:write',
               'owner':row['owner'],'task_id':row['task_id']}
    key = approvals.request('local',current_context().session,'write-approval',
                            {'content':'TP18 secret approved file content'},binding=binding)
    with closing(approvals.connect()) as conn,conn:
        conn.execute("INSERT INTO temporal_approval_outbox VALUES('outbox',?,'approved','pending',?)",(key,time.time()))
    context = json.loads(row['context'])
    context['approval_db']=str(approvals.path.resolve())
    with closing(store.tasks.connect()) as conn,conn:
        conn.execute('UPDATE temporal_submissions SET context=?,queue=? WHERE id=?',(canonical(context),binding['queue'],row['id']))
        conn.execute('UPDATE temporal_protocols SET workflow_type=?,version=3 WHERE submission_id=?',(APPROVAL_WORKFLOW_NAME,row['id']))
        events_schema(conn)
        writes_schema(conn)
        conn.execute("INSERT INTO temporal_events VALUES(?,1,'event','tool.result',?,'sent')",(row['id'],'TP18 secret event payload'))
        conn.execute("INSERT INTO temporal_file_writes VALUES(?, 'write','call','write_file','digest',?,NULL,?)",
                     (row['id'],'TP18 secret private plan',time.time()))
        conn.execute("INSERT INTO temporal_human_answers VALUES(?,1,'call',? ,?,'consumed','sent')",
                     (row['id'],time.time(),'TP18 secret human answer'))
    return approvals,key


def test_payload_and_approval_outbox_erasure_are_owner_scoped_and_restartable(task):
    approvals,key = seed_rich_payloads(task)
    other = approvals.request('someone-else','other-session','unrelated',{'content':'preserve other user'})
    fence(task)
    complete_remote(task)
    from agent_execution.temporal_backend import erasure
    erase = erasure.erase_approvals
    def lost(row):
        erase(row)
        raise OSError('Simulated crash after approval DB commit')
    with patch.object(erasure,'erase_approvals',side_effect=lost),pytest.raises(OSError):
        purge(task[0],task[1]['id'])
    assert task[0].get(task[1]['id'])['state']=='erasing'
    purge(Store(),task[1]['id'])
    with closing(task[0].tasks.connect()) as conn:
        for name in tables(conn):
            if name.startswith('temporal_') or name in {'tasks','task_steps','operation_ledger'}:
                assert 'TP18 secret' not in repr([tuple(v) for v in conn.execute('SELECT * FROM '+name)])
    with closing(approvals.connect()) as conn:
        assert conn.execute('SELECT 1 FROM approvals WHERE id=?',(key,)).fetchone() is None
        assert conn.execute('SELECT count(*) FROM temporal_approval_bindings').fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM temporal_approval_outbox').fetchone()[0]==0
        assert 'preserve other user' in conn.execute('SELECT arguments FROM approvals WHERE id=?',(other,)).fetchone()[0]


def test_changed_approval_database_blocks_cleanup_without_touching_other_data(task,monkeypatch):
    seed_rich_payloads(task)
    fence(task)
    complete_remote(task)
    monkeypatch.setenv('AGENT_APPROVAL_DB',str(current_context().workspace/'different-approvals.db'))
    with pytest.raises(PermissionError,match='Approval database changed'):
        purge(task[0],task[1]['id'])
    assert task[0].get(task[1]['id'])['input']




def test_previously_confirmed_deletion_is_queued_without_reviving_it(task):
    from agent_execution.temporal_backend.retention import queue_deleted_sessions
    life = LifecycleStore()
    plan = life.preview('delete_session')
    key = plan['plan_id']
    with closing(life.connect()) as conn,conn:
        conn.execute("INSERT INTO wl_operations VALUES(?,?,'completed','',?)",(key,key,time.time()))
        conn.execute("INSERT INTO wl_outbox VALUES(?,?,'completed')",(session_key(),json.dumps({'plan_id':key})))
        conn.execute("UPDATE wl_sessions SET state='deleted'")
        queue_deleted_sessions(conn)
        assert conn.execute('SELECT reason FROM temporal_erasures').fetchone()[0]=='deleted_session'
        assert conn.execute('SELECT state FROM temporal_submissions').fetchone()[0]=='erasing'
    client,_ = service()
    with patch('agent_execution.temporal_backend.client.connect',AsyncMock(return_value=client)):
        from agent_execution.temporal_backend.erasure import reconcile
        assert asyncio.run(reconcile(Store()))==1
    with closing(life.connect()) as conn:
        assert conn.execute('SELECT state FROM wl_sessions').fetchone()[0]=='deleted'
        assert conn.execute('SELECT count(*) FROM temporal_submissions').fetchone()[0]==0


def test_deletion_without_an_authorized_completed_plan_is_not_auto_queued(task):
    from agent_execution.temporal_backend.retention import queue_deleted_sessions
    with closing(task[0].tasks.connect()) as conn,conn:
        conn.execute("UPDATE wl_sessions SET state='deleted'")
        conn.execute("INSERT INTO wl_outbox VALUES(?,?,'completed')",(session_key(),json.dumps({'plan_id':'unknown'})))
        queue_deleted_sessions(conn)
        assert 'temporal_erasures' not in tables(conn)
    assert task[0].get(task[1]['id'])['state']=='completed'




@pytest.mark.parametrize('delivery',[None,{'kind':'websocket','path':'unused','owner':'unused','ref':'unused'}])
def test_only_unfinished_reliable_delivery_blocks_expired_event_gc(task,delivery):
    store,row,_ = task
    from agent_execution.temporal_backend.publication import install
    context = json.loads(row['context'])
    context['publication']={'delivery':delivery}
    with closing(store.tasks.connect()) as conn,conn:
        install(conn)
        conn.execute('UPDATE temporal_submissions SET context=? WHERE id=?',(canonical(context),row['id']))
        conn.execute("INSERT INTO temporal_events VALUES(?,1,'event','end','{}','pending')",(row['id'],))
    expected = [row['id']] if delivery is None else []
    assert select(store,owner_key(row['owner']),time.time()-30*86400)==expected


def test_completed_approved_operation_is_eligible_but_unconsumed_wait_is_not(task):
    store,row,_ = task
    from agent_execution.temporal_backend.approval_gate import ApprovalGate
    ApprovalGate(store)
    with closing(store.tasks.connect()) as conn,conn:
        conn.execute("INSERT INTO temporal_approval_waits VALUES(?,1,'read','call','read_file','{}','digest','{}','action',?,NULL,'approved')",
                     (row['id'],time.time()+30))
    assert select(store,owner_key(row['owner']),time.time()-30*86400)==[row['id']]
    with closing(store.tasks.connect()) as conn,conn:
        conn.execute("UPDATE temporal_approval_waits SET operation_id='not-started'")
    assert select(store,owner_key(row['owner']),time.time()-30*86400)==[]


def test_changed_workflow_target_never_reaches_remote_service(task):
    fence(task)
    with closing(task[0].tasks.connect()) as conn,conn:
        conn.execute("UPDATE temporal_submissions SET workflow_id='another-workflow' WHERE id=?",(task[1]['id'],))
    client,_ = service()
    with pytest.raises(PermissionError,match='Untrusted'):
        asyncio.run(remote(task[0],task[1]['id'],client))
    client.workflow_service.describe_namespace.assert_not_called()
    client.get_workflow_handle.assert_not_called()


def test_reference_capacity_is_bounded_without_releasing_live_reference(task,monkeypatch):
    from agent_execution.delivery import CapacityError
    monkeypatch.setenv('AGENT_TEMPORAL_OPERATION_LIMIT','1')
    with reference(task[0],task[1]):
        with pytest.raises(CapacityError):
            with reference(task[0],task[1]):
                pass
        with closing(task[0].tasks.connect()) as conn:
            assert blockers(conn,task[1]['id'])




def test_invalid_remote_receipt_cannot_authorize_file_or_artifact_cleanup(task):
    from agent_execution.temporal_backend.erasure import ensure_session
    fence(task,'session')
    with closing(task[0].tasks.connect()) as conn,conn:
        conn.execute("UPDATE temporal_erasures SET state='remote_done',receipt='{}'")
    with pytest.raises(ErasurePending,match='receipt'):
        ensure_session(task[0],session_key())
    with pytest.raises(ErasurePending,match='receipt'):
        purge(task[0],task[1]['id'])
    with closing(task[0].tasks.connect()) as conn:
        assert conn.execute('SELECT count(*) FROM temporal_artifacts').fetchone()[0]>0


def test_cli_dry_run_does_not_install_optional_tables(task):
    store,row,_ = task
    result = subprocess.run([sys.executable,'-m','agent_execution.temporal_backend.retention_cli',
        '--owner-hash',owner_key(row['owner']),'--days','30'],capture_output=True,text=True,timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)=={'dry_run':True,'candidates':[row['id']],'pending':[]}
    with closing(store.tasks.connect()) as conn:
        assert 'temporal_erasures' not in tables(conn)
