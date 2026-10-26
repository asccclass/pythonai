"""SDK-free retention admission, frozen targets and irreversible opaque task fences."""
from contextlib import closing
import json
import re
import time
import uuid

from .store import canonical,digest


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def task_token(owner,task):
    return digest([owner,task])


def assert_not_erased(conn,owner,task):
    if 'temporal_erased_tasks' in tables(conn) and conn.execute(
        'SELECT 1 FROM temporal_erased_tasks WHERE token=?',(task_token(owner,task),)).fetchone():
        raise PermissionError('Task was fenced for erasure; its ID cannot be reused')


def install(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS temporal_erased_tasks(
        token TEXT PRIMARY KEY,state TEXT NOT NULL,receipt TEXT NOT NULL,updated REAL NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS temporal_erasures(
        submission_id TEXT PRIMARY KEY,token TEXT NOT NULL UNIQUE,reason TEXT NOT NULL,
        session TEXT,state TEXT NOT NULL,receipt TEXT NOT NULL,updated REAL NOT NULL)""")


def preview(conn,owners):
    if 'temporal_submissions' not in tables(conn):
        return []
    result = []
    for owner in owners:
        for row in conn.execute('SELECT * FROM temporal_submissions WHERE owner=? ORDER BY id',(owner,)):
            row = dict(row)
            result.append({'id':row['id'],'state':row['state'],
                'digest':digest([row['workflow_id'],row['input_digest'],row['context'],row['state'],row['updated']])})
    return sorted(result,key=lambda v:v['id'])


def prepare(conn,row,reason,session=None):
    if reason not in {'session','deleted_session','retention'}:
        raise ValueError('Unsupported erasure reason')
    install(conn)
    old = conn.execute('SELECT state FROM temporal_erasures WHERE submission_id=?',(row['id'],)).fetchone()
    if old:
        return
    from agent_execution.delivery import positive_limit,CapacityError
    if conn.execute('SELECT count(*) FROM temporal_erased_tasks').fetchone()[0]>=positive_limit('AGENT_TEMPORAL_OPERATION_LIMIT',100000):
        raise CapacityError('Erasure tombstone capacity reached; fences are never evicted')
    token = task_token(row['owner'],row['task_id'])
    conn.execute("INSERT INTO temporal_erased_tasks VALUES(?,'pending','{}',?)",(token,time.time()))
    conn.execute("INSERT INTO temporal_erasures VALUES(?,?,?,?,'pending','{}',?)",
                 (row['id'],token,reason,session,time.time()))
    from .ledger import fence_operations
    fence_operations(conn,row['owner'],row['task_id'])
    conn.execute("UPDATE temporal_submissions SET state='erasing',fence=?,updated=? WHERE id=?",
                 (uuid.uuid4().hex,time.time(),row['id']))
    conn.execute("UPDATE tasks SET state='cancelled',phase='temporal_erasing',lease_owner=NULL,lease_until=NULL WHERE owner=? AND id=?",
                 (row['owner'],row['task_id']))


def prepare_session(conn,session):
    if 'temporal_submissions' not in tables(conn):
        return
    lifecycle = conn.execute('SELECT state FROM wl_sessions WHERE id=?',(session,)).fetchone()
    if not lifecycle or lifecycle[0] not in {'deleting','deleted'}:
        raise PermissionError('Session erasure requires its committed lifecycle tombstone')
    owners = [r[0] for r in conn.execute('SELECT task_owner FROM wl_session_owners WHERE session=?',(session,))]
    for owner in owners:
        for row in conn.execute('SELECT * FROM temporal_submissions WHERE owner=?',(owner,)).fetchall():
            prepare(conn,dict(row),'deleted_session' if lifecycle[0]=='deleted' else 'session',session)



def queue_deleted_sessions(conn):
    names = tables(conn)
    if not {'temporal_submissions','wl_sessions','wl_outbox','wl_operations','wl_plans'}<=names:
        return
    rows = conn.execute("""SELECT s.id,o.payload FROM wl_sessions s JOIN wl_outbox o ON o.session=s.id
        WHERE s.state='deleted' AND o.state='completed' AND EXISTS(
        SELECT 1 FROM wl_session_owners w JOIN temporal_submissions t ON t.owner=w.task_owner
        WHERE w.session=s.id) LIMIT 100""").fetchall()
    for session,payload in rows:
        key = json.loads(payload).get('plan_id')
        consent = conn.execute("""SELECT 1 FROM wl_operations o JOIN wl_plans p ON p.id=o.plan
            WHERE o.id=? AND o.state='completed' AND p.session=? AND p.action='delete_session'""",(key,session)).fetchone()
        if consent:
            prepare_session(conn,session)
            conn.execute("UPDATE temporal_erasures SET reason='deleted_session' WHERE session=? AND state!='local_done'",(session,))

def eligible(conn,row,before):
    if 'temporal_agent_graphs' in tables(conn) and conn.execute(
            'SELECT 1 FROM temporal_agent_graphs WHERE owner=? AND root=?',(row['owner'],row['task_id'])).fetchone():
        return False  # Shared budgets and guarded merge references still belong to this DAG.
    from .schedules import task_pending
    if task_pending(conn,row['id']):
        return False
    if row['state'] not in {'completed','failed','cancelled'} or row['updated']>=before:
        return False
    names = tables(conn)
    task = conn.execute('SELECT state,lease_until FROM tasks WHERE owner=? AND id=?',(row['owner'],row['task_id'])).fetchone()
    if not task or task[0] not in {'completed','failed','cancelled'} or (task[1] or 0)>time.time():
        return False
    for table,where,parameters in [
        ('operation_ledger',"owner=? AND task_id=? AND state IN ('running','uncertain')",(row['owner'],row['task_id'])),
        ('temporal_step_runs',"submission_id=? AND status='running'",(row['id'],)),
        ('temporal_finalizations',"submission_id=? AND state!='completed'",(row['id'],)),
        ('temporal_profile_inputs',"owner=? AND task_id=? AND state='running'",(row['owner'],row['task_id'])),
        ('temporal_human_answers',"submission_id=? AND state IN ('waiting','answered')",(row['id'],)),
        ('temporal_approval_waits',"submission_id=? AND state IN ('preparing','pending')",(row['id'],))]:
        if table in names and conn.execute('SELECT 1 FROM '+table+' WHERE '+where,parameters).fetchone():
            return False
    publication = json.loads(row['context']).get('publication') or {}
    if publication.get('delivery') is not None and 'temporal_events' in names and conn.execute(
            "SELECT 1 FROM temporal_events WHERE submission_id=? AND delivery='pending'",(row['id'],)).fetchone():
        return False
    if 'temporal_approval_waits' in names:
        for operation, in conn.execute("SELECT operation_id FROM temporal_approval_waits WHERE submission_id=? AND state='approved'",(row['id'],)):
            if 'operation_ledger' not in names or not conn.execute(
                    "SELECT 1 FROM operation_ledger WHERE owner=? AND task_id=? AND step_id=? AND state='completed'",
                    (row['owner'],row['task_id'],operation)).fetchone():
                return False
    if 'temporal_local_references' in names:
        from .references import process_stamp
        for pid,stamp in conn.execute('SELECT pid,process FROM temporal_local_references WHERE submission_id=?',(row['id'],)):
            if process_stamp(pid)==stamp:
                return False
    return True


def candidates(conn,owner_hash,before,limit):
    if not re.fullmatch('[0-9a-f]{64}',owner_hash or '') or type(limit) is not int or not 1<=limit<=100:
        raise ValueError('Retention requires an exact owner hash and limit 1..100')
    if type(before) not in {int,float} or not 0<before<time.time():
        raise ValueError('Retention cutoff must be in the past')
    if 'temporal_submissions' not in tables(conn):
        return []
    from .admission import owner_key
    result = []
    for value in conn.execute("SELECT * FROM temporal_submissions WHERE state IN ('completed','failed','cancelled') AND updated<? ORDER BY updated,id",(before,)):
        row = dict(value)
        if owner_key(row['owner'])==owner_hash and eligible(conn,row,before):
            result.append(row['id'])
            if len(result)==limit:
                break
    return result


def select(store,owner_hash,before,limit=100,apply=False):
    with closing(store.tasks.connect()) as conn,conn:
        if apply:
            conn.execute('BEGIN IMMEDIATE')
        keys = candidates(conn,owner_hash,before,limit)
        if apply:
            for key in keys:
                row = dict(conn.execute('SELECT * FROM temporal_submissions WHERE id=?',(key,)).fetchone())
                prepare(conn,row,'retention')
    return keys
