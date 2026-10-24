import asyncio
from contextlib import closing
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,Mock,patch
import pytest

from agent_execution import execution_scope
from agent_execution.policy import current_context
from agent_execution.temporal_backend.tests.test_backend import isolated
from agent_execution.temporal_backend.tests.test_schedules import finish_task
from agent_execution.temporal_backend.schedules import Schedules
from agent_execution.temporal_backend.schedule_migration import (
    Migration,preview,times,slot,utc,migration,admit_native,admit_legacy)
from agent_execution.temporal_backend.store import canonical
from permissions import set_current_role

@pytest.fixture
def setup(isolated,monkeypatch):
    root=current_context().workspace
    monkeypatch.setenv('SCHEDULER_DB_PATH',str(root/'scheduler.db'))
    monkeypatch.setenv('AGENT_APPROVAL_DB',str(root/'approvals.db'))
    monkeypatch.delenv('AGENT_TEMPORAL_TRIAL_OWNERS',raising=False)
    s=Schedules()
    key=s.scheduler.add_job('legacy','*/5 * * * *','watch',{'prompt':'private original prompt'},timezone='UTC',max_runs=8)
    return s,Migration(s),key

def saved(setup):
    s,m,key=setup
    with closing(s.scheduler.connect()) as conn:
        return dict(migration(conn,key))

def job(setup):
    s,m,key=setup
    with closing(s.scheduler.connect()) as conn:
        return dict(conn.execute('SELECT * FROM scheduled_jobs WHERE id=?',(key,)).fetchone())

def stage(setup):
    s,m,key=setup
    m.stage(key,preview(key)['source_digest'])

def mock_service(setup,monkeypatch):
    s,m,key=setup
    async def sync(key,client=None):
        with closing(s.scheduler.connect()) as conn,conn:
            row=s._row(key,conn)
            conn.execute('UPDATE temporal_schedule_bindings SET applied=revision,remote_deleted=? WHERE job_id=?',
                (int(bool(row['retired'] or row['cancelled'])),key))
    monkeypatch.setattr(s,'sync',sync)
    monkeypatch.setattr(m,'verify_times',AsyncMock(return_value=[]))
    client=NS(namespace='pythonai-dev',get_schedule_handle=Mock(return_value=NS(
        describe=AsyncMock(return_value=NS(info=NS(running_actions=[]))))))
    monkeypatch.setattr(m,'closed',AsyncMock())
    return client

def active(setup,monkeypatch):
    s,m,key=setup
    stage(setup)
    client=mock_service(setup,monkeypatch)
    m.cutover(key)
    asyncio.run(m.apply(key,client))
    assert saved(setup)['state']=='temporal'
    return client

def native_id(setup,when=None):
    s,m,key=setup
    when=when or times(job(setup)['cron_expression'],job(setup)['timezone'],saved(setup)['barrier'],1)[0]
    return s.remote_id(s.get(key))+'-'+when.replace('+00:00','Z')

def test_preview_is_read_only_and_sdk_free(setup):
    s,m,key=setup
    before={p:p.read_bytes() for p in [s.scheduler.db_path,s.tasks.path]}
    import builtins
    original=builtins.__import__
    def guarded(name,*args,**kwargs):
        if name.startswith('temporalio'):raise AssertionError('preview imported SDK')
        return original(name,*args,**kwargs)
    with patch('builtins.__import__',side_effect=guarded):
        value=preview(key)
    assert value['timezone']=='UTC' and len(value['next_10'])==10 and value['backfill']=='disabled'
    assert all(p.read_bytes()==content for p,content in before.items())
    assert 'private original prompt' not in canonical(value)

@pytest.mark.parametrize('zone,expression,base,expected',[
    ('America/New_York','30 2 * * *','2026-03-07T00:00:00Z',
        ['2026-03-07T07:30:00+00:00','2026-03-09T06:30:00+00:00','2026-03-10T06:30:00+00:00']),
    ('America/New_York','30 1 * * *','2026-11-01T00:00:00Z',
        ['2026-11-01T05:30:00+00:00','2026-11-01T06:30:00+00:00','2026-11-02T06:30:00+00:00']),
    ('Asia/Taipei','0 9 * * *','2026-10-24T00:00:00Z',
        ['2026-10-24T01:00:00+00:00','2026-10-25T01:00:00+00:00','2026-10-26T01:00:00+00:00'])])
