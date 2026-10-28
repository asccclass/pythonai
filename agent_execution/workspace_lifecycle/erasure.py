"""Outbox-driven data erasure after the frozen filesystem cleanup completes."""
from contextlib import closing
import json
import hashlib
import sqlite3
import time
import os
from pathlib import Path

from agent_execution.sessions import tenant_memory_path
from .paths import identity


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def transports(owners,task_db=None):
    from agent_execution.tasks import TaskStore
    path=task_db or TaskStore().path
    with closing(sqlite3.connect(path)) as conn:
        if 'wl_transports' not in tables(conn):return []
        selected=set(owners)
        return [tuple(r) for r in conn.execute('SELECT DISTINCT t.owner,t.path FROM wl_transports t JOIN wl_session_owners o ON o.session=t.session') if r[0] in selected]


def external_blockers(owners,task_db=None):
    result=[]
    if task_db is not None:
        from agent_execution.temporal_backend.dag import busy
        with closing(sqlite3.connect(task_db)) as conn:
            if busy(conn,owners):result.append('temporal-dag:local-process')
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
    from communication_store import CommunicationStore
    for owner,path in transports(owners,task_db):result.extend(CommunicationStore(path).lifecycle_blockers(owner))
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
        native = 'temporal_schedule_bindings' in tables(conn)
        query = 'SELECT id,name,owner,context,cron_expression,job_type,job_payload FROM scheduled_jobs WHERE owner=? AND (is_enabled=1'
        query += ' OR id IN (SELECT job_id FROM temporal_schedule_bindings))' if native else ')'
        for row in conn.execute(query,(owner,)):
            value=dict(row)
            if native:
                binding=conn.execute('SELECT address,namespace,queue,overlap FROM temporal_schedule_bindings WHERE job_id=?',(row['id'],)).fetchone()
                if binding:
                    value['temporal']=list(binding)
                    value['configuration']=list(conn.execute('SELECT timezone,timeout,max_runs FROM scheduled_jobs WHERE id=?',(row['id'],)).fetchone())
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
    def result():
        from agent_execution.temporal_backend.erasure import session_receipts
        receipts = session_receipts(store,plan['session'])
        value = {'operation_id':key,'state':'deleted'}
        if receipts:value['temporal']=receipts
        return value
    with closing(store.connect()) as conn:
        operation=conn.execute('SELECT state FROM wl_operations WHERE id=?',(key,)).fetchone()
        if not operation or operation[0]!='completed':raise ValueError('Filesystem cleanup is not complete')
        outbox=conn.execute('SELECT state FROM wl_outbox WHERE session=?',(plan['session'],)).fetchone()
        if outbox and outbox[0]=='completed':return result()
        owners=[r[0] for r in conn.execute('SELECT task_owner FROM wl_session_owners WHERE session=?',(plan['session'],))]
        records={owner:[dict(r) for r in conn.execute('SELECT id,episode FROM tasks WHERE owner=?',(owner,))] for owner in owners}
    from agent_execution.temporal_backend.erasure import guard_session
    guard_session(store,plan['session'])
    if external_blockers(owners,store.path):raise ValueError('A transport, schedule or memory worker is still active; cancel/wait before erasure')
    from communication_store import CommunicationStore
    for owner,path in transports(owners,store.path):CommunicationStore(path).erase_session(owner)
    erase_schedules(owners)
    trace_path=Path(os.environ.get('AGENT_TRACE_DB') or Path(os.environ.get('LOCALAPPDATA',Path.home()))/'PythonAI'/'traces.db')
    if trace_path.exists():
        identity(trace_path)
        with closing(sqlite3.connect(trace_path)) as conn,conn:
            if 'spans' in tables(conn):
                for owner in owners:conn.execute("UPDATE spans SET metadata='{}',episode=NULL,status='deleted' WHERE owner=?",(owner,))
    # Indexes are rebuildable caches. Drop cached text for this owner/workspace
    # so raw deleted source is not left available in a stale search snapshot.
    from agent_execution.code_index import CodeIndex
    index=CodeIndex(initialize_embeddings=False)
    if index.path.exists():
        with closing(index.connect()) as conn,conn:
            if 'ci_files' in tables(conn):conn.execute('DELETE FROM ci_files')
    for owner in owners:
        source,user,project,session=json.loads(owner)
        episodes=[row['episode'] for row in records[owner] if row['episode'] is not None]
        erase_memory(tenant_memory_path(user,project,session))
        if source=='api':erase_memory(tenant_memory_path(user,project,''),episodes)
        if runtime is not None and getattr(runtime,'memory',None) is not None:
            erase_memory(runtime.memory.db_path,episodes)
        if source=='websocket' and event_store is not None and user.startswith('websocket:') and session.startswith(project+':'):
            event_store.erase_owner(event_store.owner(user[len('websocket:'):],project,session[len(project)+1:]))
        from agent_execution.approvals import ApprovalStore
        approvals=ApprovalStore()
        with closing(approvals.connect()) as conn,conn:
            conn.execute("UPDATE approvals SET state='expired',arguments='{}' WHERE identity=? AND session=?",(user,session))
    with closing(store.connect()) as conn:
        has_temporal = 'temporal_submissions' in tables(conn)
    if has_temporal:
        from agent_execution.temporal_backend.erasure import finish_session
        from agent_execution.temporal_backend.store import Store
        finish_session(Store(store.path),plan['session'])
    from agent_execution.temporal_backend.dag_erasure import scrub as scrub_dags
    scrub_dags(store,plan['session'])
    with closing(store.connect()) as conn,conn:
        conn.execute('BEGIN IMMEDIATE')
        names=tables(conn)
        for owner in owners:
            conn.execute("UPDATE tasks SET goal='',criteria='{}',checkpoint='{}',reply='',episode=NULL WHERE owner=?",(owner,))
            conn.execute("UPDATE task_steps SET input='{}',result=NULL WHERE owner=?",(owner,))
            if 'temporal_profile_inputs' in names:conn.execute('DELETE FROM temporal_profile_inputs WHERE owner=?',(owner,))
            if 'delivery_artifacts' in names:conn.execute("UPDATE delivery_artifacts SET payload='{}',state='deleted' WHERE owner=?",(owner,))
            if 'agent_nodes' in names:
                conn.execute("UPDATE agent_nodes SET goal='',payload='{}',result='' WHERE owner=?",(owner,))
                conn.execute("UPDATE agent_events SET data='{}' WHERE owner=?",(owner,))
        conn.execute("UPDATE wl_sessions SET state='deleted' WHERE id=?",(plan['session'],))
        conn.execute('DELETE FROM companion_attachment_refs WHERE session=?',(plan['session'],))
        conn.execute("UPDATE wl_outbox SET state='completed' WHERE session=?",(plan['session'],))
        conn.execute('INSERT OR IGNORE INTO wl_audit VALUES(?,?,?,?,?,?,?)',(key,plan['principal'],plan['session'],'deleted',sum(r['files'] for r in plan['payload']['items']),sum(r['bytes'] for r in plan['payload']['items']),time.time()))
    if runtime is not None:runtime.messages=[m for m in runtime.messages if m.get('role')=='system']
    return result()
