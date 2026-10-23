import asyncio
from contextlib import closing
import json
from unittest.mock import patch,AsyncMock,Mock
from types import SimpleNamespace as NS

import pytest
from agent_execution import execution_scope
from agent_execution.policy import current_context
from agent_execution.tasks import task_owner
from permissions import set_current_role
from agent_execution.temporal_backend.tests.test_backend import isolated
from agent_execution.temporal_backend.schedules import Schedules
from agent_execution.temporal_backend.store import canonical
from agent_execution.temporal_backend.contracts import READ_TOOLS
from agent_execution.temporal_backend.config import Settings

@pytest.fixture
def schedules(isolated,monkeypatch):
    monkeypatch.setenv('SCHEDULER_DB_PATH',str(current_context().workspace/'scheduler.db'))
    monkeypatch.setenv('AGENT_APPROVAL_DB',str(current_context().workspace/'approvals.db'))
    monkeypatch.delenv('AGENT_TEMPORAL_TRIAL_OWNERS',raising=False)
    return Schedules()

def create(schedules,**values):
    return schedules.create('schedule','*/5 * * * *','watch',{'prompt':'private prompt'},**values)

def occurrence(schedules,key,number='1'):
    return schedules.remote_id(schedules.get(key))+'-'+number

def finish_task(schedules,prepared,reply='{"state":"waiting","summary":"quiet"}'):
    row = schedules.tasks.get(prepared['submission_id'])
    row = schedules.tasks.claim(row['id'],Settings.load())
    with closing(schedules.tasks.tasks.connect()) as conn,conn:
        conn.execute("UPDATE tasks SET state='completed' WHERE owner=? AND id=?",(row['owner'],row['task_id']))
    schedules.tasks.finish(row['id'],row['fence'],'completed',{'reply':reply,'messages':[]})

def test_default_paused_and_legacy_engine_never_claims(schedules):
    key = create(schedules)
    row = schedules.get(key)
    assert row['timezone']=='Asia/Taipei' and row['execution_backend']=='temporal' and not row['is_enabled']
    schedules.change(key,'resume')
    with closing(schedules.scheduler.connect()) as conn,conn:
        conn.execute("UPDATE scheduled_jobs SET next_run_at='2000-01-01' WHERE id=?",(key,))
    assert schedules.scheduler.get_due_jobs()==[]
    assert schedules.scheduler.claim_due(schedules.get(key),'legacy') is None
    native = schedules.definition(schedules.get(key))
    assert native.policy.catchup_window.total_seconds()==10
    assert native.policy.overlap.name=='SKIP'

def test_owner_scope_and_roles(schedules):
    key = create(schedules)
    with execution_scope(session='other'):
        assert schedules.list()==[]
        with pytest.raises(KeyError):schedules.change(key,'cancel')
    with set_current_role('viewer'):
        with pytest.raises(PermissionError):schedules.change(key,'pause')
        with pytest.raises(PermissionError):create(schedules)

@pytest.mark.parametrize('values',[{'overlap':'ALLOW_ALL'},{'overlap':'BUFFER_ONE'},{'max_runs':0},
    {'timeout':301},{'timezone':'No/SuchZone'},{'paused':1}])
def test_invalid_contracts_rejected(schedules,values):
    with pytest.raises((ValueError,KeyError)):
        create(schedules,**values)

def test_updates_pause_resume_and_irreversible_cancel(schedules):
    key = create(schedules)
    schedules.change(key,'update',timezone='UTC',cron='0 9 * * *',max_runs=2)
    assert schedules.get(key)['timezone']=='UTC'
    schedules.change(key,'resume')
    assert schedules.get(key)['is_enabled']
    schedules.change(key,'pause')
    assert not schedules.get(key)['is_enabled']
    schedules.change(key,'cancel')
    schedules.change(key,'cancel')
    with pytest.raises(PermissionError):schedules.change(key,'resume')

def test_unique_occurrence_counts_once_and_retry_recovers(schedules):
    key = create(schedules,max_runs=2,paused=False)
    first = schedules.prepare(key,occurrence(schedules,key))
    again = schedules.prepare(key,occurrence(schedules,key))
    assert first==again
    assert schedules.get(key)['run_count']==1
    row = schedules.tasks.get(first['submission_id'])
    assert json.loads(row['context'])['allowed_tools']==sorted(READ_TOOLS)
    assert not json.loads(row['context']).get('file_writes')
    assert schedules.poll(key,first['occurrence_id'])=={'done':False}
    finish_task(schedules,first)
    assert schedules.poll(key,first['occurrence_id'])=={'done':True}
    assert schedules.scheduler.notifications()==[]
    second = schedules.prepare(key,occurrence(schedules,key,'2'))
    assert schedules.get(key)['run_count']==2 and not schedules.get(key)['is_enabled']
    finish_task(schedules,second)
    schedules.poll(key,second['occurrence_id'])
    notices=schedules.scheduler.notifications()
    assert len(notices)==1 and notices[0]['state']=='needs_input'
    assert 'limit' in notices[0]['output']
    assert schedules.prepare(key,occurrence(schedules,key,'3'))=={'done':True}
    with pytest.raises(ValueError):schedules.change(key,'resume')