def test_dst_gap_fold_and_taipei_are_distinct_utc_slots(zone,expression,base,expected):
    assert times(expression,zone,base,3)==expected

def test_staged_schedule_remains_legacy_until_cutover(setup):
    s,m,key=setup
    stage(setup)
    assert saved(setup)['state']=='staged'
    assert job(setup)['is_enabled']==1 and not s.get(key)['is_enabled']
    assert s.prepare(key,native_id(setup))=={'done':True}
    assert job(setup)['run_count']==0

def test_cutover_fences_old_cached_claim_and_stale_native_tick(setup,monkeypatch):
    s,m,key=setup
    old=job(setup)
    stage(setup)
    m.cutover(key)
    assert job(setup)['execution_backend']=='transition' and not job(setup)['is_enabled']
    assert s.scheduler.claim_due(old,'old-worker') is None
    assert s.prepare(key,native_id(setup))=={'done':True}
    client=mock_service(setup,monkeypatch)
    asyncio.run(m.apply(key,client))
    assert job(setup)['execution_backend']=='temporal' and not job(setup)['is_enabled']
    assert s.get(key)['is_enabled']
    stale=utc(saved(setup)['barrier'])-timedelta(minutes=5)
    assert s.prepare(key,native_id(setup,stale.isoformat()))=={'done':True}
    with closing(s.scheduler.connect()) as conn:
        assert conn.execute('SELECT id FROM scheduled_jobs WHERE is_enabled=1').fetchall()==[]

def test_metadata_retry_keeps_one_fence_task_and_count(setup,monkeypatch):
    s,m,key=setup
    active(setup,monkeypatch)
    first=s.prepare(key,native_id(setup))
    again=s.prepare(key,native_id(setup))
    assert first==again and job(setup)['run_count']==1
    with closing(s.scheduler.connect()) as conn:
        assert conn.execute('SELECT count(*) FROM schedule_occurrence_fences').fetchone()[0]==1

def test_fence_normalizes_equal_utc_instants(setup,monkeypatch):
    s,m,key=setup
    active(setup,monkeypatch)
    first=s.prepare(key,native_id(setup))
    when=times(job(setup)['cron_expression'],'UTC',saved(setup)['barrier'],1)[0]
    alternative=s.remote_id(s.get(key))+'-'+when
    assert s.prepare(key,alternative)=={'done':True}
    assert first['submission_id'] and job(setup)['run_count']==1

@pytest.mark.parametrize('state',['running','uncertain'])
def test_active_or_unknown_legacy_run_cannot_stage(setup,state):
    s,m,key=setup
    with closing(s.scheduler.connect()) as conn,conn:
        conn.execute('INSERT INTO scheduled_job_runs(id,job_id,status) VALUES(?,?,?)',('old',key,state))
    with pytest.raises(RuntimeError):preview(key)
    assert job(setup)['execution_backend']=='sqlite'

def test_live_lease_blocks_stage_without_claiming_a_slot(setup):
    s,m,key=setup
    assert s.scheduler.acquire_lock(key,'other')
    with pytest.raises(RuntimeError):preview(key)
    with closing(s.scheduler.connect()) as conn:
        assert conn.execute('SELECT count(*) FROM schedule_occurrence_fences').fetchone()[0]==0

@pytest.mark.parametrize('field,value',[('timezone','Asia/Taipei'),('run_count',1),('next_run_at','2030-01-01T00:00:00+00:00')])
def test_changed_source_requires_new_preview(setup,field,value):
    s,m,key=setup
    plan=preview(key)
    with closing(s.scheduler.connect()) as conn,conn:
        conn.execute('UPDATE scheduled_jobs SET '+field+'=? WHERE id=?',(value,key))
    with pytest.raises(ValueError):m.stage(key,plan['source_digest'])

def test_legacy_progress_after_stage_requires_restage(setup):
    s,m,key=setup
    stage(setup)
    with closing(s.scheduler.connect()) as conn,conn:
        conn.execute('UPDATE scheduled_jobs SET run_count=1 WHERE id=?',(key,))
    with pytest.raises(ValueError):m.cutover(key)
    m.stage(key,preview(key)['source_digest'])
    m.cutover(key)
    assert saved(setup)['state']=='cutting_over'

