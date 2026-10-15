"""Outbox-driven data erasure after the frozen filesystem cleanup completes."""
from contextlib import closing
import json
import hashlib
import sqlite3
import time

from agent_execution.sessions import tenant_memory_path
from .paths import identity


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def external_blockers(owners):
    result=[]
    from agent_execution.scheduling.store import scheduler_db_path
    path=scheduler_db_path()
    if path.exists():
        with closing(sqlite3.connect(path)) as conn:
            if 'scheduled_jobs' in tables(conn):
                for owner in owners:
                    for row in conn.execute('SELECT j.id FROM scheduled_jobs j JOIN scheduled_job_locks l ON l.job_id=j.id WHERE j.owner=? AND l.expires_at>?',(owner,time.time())):
                        result.append('schedule:'+row[0])
    for owner in owners:
        source,user,project,session=json.loads(owner)
        path=tenant_memory_path(user,project,session)
        if path.exists():
            identity(path)
            with closing(sqlite3.connect(path)) as conn:
                if 'memory_jobs' in tables(conn) and conn.execute("SELECT 1 FROM memory_jobs WHERE status='processing'").fetchone():
                    result.append('memory-worker:'+session)
    return result


def schedules(owners):
    from agent_execution.scheduling.store import scheduler_db_path
    path=scheduler_db_path()
    if not path.exists():return []
    with closing(sqlite3.connect(path)) as conn:
        return schedule_snapshot(conn,owners)


def schedule_snapshot(conn,owners):
    conn.row_factory=sqlite3.Row
    if 'scheduled_jobs' not in tables(conn):return []
    result=[]
    for owner in owners:
        for row in conn.execute('SELECT id,name,owner,context,cron_expression,job_type,job_payload FROM scheduled_jobs WHERE owner=? AND is_enabled=1',(owner,)):
            value=dict(row)
            result.append({'id':value['id'],'name':value['name'],'digest':hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()})
    return sorted(result,key=lambda item:item['id'])


def disable_schedules(owners,expected):
    from agent_execution.scheduling.store import scheduler_db_path
    if not scheduler_db_path().exists():
        if expected:raise ValueError('Schedules changed since preview')
        return
    with closing(sqlite3.connect(scheduler_db_path())) as conn,conn:
        conn.execute('BEGIN IMMEDIATE')
        if schedule_snapshot(conn,owners)!=expected:raise ValueError('Schedules changed since preview')
        for row in expected:
            conn.execute('UPDATE scheduled_jobs SET is_enabled=0 WHERE id=?',(row['id'],))


def erase_memory(path,episodes=None):
    if not path.exists():return
    identity(path)
    with closing(sqlite3.connect(path)) as conn,conn:
        names=tables(conn)
        if 'episodes' not in names:return
        if episodes is None:episodes=[r[0] for r in conn.execute('SELECT id FROM episodes')]
        for episode in episodes:
            conn.execute("UPDATE episodes SET status='deleted',summary='' WHERE id=?",(episode,))
            conn.execute("UPDATE episode_events SET content='',metadata='{}' WHERE episode_id=?",(episode,))
            if 'memory_jobs' in names:
                conn.execute("UPDATE memory_jobs SET status='cancelled',payload='{}',last_error='' WHERE episode_id=?",(episode,))
            # Retain shared derived memories, but remove access to raw provenance.
            if 'entities' in names:
                conn.execute('UPDATE entities SET source_event_id=NULL WHERE source_event_id IN (SELECT id FROM episode_events WHERE episode_id=?)',(episode,))