def test_crash_between_reserve_and_submit_does_not_lose_run(schedules):
    key = create(schedules,paused=False,max_runs=1)
    with patch.object(schedules.tasks,'submit',side_effect=RuntimeError('crash')):
        with pytest.raises(RuntimeError):schedules.prepare(key,occurrence(schedules,key))
    prepared = schedules.prepare(key,occurrence(schedules,key))
    assert prepared['submission_id'] and schedules.get(key)['run_count']==1

def test_cancel_stops_already_submitted_task(schedules):
    key = create(schedules,paused=False)
    prepared=schedules.prepare(key,occurrence(schedules,key))
    schedules.change(key,'cancel')
    assert schedules.tasks.get(prepared['submission_id'])['state']=='cancelled'
    with pytest.raises(PermissionError):schedules.prepare(key,occurrence(schedules,key,'2'))

def test_pause_does_not_cancel_existing_occurrence(schedules):
    key=create(schedules,paused=False)
    prepared=schedules.prepare(key,occurrence(schedules,key))
    schedules.change(key,'pause')
    assert schedules.poll(key,prepared['occurrence_id'])=={'done':False}
    assert schedules.prepare(key,occurrence(schedules,key,'2'))=={'done':True}

def test_tombstone_and_trial_stop_new_occurrences(schedules,monkeypatch):
    key=create(schedules,paused=False)
    monkeypatch.setenv('AGENT_TEMPORAL_TRIAL_OWNERS','[]')
    with pytest.raises(PermissionError):schedules.prepare(key,occurrence(schedules,key))
    monkeypatch.delenv('AGENT_TEMPORAL_TRIAL_OWNERS')
    with closing(schedules.tasks.tasks.connect()) as conn,conn:
        conn.execute("UPDATE wl_sessions SET state='deleting'")
    with pytest.raises(PermissionError):schedules.prepare(key,occurrence(schedules,key))
    assert schedules.get(key)['run_count']==0

def test_revoked_remote_owner_cancels_active_task(schedules,monkeypatch):
    roles=current_context().workspace/'roles.json'
    roles.write_text(canonical({'telegram:123':'admin'}),encoding='utf-8')
    monkeypatch.setenv('ROLES_CONFIG',str(roles))
    with execution_scope(source='communication',identity='telegram:123',session='telegram:456'):
        key=create(schedules,paused=False)
        prepared=schedules.prepare(key,occurrence(schedules,key))
        roles.write_text('{}',encoding='utf-8')
        assert schedules.poll(key,prepared['occurrence_id'])=={'done':True}
        assert schedules.tasks.get(prepared['submission_id'])['state']=='cancelled'
        assert schedules.scheduler.notifications()[0]['state']=='needs_input'

def test_wrong_workflow_and_workspace_rejected(schedules,monkeypatch):
    key=create(schedules,paused=False)
    with pytest.raises(PermissionError):schedules.prepare(key,'unrelated')
    monkeypatch.setenv('AGENT_WORKSPACE_ROOT',str(current_context().workspace/'other'))
    with pytest.raises(PermissionError):schedules.prepare(key,occurrence(schedules,key))

def test_snapshot_includes_paused_native_and_configuration(schedules):
    from agent_execution.workspace_lifecycle.erasure import schedules as snapshot
    key=create(schedules)
    owners=[task_owner(current_context())]
    before=snapshot(owners)
    assert [r['id'] for r in before]==[key]
    schedules.change(key,'update',overlap='SKIP',cron='0 9 * * *')
    assert snapshot(owners)!=before

def test_sqlite_jobs_keep_original_backend(schedules):
    key=schedules.scheduler.add_job('legacy','* * * * *','watch',{'prompt':'unchanged'})
    assert next(r for r in schedules.scheduler.list_jobs() if r['id']==key)['execution_backend']=='sqlite'
    assert schedules.list()==[]


def test_original_tool_ceiling_is_preserved(schedules):
    with execution_scope(allowed_tools=frozenset({'schedule_job','read_file'})):
        key=create(schedules,paused=False)
    prepared=schedules.prepare(key,occurrence(schedules,key))
    assert json.loads(schedules.tasks.get(prepared['submission_id'])['context'])['allowed_tools']==['read_file']

def test_schedule_rpc_lock_is_not_expired_while_process_is_alive(schedules):
    key=create(schedules)
    with schedules.rpc_lock(key):
        with pytest.raises(RuntimeError):
            with schedules.rpc_lock(key):
                pass
    with schedules.rpc_lock(key):
        pass