def test_other_owner_and_viewer_cannot_transition(setup):
    s,m,key=setup
    plan=preview(key)
    stage(setup)
    with execution_scope(session='other'):
        for action in (lambda:preview(key),lambda:m.stage(key,plan['source_digest']),lambda:m.cutover(key),lambda:m.rollback(key)):
            with pytest.raises(PermissionError):action()
    with set_current_role('viewer'):
        with pytest.raises(PermissionError):m.rollback(key)
    assert saved(setup)['state']=='staged'

def test_offline_cutover_stays_fenced_and_retries_same_intent(setup,monkeypatch):
    s,m,key=setup
    stage(setup)
    m.cutover(key)
    monkeypatch.setattr(s,'sync',AsyncMock(side_effect=TimeoutError()))
    with pytest.raises(TimeoutError):asyncio.run(m.apply(key,NS()))
    m.cutover(key)
    assert saved(setup)['state']=='cutting_over' and not job(setup)['is_enabled']
    client=mock_service(setup,monkeypatch)
    asyncio.run(m.apply(key,client))
    assert saved(setup)['state']=='temporal'

def test_calendar_mismatch_never_unpauses(setup,monkeypatch):
    s,m,key=setup
    stage(setup)
    m.cutover(key)
    client=mock_service(setup,monkeypatch)
    monkeypatch.setattr(m,'verify_times',AsyncMock(side_effect=RuntimeError('calendar mismatch')))
    with pytest.raises(RuntimeError):asyncio.run(m.apply(key,client))
    assert saved(setup)['state']=='cutting_over' and not s.get(key)['is_enabled']

def test_rollback_keeps_count_budget_and_utc_and_skips_past_slots(setup,monkeypatch):
    s,m,key=setup
    with closing(s.scheduler.connect()) as conn,conn:
        conn.execute('UPDATE scheduled_jobs SET run_count=2 WHERE id=?',(key,))
    client=active(setup,monkeypatch)
    first=s.prepare(key,native_id(setup))
    finish_task(s,first)
    s.poll(key,first['occurrence_id'])
    row=s.tasks.get(first['submission_id'])
    contract=s.tasks.verify_budget_contract(row)
    m.rollback(key)
    asyncio.run(m.apply(key,client))
    assert saved(setup)['state']=='sqlite'
    assert job(setup)['execution_backend']=='sqlite' and job(setup)['is_enabled']
    assert job(setup)['run_count']==3 and job(setup)['timezone']=='UTC'
    assert utc(job(setup)['next_run_at'])>utc(native_id(setup).split(key+'-',1)[1])
    assert s.tasks.verify_budget_contract(row)==contract
    assert s.prepare(key,native_id(setup))=={'done':True}

def test_rollback_drains_accepted_work_before_enabling_sqlite(setup,monkeypatch):
    s,m,key=setup
    client=active(setup,monkeypatch)
    first=s.prepare(key,native_id(setup))
    m.rollback(key)
    with pytest.raises(RuntimeError):asyncio.run(m.apply(key,client))
    assert job(setup)['execution_backend']=='transition' and not job(setup)['is_enabled']
    finish_task(s,first)
    s.poll(key,first['occurrence_id'])
    asyncio.run(m.apply(key,client))
    assert saved(setup)['state']=='sqlite'

def test_closed_trial_can_still_rollback_and_manual_disable_is_preserved(setup,monkeypatch):
    s,m,key=setup
    client=active(setup,monkeypatch)
    assert s.scheduler.disable(key)
    monkeypatch.setenv('AGENT_TEMPORAL_TRIAL_OWNERS','[]')
    m.rollback(key)
    asyncio.run(m.apply(key,client))
    assert saved(setup)['state']=='sqlite' and not job(setup)['is_enabled']


def test_watch_completion_during_rollback_cannot_revive_sqlite(setup,monkeypatch):
    s,m,key=setup
    client=active(setup,monkeypatch)
    first=s.prepare(key,native_id(setup))
    m.rollback(key)
    finish_task(s,first,'{"state":"completed","summary":"done"}')
    s.poll(key,first['occurrence_id'])
    asyncio.run(m.apply(key,client))
    assert saved(setup)['state']=='sqlite' and not job(setup)['is_enabled']

