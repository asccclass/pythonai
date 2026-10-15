"""Exact-plan execution, persistent intent and pinned native filesystem operations."""
from contextlib import closing
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from agent_execution.approvals import ApprovalStore
from agent_execution.policy import current_context
from .native import PinnedTree
from .paths import directory, identity, snapshot
from .store import principal, session_key, public_payload


def enabled():
    if os.environ.get('AGENT_WORKSPACE_CLEANUP','1').lower() not in {'1','true'}:
        raise PermissionError('New destructive workspace operations are disabled')


def trash(row,key):
    root=Path(row['root_path'])
    path=root.parent/'.pythonai-workspace-trash'
    if not path.exists():path.mkdir()
    identity(path)
    if os.name=='nt':
        # Use the physical extended path for all persisted archive operations;
        # Store-packaged processes may virtualize LocalAppData, and archive
        # metadata paths may exceed MAX_PATH.
        physical=str(path.resolve(strict=True))
        path=Path(physical if physical.startswith('\\\\?\\') else '\\\\?\\'+physical)
        result=subprocess.run(['whoami','/user','/fo','csv','/nh'],capture_output=True,text=True,timeout=5)
        records=list(csv.reader(io.StringIO(result.stdout)))
        sid=records[0][-1] if result.returncode==0 and records else ''
        if not re.fullmatch(r'S-1-\d+(?:-\d+)+',sid):raise PermissionError('Cannot determine service SID')
        result=subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r','*'+sid+':(OI)(CI)F'],capture_output=True,timeout=5)
        if result.returncode:raise PermissionError('Cannot protect archive ACL')
    else:raise PermissionError('Native destructive cleanup is not available on this platform')
    for part in (row['root'],hashlib.sha256(principal().encode()).hexdigest(),row['id'],key):
        path=path/part
        if not path.exists():path.mkdir()
        identity(path)
    if path.stat().st_dev!=root.stat().st_dev:raise PermissionError('Archive must be on the same volume')
    return path/'payload'


def check_items(store,plan,conn):
    if plan['expires']<=time.time():raise ValueError('Preview expired; preview again')
    store.ensure_session(conn)
    owners=[r[0] for r in conn.execute('SELECT task_owner FROM wl_session_owners WHERE session=?',(session_key(),))]
    for owner in owners:
        if conn.execute("SELECT 1 FROM tasks WHERE owner=? AND (state NOT IN ('completed','failed','cancelled') OR COALESCE(lease_until,0)>?)",(owner,time.time())).fetchone():
            raise ValueError('Task still active; wait for confirmed termination')
    from .erasure import external_blockers
    if external_blockers(owners):raise ValueError('Disable schedules or wait for memory workers before cleanup')
    from .erasure import schedules
    if plan['action']=='delete_session' and schedules(owners)!=plan['payload'].get('schedules',[]):
        raise ValueError('Schedules changed; preview again')
    for kept in plan['payload']['kept']:
        row=store.get(kept['id'],conn)
        refs=sorted(r[0] for r in conn.execute('SELECT session FROM wl_refs WHERE directory=?',(row['id'],)))
        if row['generation']!=kept.get('generation') or refs!=kept.get('references'):
            raise ValueError('Preserved directory references changed; preview again')
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='agent_nodes'").fetchone():
        from agent_execution.coordinator import SUCCESS,STOPPED
        for owner in owners:
            for node in conn.execute('SELECT state,expires FROM agent_nodes WHERE owner=?',(owner,)):
                if node['state'] not in SUCCESS|STOPPED or (node['expires'] or 0)>time.time():
                    raise ValueError('A delegated worker still needs termination or resolution')
    for item in plan['payload']['items']:
        row=store.get(item['id'],conn)
        if row['generation']!=item['generation'] or row['state']!=item['state'] or (row['purpose']!='temporary' and plan['action']!='restore'):
            raise ValueError('Directory registration changed; preview again')
        if row['build_state']=='building':raise ValueError('Program creation is still in progress')
        refs=[r[0] for r in conn.execute('SELECT session FROM wl_refs WHERE directory=?',(row['id'],))]
        if refs!=[session_key()]:raise ValueError('Directory references changed; preview again')
        if snapshot(store.locate(row))['digest']!=item['digest']:raise ValueError('Directory contents changed; preview again')