def test_scheduler_database_relocation_refuses_erasure(schedules,monkeypatch):
    from agent_execution.temporal_backend.schedule_erasure import keys_for_session
    from agent_execution.workspace_lifecycle.store import session_key
    create(schedules)
    monkeypatch.setenv('SCHEDULER_DB_PATH',str(current_context().workspace/'moved.db'))
    with pytest.raises(PermissionError):keys_for_session(schedules.tasks,session_key())
    with pytest.raises(PermissionError):Schedules()

def test_schedule_deadline_is_pinned_before_submission_and_not_extended(schedules):
    import time
    key=create(schedules,paused=False,timeout=20)
    before=time.time()
    prepared=schedules.prepare(key,occurrence(schedules,key))
    contract=schedules.tasks.verify_budget_contract(schedules.tasks.get(prepared['submission_id']))
    assert before<contract['deadline']<=before+21
    assert schedules.prepare(key,occurrence(schedules,key))==prepared
    assert schedules.tasks.verify_budget_contract(schedules.tasks.get(prepared['submission_id']))==contract

def test_disable_legacy_tool_sets_native_pause_intent(schedules):
    key=create(schedules,paused=False)
    before=schedules.get(key)['revision']
    assert schedules.scheduler.disable(key)
    row=schedules.get(key)
    assert not row['is_enabled'] and row['revision']==before+1

@pytest.mark.parametrize('kind,payload',[('command',{'command':['whoami']}),('skill',{'skill':'example'}),
    ('agent',{'prompt':'task','file_writes':True}),('watch',{'prompt':''})])
def test_unsupported_executors_and_payload_authority_rejected(schedules,kind,payload):
    with pytest.raises(ValueError):
        schedules.create('invalid','* * * * *',kind,payload)

def test_cli_zero_limit_is_rejected_without_rpc(schedules):
    from agent_execution.temporal_backend.schedule_cli import main
    with patch('dotenv.load_dotenv'):
        with pytest.raises(ValueError):
            main(['create','--name','invalid','--cron','* * * * *','--prompt','task','--max-runs','0'])

def test_session_cleanup_stays_frozen_when_schedule_service_is_offline(schedules):
    from agent_execution.workspace_lifecycle import LifecycleStore
    life=LifecycleStore()
    life.create('owned','temporary')
    key=create(schedules)
    preview=life.preview('delete_session')
    with patch('agent_execution.temporal_backend.schedules.Schedules.sync',new=AsyncMock(side_effect=TimeoutError('offline'))):
        result=life.confirm(preview['plan_id'])
    assert result['state']=='blocked' and (current_context().workspace/'owned').exists()
    assert schedules.get(key)['cancelled']

def test_retention_waits_for_schedule_notification_projection(schedules):
    from agent_execution.temporal_backend.schedules import task_pending
    key=create(schedules,paused=False)
    prepared=schedules.prepare(key,occurrence(schedules,key))
    finish_task(schedules,prepared)
    with closing(schedules.tasks.tasks.connect()) as conn:
        assert task_pending(conn,prepared['submission_id'])
    schedules.poll(key,prepared['occurrence_id'])
    with closing(schedules.tasks.tasks.connect()) as conn:
        assert not task_pending(conn,prepared['submission_id'])

def test_remote_schedule_action_tampering_is_rejected(schedules):
    key=create(schedules)
    from temporalio.client import ScheduleAlreadyRunningError
    client=NS(namespace=Settings.load().namespace,create_schedule=AsyncMock(side_effect=ScheduleAlreadyRunningError()))
    async def update(updater,**kwargs):
        updater(NS(description=NS(schedule=NS(action=NS(workflow='untrusted',task_queue=schedules.queue,id=schedules.remote_id(schedules.get(key)))))))
    client.get_schedule_handle=Mock(return_value=NS(update=update))
    with pytest.raises(PermissionError):
        asyncio.run(schedules.sync(key,client))
    assert schedules.get(key)['applied']==0

def test_schedule_erasure_requires_committed_session_tombstone(schedules):
    from agent_execution.temporal_backend.schedule_erasure import erase
    from agent_execution.workspace_lifecycle.store import session_key
    create(schedules)
    with pytest.raises(PermissionError):
        asyncio.run(erase(schedules.tasks,session_key()))

def test_crud_rpc_does_not_starve_metadata_but_cancel_waits_for_it(schedules):
    key=create(schedules,paused=False)
    with schedules.rpc_lock(key):
        prepared=schedules.prepare(key,occurrence(schedules,key))
    assert prepared['submission_id']
    schedules.change(key,'cancel')
    with schedules.rpc_lock(key,'metadata'):
        with pytest.raises(RuntimeError):
            asyncio.run(schedules.sync(key,NS(namespace=Settings.load().namespace)))

@pytest.mark.parametrize('delegation',[{'coordinator_owner':'root-task-owner'},{'lifecycle_owner':'parent-lifecycle-owner'}])
def test_delegated_context_cannot_create_persistent_schedule(schedules,delegation):
    with execution_scope(**delegation):
        with pytest.raises(PermissionError):
            create(schedules)
    assert schedules.list()==[]
    assert schedules.scheduler.list_jobs()==[]