def erase_schedules(owners):
    from agent_execution.scheduling.store import scheduler_db_path
    path=scheduler_db_path()
    if not path.exists():return
    with closing(sqlite3.connect(path)) as conn,conn:
        conn.execute('BEGIN IMMEDIATE')
        if 'scheduled_jobs' not in tables(conn):return
        for owner in owners:
            keys=[r[0] for r in conn.execute('SELECT id FROM scheduled_jobs WHERE owner=?',(owner,))]
            for key in keys:
                conn.execute("UPDATE scheduled_job_runs SET output='',error_message='' WHERE job_id=?",(key,))
                conn.execute("UPDATE schedule_notification_delivery SET status='cancelled',token=NULL,expires=NULL WHERE id IN (SELECT id FROM schedule_notifications WHERE job_id=?)",(key,))
                conn.execute("UPDATE schedule_notifications SET output='',state='deleted' WHERE job_id=?",(key,))
            conn.execute("UPDATE scheduled_jobs SET is_enabled=0,name='',description='',job_payload='{}',history='[]',last_digest=NULL WHERE owner=?",(owner,))


def finalize(store,key,runtime=None,event_store=None):
    plan=store.plan(key)
    if plan['action']!='delete_session':raise ValueError('Not a session deletion')
    with closing(store.connect()) as conn:
        operation=conn.execute('SELECT state FROM wl_operations WHERE id=?',(key,)).fetchone()
        if not operation or operation[0]!='completed':raise ValueError('Filesystem cleanup is not complete')
        outbox=conn.execute('SELECT state FROM wl_outbox WHERE session=?',(plan['session'],)).fetchone()
        if outbox and outbox[0]=='completed':return {'operation_id':key,'state':'deleted'}
        owners=[r[0] for r in conn.execute('SELECT task_owner FROM wl_session_owners WHERE session=?',(plan['session'],))]
        records={owner:[dict(r) for r in conn.execute('SELECT id,episode FROM tasks WHERE owner=?',(owner,))] for owner in owners}
    if external_blockers(owners):raise ValueError('A schedule or memory worker is still active; disable/wait before erasure')
    erase_schedules(owners)
    # Indexes are rebuildable caches. Drop cached text for this owner/workspace
    # so raw deleted source is not left available in a stale search snapshot.
    from agent_execution.code_index import CodeIndex
    index=CodeIndex()
    if index.path.exists():
        with closing(index.connect()) as conn,conn:
            if 'ci_files' in tables(conn):conn.execute('DELETE FROM ci_files')
    for owner in owners:
        source,user,project,session=json.loads(owner)
        episodes=[row['episode'] for row in records[owner] if row['episode'] is not None]
        erase_memory(tenant_memory_path(user,project,session))
        if runtime is not None and getattr(runtime,'memory',None) is not None:
            erase_memory(runtime.memory.db_path,episodes)
        if source=='websocket' and event_store is not None and user.startswith('websocket:') and session.startswith(project+':'):
            event_store.erase_owner(event_store.owner(user[len('websocket:'):],project,session[len(project)+1:]))
        from agent_execution.approvals import ApprovalStore
        approvals=ApprovalStore()
        with closing(approvals.connect()) as conn,conn:
            conn.execute("UPDATE approvals SET state='expired',arguments='{}' WHERE identity=? AND session=?",(user,session))
    with closing(store.connect()) as conn,conn:
        conn.execute('BEGIN IMMEDIATE')
        names=tables(conn)
        for owner in owners:
            conn.execute("UPDATE tasks SET goal='',criteria='{}',checkpoint='{}',reply='',episode=NULL WHERE owner=?",(owner,))
            conn.execute("UPDATE task_steps SET input='{}',result=NULL WHERE owner=?",(owner,))
            if 'delivery_artifacts' in names:conn.execute("UPDATE delivery_artifacts SET payload='{}',state='deleted' WHERE owner=?",(owner,))
            if 'agent_nodes' in names:
                conn.execute("UPDATE agent_nodes SET goal='',payload='{}',result='' WHERE owner=?",(owner,))
                conn.execute("UPDATE agent_events SET data='{}' WHERE owner=?",(owner,))
        conn.execute("UPDATE wl_sessions SET state='deleted' WHERE id=?",(plan['session'],))
        conn.execute("UPDATE wl_outbox SET state='completed' WHERE session=?",(plan['session'],))
    if runtime is not None:runtime.messages=[m for m in runtime.messages if m.get('role')=='system']
    return {'operation_id':key,'state':'deleted'}