def test_unknown_delete_keeps_legacy_disabled_until_same_rollback_resumes(setup,monkeypatch):
    s,m,key=setup
    client=active(setup,monkeypatch)
    m.rollback(key)
    original=s.sync
    async def interrupted(key,client=None):
        if s.get(key)['retired']:raise TimeoutError()
        await original(key,client)
    monkeypatch.setattr(s,'sync',interrupted)
    with pytest.raises(TimeoutError):asyncio.run(m.apply(key,client))
    assert saved(setup)['state']=='rolling_back' and not job(setup)['is_enabled']
    monkeypatch.setattr(s,'sync',original)
    asyncio.run(m.apply(key,client))
    assert saved(setup)['state']=='sqlite'

def test_legacy_fence_blocks_duplicate_occurrence_after_rollback(setup,monkeypatch):
    s,m,key=setup
    client=active(setup,monkeypatch)
    first=s.prepare(key,native_id(setup))
    finish_task(s,first)
    s.poll(key,first['occurrence_id'])
    m.rollback(key)
    asyncio.run(m.apply(key,client))
    with closing(s.scheduler.connect()) as conn,conn:
        due=conn.execute('SELECT slot FROM schedule_occurrence_fences WHERE job_id=?',(key,)).fetchone()[0]
        row=job(setup)
        row['next_run_at']=due
        assert not admit_legacy(conn,row)
    assert not job(setup)['is_enabled']

def test_native_schema_upgrade_preserves_enable_but_old_queries_see_disabled(setup):
    s,m,key=setup
    native=s.create('new','* * * * *','watch',{'prompt':'read only'},paused=False)
    assert s.get(native)['is_enabled']
    for _ in range(2):
        again=Schedules()
        assert again.get(native)['is_enabled']
    with closing(s.scheduler.connect()) as conn:
        assert conn.execute('SELECT is_enabled FROM scheduled_jobs WHERE id=?',(native,)).fetchone()[0]==0

@pytest.mark.parametrize('kind,payload',[('command',{'command':['whoami']}),('skill',{'skill':'example'})])
def test_unsupported_types_remain_sqlite(setup,kind,payload):
    s,m,key=setup
    with closing(s.scheduler.connect()) as conn,conn:
        conn.execute('UPDATE scheduled_jobs SET job_type=?,job_payload=? WHERE id=?',(kind,canonical(payload),key))
    with pytest.raises(ValueError):preview(key)
    assert job(setup)['execution_backend']=='sqlite'


def test_pre_tp20_schema_upgrade_copies_native_enable_once(setup):
    from agent_execution.scheduling.store import SchedulerStore
    s,m,key=setup
    native=s.create('old native','* * * * *','watch',{'prompt':'read'},paused=False)
    with closing(s.scheduler.connect()) as conn,conn:
        conn.execute('ALTER TABLE temporal_schedule_bindings DROP COLUMN enabled')
        conn.execute('UPDATE scheduled_jobs SET is_enabled=1 WHERE id=?',(native,))
    SchedulerStore(s.scheduler.db_path)
    assert Schedules().get(native)['is_enabled']
    with closing(s.scheduler.connect()) as conn:
        assert conn.execute('SELECT is_enabled FROM scheduled_jobs WHERE id=?',(native,)).fetchone()[0]==0


def test_rollback_waits_for_live_local_reference(setup,monkeypatch):
    from agent_execution.temporal_backend.references import reference
    s,m,key=setup
    client=active(setup,monkeypatch)
    first=s.prepare(key,native_id(setup))
    finish_task(s,first)
    s.poll(key,first['occurrence_id'])
    task=s.tasks.get(first['submission_id'])
    m.rollback(key)
    with reference(s.tasks,task):
        with pytest.raises(RuntimeError,match='reference'):
            asyncio.run(m.apply(key,client))
        assert not job(setup)['is_enabled']
    asyncio.run(m.apply(key,client))
    assert saved(setup)['state']=='sqlite'


def test_reconcile_only_resumes_explicit_transition(setup,monkeypatch):
    from agent_execution.temporal_backend.schedule_migration import reconcile
    s,m,key=setup
    stage(setup)
    apply=AsyncMock()
    monkeypatch.setattr(Migration,'apply',apply)
    asyncio.run(reconcile(s,NS()))
    apply.assert_not_awaited()
    m.cutover(key)
    asyncio.run(reconcile(s,NS()))
    apply.assert_awaited_once()


