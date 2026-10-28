"""Idempotent human-requested cleanup of one turn's registered files."""
from contextlib import closing
import json
from pathlib import Path
import time
import uuid


def initialize(store):
    with closing(store.connect()) as conn, conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS companion_attachment_refs (
                session TEXT NOT NULL, task TEXT NOT NULL, directory TEXT NOT NULL,
                PRIMARY KEY(session,task,directory));
            CREATE TABLE IF NOT EXISTS companion_turn_cleanup (
                session TEXT NOT NULL, task TEXT NOT NULL, state TEXT NOT NULL,
                plan TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                lease TEXT, expires REAL NOT NULL DEFAULT 0, PRIMARY KEY(session,task));
        ''')


def validate_task(task):
    if not isinstance(task,str) or not 1<=len(task)<=256:
        raise ValueError('A valid request_id is required')


def status(task):
    from agent_execution.policy import execution_scope
    with execution_scope(task_id=None):
        return _status(task)


def _status(task):
    from agent_execution.workspace_lifecycle import LifecycleStore
    from agent_execution.workspace_lifecycle.store import session_key
    validate_task(task)
    store = LifecycleStore()
    with closing(store.connect()) as conn, conn:
        store.ensure_session(conn)
        row = conn.execute('SELECT * FROM companion_turn_cleanup WHERE session=? AND task=?',
                           (session_key(),task)).fetchone()
        if not row:
            return {'state':'not_started','request_id':task}
        plan = json.loads(row['plan'])
        state = 'deleted' if row['state']=='completed' else 'blocked' if row['state']=='deleting' and row['expires']<=time.time() else row['state']
        return {'state':state,'request_id':task,'files':len(plan['outputs']),
                'attachment_directories':len(plan['sources']),'error':row['error']}


def _source_refs(store, conn, session):
    """Backfill sources staged before per-turn reference tracking was introduced."""
    from .text_attachments import REFERENCE_PREFIX
    known = {r['id']:r for r in conn.execute("SELECT d.id,d.origin_task FROM wl_directories d "
        "JOIN wl_refs f ON f.directory=d.id WHERE f.session=? AND d.relative LIKE 'uploaded-text-%' "
        "AND d.state='active'", (session,))}
    deleted = {r[0] for r in conn.execute("SELECT task FROM companion_turn_cleanup WHERE session=? AND state='completed'",(session,))}
    refs = []
    for row in conn.execute('SELECT t.id,t.checkpoint FROM tasks t JOIN wl_session_owners o ON o.task_owner=t.owner WHERE o.session=?',(session,)):
        if row['id'] in deleted:
            continue
        refs.extend((row['id'],key) for key,item in known.items() if item['origin_task']==row['id'])
        saved = json.loads(row['checkpoint'])
        files = list(saved.get('attachment_translation',{}).get('files',[]))
        for message in saved.get('messages',[]):
            content = message.get('content')
            if message.get('role')=='user' and isinstance(content,list):
                for part in content:
                    text = part.get('text','')
                    if isinstance(text,str) and text.startswith(REFERENCE_PREFIX):
                        files.append(json.loads(text[len(REFERENCE_PREFIX):]))
        refs.extend((row['id'],file['directory_id']) for file in files
                    if isinstance(file,dict) and file.get('directory_id') in known)
    conn.executemany('INSERT OR IGNORE INTO companion_attachment_refs VALUES(?,?,?)',
                     [(session,task,directory) for task,directory in refs])


def _plan(store, conn, session, task):
    from .outputs import preview
    from agent_execution.workspace_lifecycle.paths import snapshot
    _source_refs(store,conn,session)
    outputs = preview(store,conn,task=task)
    sources = []
    for ref in conn.execute('SELECT directory FROM companion_attachment_refs WHERE session=? AND task=?',(session,task)):
        row = store.get(ref[0],conn)
        others = conn.execute('SELECT 1 FROM companion_attachment_refs WHERE directory=? AND (session!=? OR task!=?)',
                              (row['id'],session,task)).fetchone()
        shared = conn.execute('SELECT COUNT(*) FROM wl_refs WHERE directory=?',(row['id'],)).fetchone()[0]>1
        if row['purpose']!='temporary' or shared or others:
            continue
        if row['state']!='active' or not row['relative'].startswith('uploaded-text-'):
            raise ValueError('Attachment directory needs cleanup recovery')
        try:
            manifest = snapshot(store.locate(row))
            if any(record[0] not in {'.','source.txt'} for record in manifest['records']):
                raise PermissionError('Unexpected files in uploaded source directory; retained')
            sources.append({'id':row['id'],'identity':row['identity'],'missing':False,'digest':manifest['digest']})
        except FileNotFoundError:
            sources.append({'id':row['id'],'identity':row['identity'],'missing':True})
    return {'outputs':outputs,'sources':sources}


def delete(task):
    from agent_execution.policy import execution_scope
    with execution_scope(task_id=None):
        return _delete(task)


def _delete(task):
    from agent_execution.workspace_lifecycle import LifecycleStore
    from agent_execution.workspace_lifecycle.store import session_key
    from agent_execution.workspace_lifecycle.operations import enabled
    from agent_execution.workspace_lifecycle.native import PinnedTree
    from .outputs import purge
    validate_task(task)
    enabled()
    store = LifecycleStore()
    lease = uuid.uuid4().hex
    with closing(store.connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        session = store.ensure_session(conn)
        old = conn.execute('SELECT * FROM companion_turn_cleanup WHERE session=? AND task=?',(session,task)).fetchone()
        if old and old['state']=='completed':
            return {'state':'deleted','request_id':task}
        if old and old['expires']>time.time() and old['lease']:
            raise ValueError('Turn file cleanup is already running')
        owners = [r[0] for r in conn.execute('SELECT task_owner FROM wl_session_owners WHERE session=?',(session,))]
        if conn.execute('SELECT 1 FROM tasks t JOIN wl_session_owners o ON o.task_owner=t.owner '
                        "WHERE o.session=? AND (t.state='running' OR COALESCE(t.lease_until,0)>?)",(session,time.time())).fetchone():
            raise ValueError('Wait for conversation tasks to stop before deleting files')
        from agent_execution.workspace_lifecycle.erasure import external_blockers
        if external_blockers(owners,store.path):
            raise ValueError('Conversation background workers are still using files')
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'agent_nodes' in names:
            from agent_execution.coordinator import SUCCESS, STOPPED
            for owner in owners:
                if any(r['state'] not in SUCCESS|STOPPED or (r['expires'] or 0)>time.time()
                       for r in conn.execute('SELECT state,expires FROM agent_nodes WHERE owner=?',(owner,))):
                    raise ValueError('Conversation agents still need to stop')
        tasks = list(conn.execute('SELECT t.* FROM tasks t JOIN wl_session_owners o ON o.task_owner=t.owner WHERE o.session=? AND t.id=?',(session,task)))
        if 'execution_bindings' in names:
            for row in tasks:
                binding = conn.execute('SELECT backend FROM execution_bindings WHERE owner=? AND task_id=?',(row['owner'],task)).fetchone()
                if binding and binding[0]=='temporal':
                    native = conn.execute('SELECT state FROM temporal_submissions WHERE owner=? AND task_id=?',(row['owner'],task)).fetchone() if 'temporal_submissions' in names else None
                    if not native or native[0] not in {'completed','failed','cancelled','deleted'}:
                        raise ValueError('Stop the native workflow before deleting its files')
        own_files = conn.execute('SELECT 1 FROM companion_outputs WHERE session=? AND task=?',(session,task)).fetchone()
        own_sources = conn.execute('SELECT 1 FROM companion_attachment_refs WHERE session=? AND task=?',(session,task)).fetchone()
        if not tasks and not own_files and not own_sources and (
                conn.execute('SELECT 1 FROM companion_outputs WHERE task=? AND session!=?',(task,session)).fetchone()
                or conn.execute('SELECT 1 FROM tasks WHERE id=?',(task,)).fetchone()):
            raise PermissionError('Task files do not belong to this conversation')
        if any((r['lease_until'] or 0)>time.time() or r['state']=='running' for r in tasks):
            raise ValueError('Wait for the task to stop before deleting its files')
        if any(r['state'] in {'uncertain','suspended'} or r['phase'] in {'tool_started','skill_started','waiting_approval'} for r in tasks):
            raise ValueError('Resolve pending task effects or approvals before deleting this turn')
        if not old:
            plan = _plan(store,conn,session,task)
            conn.execute("INSERT INTO companion_turn_cleanup(session,task,state,plan) VALUES(?,?,'deleting',?)",
                         (session,task,json.dumps(plan)))
            for item in plan['outputs']:
                conn.execute("UPDATE companion_outputs SET state='deleting' WHERE id=? AND session=?",(item['id'],session))
            for item in plan['sources']:
                conn.execute("UPDATE wl_directories SET state='purging' WHERE id=?",(item['id'],))
            for row in tasks:
                conn.execute("UPDATE tasks SET state='cancelled',lease_owner=NULL,lease_until=NULL WHERE owner=? AND id=? AND state='pending'",
                             (row['owner'],task))
        else:
            plan = json.loads(old['plan'])
        conn.execute("UPDATE companion_turn_cleanup SET state='deleting',lease=?,expires=?,error='' WHERE session=? AND task=?",
                     (lease,time.time()+600,session,task))

    def fenced():
        with closing(store.connect()) as conn:
            if not conn.execute('SELECT 1 FROM companion_turn_cleanup WHERE session=? AND task=? AND lease=? AND expires>?',
                                (session,task,lease,time.time())).fetchone():
                raise PermissionError('Turn cleanup lease expired')
    try:
        fenced()
        purge(store,plan['outputs'])
        for item in plan['sources']:
            fenced()
            row = store.get(item['id'])
            if row['state']=='purged':
                continue
            if row['identity']!=item['identity'] or row['state']!='purging':
                raise PermissionError('Uploaded source registration changed')
            try:
                location = store.locate(row)
                if item['missing']:
                    raise PermissionError('A missing uploaded directory was recreated')
                with PinnedTree(location) as locked:
                    if locked.digest!=item['digest'] and len(locked.records)>1:
                        raise PermissionError('Uploaded source changed during cleanup')
                    locked.purge()
            except FileNotFoundError:
                pass
            with closing(store.connect()) as conn, conn:
                conn.execute("UPDATE wl_directories SET original_relative=COALESCE(original_relative,relative),relative=?,state='purged',generation=generation+1 WHERE id=?",
                             ('@purged/'+row['id'],row['id']))
                conn.execute('DELETE FROM wl_refs WHERE directory=?',(row['id'],))
        with closing(store.connect()) as conn, conn:
            conn.execute('DELETE FROM companion_attachment_refs WHERE session=? AND task=?',(session,task))
            conn.execute("UPDATE companion_turn_cleanup SET state='completed',lease=NULL,expires=0 WHERE session=? AND task=? AND lease=?",(session,task,lease))
        return {'state':'deleted','request_id':task,'files':len(plan['outputs']),'attachment_directories':len(plan['sources'])}
    except Exception as error:
        with closing(store.connect()) as conn, conn:
            conn.execute("UPDATE companion_turn_cleanup SET state='blocked',error=?,lease=NULL,expires=0 WHERE session=? AND task=? AND lease=?",
                         (str(error),session,task,lease))
        raise