def execute(store,key,approval):
    enabled()
    context=current_context()
    with closing(store.connect()) as conn,conn:
        conn.execute('BEGIN IMMEDIATE')
        plan=store.plan(key,conn)
        old=conn.execute('SELECT * FROM wl_operations WHERE plan=?',(key,)).fetchone()
        if old:
            if old['state']=='completed':return {'operation_id':old['id'],'state':'completed','action':plan['action']}
            raise ValueError('Operation needs recovery; do not repeat the decision')
        check_items(store,plan,conn)
        if plan['approval']!=approval:raise PermissionError('Approval does not match this plan')
        ApprovalStore().consume(approval,context.identity,context.session,'workspace:'+key,public_payload(plan['payload']))
        conn.execute("INSERT INTO wl_operations VALUES(?,?,'running','',?)",(key,key,time.time()))
        conn.execute("UPDATE wl_plans SET state='executing' WHERE id=?",(key,))
        if plan['action']=='delete_session':
            conn.execute("UPDATE wl_sessions SET state='deleting' WHERE id=?",(session_key(),))
            conn.execute("INSERT INTO wl_outbox VALUES(?,?,'pending')",(session_key(),json.dumps({'plan_id':key})))
    return resume(store,key)


def resume(store,key):
    """Resume the durable frozen plan, never discover new deletion candidates."""
    with closing(store.connect()) as conn:
        plan=store.plan(key,conn)
        operation=conn.execute('SELECT * FROM wl_operations WHERE id=?',(key,)).fetchone()
        if not operation:raise PermissionError('No authorized operation exists')
        if operation['state']=='completed':return {'operation_id':key,'state':'completed','action':plan['action']}
    lease=uuid.uuid4().hex
    with closing(store.connect()) as conn,conn:
        conn.execute('BEGIN IMMEDIATE')
        saved=conn.execute('SELECT expires FROM wl_leases WHERE operation=?',(key,)).fetchone()
        if saved and saved[0]>time.time():raise ValueError('Cleanup worker is still active')
        conn.execute('INSERT OR REPLACE INTO wl_leases VALUES(?,?,?)',(key,lease,time.time()+600))
    def fenced():
        with closing(store.connect()) as conn:
            if not conn.execute('SELECT 1 FROM wl_leases WHERE operation=? AND token=? AND expires>?',(key,lease,time.time())).fetchone():
                raise PermissionError('Cleanup lease expired or was replaced')
    try:
        if plan['action']=='delete_session':
            from .erasure import disable_schedules,external_blockers,schedules
            with closing(store.connect()) as conn:
                owners=[r[0] for r in conn.execute('SELECT task_owner FROM wl_session_owners WHERE session=?',(plan['session'],))]
            if plan['state']!='completed':
                current=schedules(owners)
                if current:
                    disable_schedules(owners,plan['payload'].get('schedules',[]))
            if external_blockers(owners):raise ValueError('Waiting for workers to stop; files retained')
        for item in plan['payload']['items']:
            fenced()
            with closing(store.connect()) as conn,conn:
                conn.execute('BEGIN IMMEDIATE')
                row=store.get(item['id'],conn)
                action=plan['action']
                final='active' if action=='restore' else 'archived' if action=='archive' else 'purged'
                if row['state']==final and row['generation']>item['generation']:continue
                if (row['purpose']!='temporary' and action!='restore') or row['generation']!=item['generation']:
                    raise PermissionError('Directory registration changed after approval')
                source=directory(Path(row['root_path']),row['relative'])
                destination=(Path(row['archive']) if row['archive'] else trash(row,key))
                moving=action=='restore' or row['state'] in {'active','archiving','quarantining'}
                origin=destination if action=='restore' else source
                target=source if action=='restore' else destination
                if moving:
                    conn.execute('UPDATE wl_directories SET state=?,archive=?,manifest=? WHERE id=?',
                        ('restoring' if action=='restore' else 'archiving' if action=='archive' else 'quarantining',
                         str(destination),json.dumps({'digest':item['digest'],'records':item['manifest']}),row['id']))
                    conn.commit()  # Intent must survive a crash after the filesystem move.
                    if origin.exists() and target.exists():raise PermissionError('Both source and destination exist; recovery required')
                    if origin.exists():
                        if json.dumps(identity(origin))!=row['identity']:raise PermissionError('Source object was replaced')
                        with PinnedTree(origin) as locked:
                            if locked.digest!=item['digest']:raise PermissionError('Contents changed since approval')
                            fenced()
                            locked.move(target)
                    elif not target.exists():raise PermissionError('Both source and destination missing')
                    if json.dumps(identity(target))!=row['identity']:raise PermissionError('Destination object was replaced')
                    with PinnedTree(target) as locked:
                        if locked.digest!=item['digest']:raise PermissionError('Contents changed after move; retained for recovery')
                    conn.execute('UPDATE wl_directories SET state=? WHERE id=?',
                        ('active' if action=='restore' else 'archived' if action=='archive' else 'purge_pending',row['id']))
                    conn.commit()
                if action=='delete_session':
                    # Partial purge is explicitly recorded; the same object may be
                    # retried, but never substitute a newly created destination.
                    row=store.get(item['id'],conn)
                    if destination.exists():
                        if json.dumps(identity(destination))!=row['identity']:raise PermissionError('Purge object was replaced')
                        with PinnedTree(destination) as locked:
                            if row['state']!='purging' and locked.digest!=item['digest']:raise PermissionError('Archive contents changed')
                            original={r[0]:r for r in item['manifest']}
                            if any(original.get(r[0])!=r for r in locked.records):
                                raise PermissionError('Unexpected objects in partial purge; retained')
                            conn.execute("UPDATE wl_directories SET state='purging' WHERE id=?",(row['id'],))
                            conn.commit()
                            fenced()
                            locked.purge()
                    elif row['state']!='purging':raise PermissionError('Purge destination unexpectedly missing')
                if final=='purged':
                    conn.execute("UPDATE wl_directories SET original_relative=COALESCE(original_relative,relative),relative=?,state='purged',generation=generation+1 WHERE id=?",('@purged/'+row['id'],row['id']))
                else:
                    conn.execute('UPDATE wl_directories SET state=?,generation=generation+1 WHERE id=?',(final,row['id']))
        with closing(store.connect()) as conn,conn:
            if plan['action']=='delete_session':
                for item in plan['payload']['kept']:
                    conn.execute('UPDATE wl_directories SET generation=generation+1 WHERE id=?',(item['id'],))
                conn.execute('DELETE FROM wl_refs WHERE session=?',(plan['session'],))
                conn.execute('DELETE FROM wl_resource_refs WHERE session=?',(plan['session'],))
                # Transport-specific erasure is an outbox consumer; only mark
                # fully deleted after it acknowledges persistent data removal.
                conn.execute("UPDATE wl_outbox SET state='ready' WHERE session=?",(plan['session'],))
            conn.execute("UPDATE wl_operations SET state='completed',error='',updated=? WHERE id=?",(time.time(),key))
            conn.execute("UPDATE wl_plans SET state='completed' WHERE id=?",(key,))
        return {'operation_id':key,'state':'completed','action':plan['action'],'session':plan['session']}
    except Exception as error:
        with closing(store.connect()) as conn,conn:
            conn.execute("UPDATE wl_operations SET state='blocked',error=?,updated=? WHERE id=?",(str(error),time.time(),key))
        return {'operation_id':key,'state':'blocked','action':plan['action'],'error':str(error)}
    finally:
        with closing(store.connect()) as conn,conn:conn.execute('DELETE FROM wl_leases WHERE operation=? AND token=?',(key,lease))