def test_erasure_scrubs_migration_snapshot_and_fences(setup):
    from agent_execution.workspace_lifecycle import LifecycleStore
    from agent_execution.workspace_lifecycle.store import session_key
    from agent_execution.temporal_backend.schedule_erasure import scrub
    s,m,key=setup
    life=LifecycleStore()
    life.ensure_session()
    stage(setup)
    with closing(s.scheduler.connect()) as conn,conn:
        conn.execute('UPDATE temporal_schedule_bindings SET remote_deleted=1 WHERE job_id=?',(key,))
        conn.execute('INSERT INTO schedule_occurrence_fences VALUES(?,?,?,?)',(key,slot(datetime.now(timezone.utc)),'sqlite','old'))
    # Only a committed session tombstone permits the original TP18 erasure path.
    with closing(s.tasks.tasks.connect()) as conn,conn:
        conn.execute("UPDATE wl_sessions SET state='deleting' WHERE id=?",(session_key(),))
    scrub(s.tasks,session_key())
    with closing(s.scheduler.connect()) as conn:
        assert not conn.execute('SELECT 1 FROM temporal_schedule_migrations').fetchone()
        assert not conn.execute('SELECT 1 FROM schedule_occurrence_fences').fetchone()


def test_cli_preview_and_pending_exit_are_distinct(setup,monkeypatch,capsys):
    from agent_execution.temporal_backend.schedule_migration_cli import main
    s,m,key=setup
    with patch('dotenv.load_dotenv'):
        assert main(['preview','--id',key])==0
        assert 'private original prompt' not in capsys.readouterr().out
        monkeypatch.setattr(Migration,'apply',AsyncMock(side_effect=TimeoutError()))
        assert main(['stage','--id',key,'--digest',preview(key)['source_digest']])==2
        assert json.loads(capsys.readouterr().out)['state']=='pending'
        assert saved(setup)['state']=='staged'


@pytest.mark.parametrize('operation_state',['running','uncertain'])
def test_unknown_operation_blocks_rollback_even_if_root_is_terminal(setup,monkeypatch,operation_state):
    s,m,key=setup
    client=active(setup,monkeypatch)
    first=s.prepare(key,native_id(setup))
    finish_task(s,first)
    s.poll(key,first['occurrence_id'])
    task=s.tasks.get(first['submission_id'])
    # Reproduce a terminal projection with an unresolved persisted operation.
    with closing(s.tasks.tasks.connect()) as conn,conn:
        conn.execute('CREATE TABLE IF NOT EXISTS operation_ledger(owner TEXT,task_id TEXT,state TEXT)')
        conn.execute('INSERT INTO operation_ledger(owner,task_id,state) VALUES(?,?,?)',
            (task['owner'],task['task_id'],operation_state))
    m.rollback(key)
    with pytest.raises(RuntimeError,match='operation needs inspection'):
        asyncio.run(m.apply(key,client))
    assert saved(setup)['state']=='rolling_back' and not job(setup)['is_enabled']


def test_same_observation_stays_quiet_across_both_engine_boundaries(setup,monkeypatch):
    s,m,key=setup
    with closing(s.scheduler.connect()) as conn,conn:
        conn.execute("UPDATE scheduled_jobs SET next_run_at='2026-01-01T00:00:00+00:00' WHERE id=?",(key,))
    legacy=job(setup)
    run=s.scheduler.claim_due(legacy,'legacy-first')
    assert s.scheduler.complete(legacy,'legacy-first',run,'waiting','quiet')
    assert s.scheduler.notifications()==[]
    client=active(setup,monkeypatch)
    first=s.prepare(key,native_id(setup))
    finish_task(s,first)
    s.poll(key,first['occurrence_id'])
    assert s.scheduler.notifications()==[]
    m.rollback(key)
    asyncio.run(m.apply(key,client))
    legacy=job(setup)
    # Advance the claim clock to the next unexecuted slot without changing its identity.
    with patch('agent_execution.scheduling.store.now_utc',return_value=utc(legacy['next_run_at'])+timedelta(seconds=1)):
        run=s.scheduler.claim_due(legacy,'legacy-next')
    assert run and s.scheduler.complete(legacy,'legacy-next',run,'waiting','quiet')
    assert s.scheduler.notifications()==[]
