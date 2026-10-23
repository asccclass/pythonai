"""Human-only observation records for fenced file writes; never resume or replay."""
from contextlib import closing
import json
import time

from agent_execution.policy import current_context
from agent_execution.tasks import task_owner, assert_owner_active
from .store import canonical, digest


def binding(tasks,owner,task):
    with closing(tasks.connect()) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_submissions'").fetchone():
            return None
        row = conn.execute('SELECT * FROM temporal_submissions WHERE owner=? AND task_id=?',(owner,task)).fetchone()
    if row is None or json.loads(row['context']).get('file_writes') is None:
        return None
    from .file_writes import capabilities
    from .contracts import APPROVAL_WORKFLOW_NAME
    capabilities(json.loads(row['context']),APPROVAL_WORKFLOW_NAME)
    if task_owner(current_context())!=owner:
        raise PermissionError('Resolution is outside the current owner')
    return dict(row)


def entries(tasks,owner,task):
    row = binding(tasks,owner,task)
    if row is None:
        return None
    with closing(tasks.connect()) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_file_writes'").fetchone():
            return []
        values = conn.execute("""SELECT w.operation_id,w.call_id,w.name,w.plan,w.observation,
            o.state FROM temporal_file_writes w LEFT JOIN operation_ledger o
            ON o.owner=? AND o.task_id=? AND o.step_id=w.operation_id WHERE w.submission_id=?""",
            (owner,task,row['id'])).fetchall()
        has_resolutions = conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_write_resolutions'").fetchone()
        resolved = {r[0] for r in conn.execute('SELECT operation_id FROM temporal_write_resolutions WHERE submission_id=?',(row['id'],))} if has_resolutions else set()
    return [{'operation_id':v['operation_id'],'tool_call_id':v['call_id'],'name':v['name'],
             'state':v['state'] or 'awaiting_approval','paths':json.loads(v['plan'])['paths'],
             'effect_observed':v['observation'] is not None,'human_observation':v['operation_id'] in resolved} for v in values]


def record(tasks,owner,task,operation,text):
    row = binding(tasks,owner,task)
    if row is None:
        return False
    from permissions import current_role, get_user_role
    context = current_context()
    if current_role()!='admin' or (context.identity!='local' and get_user_role(context.identity)!='admin'):
        raise PermissionError('File-write resolution requires current admin role')
    if not isinstance(text,str) or len(text.encode())>16*1024:
        raise ValueError('Write observation exceeds 16 KiB')
    value = json.loads(text)
    if (not isinstance(value,dict) or set(value)!={'status','note'}
            or not isinstance(value['status'],str) or value['status'] not in {'matches_expected','matches_before'}
            or not isinstance(value['note'],str) or not 1<=len(value['note'])<=4000):
        raise ValueError('Use JSON status matches_expected/matches_before and a human note')
    with closing(tasks.connect()) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_file_writes'").fetchone():
            raise ValueError('No file-write plan to observe')
        plans = conn.execute('SELECT * FROM temporal_file_writes WHERE submission_id=? AND (operation_id=? OR call_id=?)',
                             (row['id'],operation,operation)).fetchall()
    if len(plans)!=1:
        raise ValueError('Specify one exact file-write operation ID from /steps')
    item = dict(plans[0])
    plan = json.loads(item['plan'])
    if row['state'] not in {'uncertain','cancelled','failed'}:
        raise PermissionError('Only fenced terminal writes can be observed')
    from .publication import publication_scope
    from .store import Store
    from .file_writes import snapshot
    from agent_execution.workspace_lifecycle import LifecycleStore
    with publication_scope(Store(tasks.path),row):
        from base import workspace_root
        for path in plan['paths']:
            LifecycleStore().assert_access(workspace_root()/path)
        observed = snapshot(plan['paths'])
    if observed['.']!=plan['before']['.']:
        raise PermissionError('Observed workspace identity changed')
    if value['status']=='matches_before':
        matches = observed==plan['before']
    else:
        matches = all(all(observed[p].get(k)==v for k,v in expected.items()) for p,expected in plan['expected'].items())
        matches = matches and all(observed[p]==before for p,before in plan['before'].items()
                                  if before['kind']=='directory' and p not in plan['paths'])
    if not matches:
        raise PermissionError('Actual filesystem does not match the claimed observation')
    data = {'status':value['status'],'note':value['note'],'actor':context.identity,
            'session':context.session,'plan_digest':digest(plan),'observed':observed}
    with closing(tasks.connect()) as conn,conn:
        conn.execute('BEGIN IMMEDIATE')
        assert_owner_active(conn,owner)
        if current_role()!='admin' or (context.identity!='local' and get_user_role(context.identity)!='admin'):
            raise PermissionError('Admin role revoked before observation commit')
        current = conn.execute('SELECT state,fence FROM temporal_submissions WHERE id=? AND owner=?',
                               (row['id'],owner)).fetchone()
        task_row = conn.execute('SELECT state FROM tasks WHERE owner=? AND id=?',(owner,task)).fetchone()
        op = conn.execute('SELECT state FROM operation_ledger WHERE owner=? AND task_id=? AND step_id=?',
                          (owner,task,item['operation_id'])).fetchone()
        if (not current or current['state']!=row['state'] or current['fence']!=row['fence']
                or not task_row or task_row['state'] not in {'uncertain','cancelled','failed'}
                or not op or op['state'] not in {'running','uncertain'}):
            raise PermissionError('Write observation is not a matching unknown fenced operation')
        conn.execute("""CREATE TABLE IF NOT EXISTS temporal_write_resolutions(
            submission_id TEXT NOT NULL,operation_id TEXT NOT NULL,data TEXT NOT NULL,
            created REAL NOT NULL,PRIMARY KEY(submission_id,operation_id))""")
        old = conn.execute('SELECT data FROM temporal_write_resolutions WHERE submission_id=? AND operation_id=?',
                           (row['id'],item['operation_id'])).fetchone()
        if old:
            if old['data']!=canonical(data):
                raise PermissionError('Human write observation is immutable')
            return True
        from .ledger import Ledger
        Ledger._capacity(conn,len(canonical(data).encode())+2048)
        conn.execute('INSERT INTO temporal_write_resolutions VALUES(?,?,?,?)',
                     (row['id'],item['operation_id'],canonical(data),time.time()))
    return True
