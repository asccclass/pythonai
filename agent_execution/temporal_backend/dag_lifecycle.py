"""Owner-authorized, at-most-once DAG merge/archive and conservative orphan recovery."""
from contextlib import closing
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from .dag import Graphs, STOPPED
from .references import process_stamp
from .store import canonical
from agent_execution.coordinator import node_task_owner
from agent_execution.policy import current_context, execution_scope
from agent_execution.tasks import assert_owner_active
from agent_execution.worktrees import WorktreeStore, git
from permissions import set_current_role

_permit = ContextVar('dag_lifecycle_permit', default=None)


def fingerprint(path):
    """Bounded content proof includes ignored/untracked bytes, excluding Git internals."""
    digest=hashlib.sha256();total=count=0
    for directory,folders,files in os.walk(path):
        folders.sort();files.sort()
        for name in folders:
            if name=='.git' or (Path(directory)/name).resolve()!=Path(directory)/name:
                raise PermissionError('Embedded repositories and links require manual preservation')
        count+=len(folders)+len(files)
        if count>10000:raise ValueError('Worktree manifest capacity exceeded')
        for name in files:
            item=Path(directory)/name
            if item==path/'.git':continue
            if item.resolve()!=item:raise PermissionError('Worktree link requires manual preservation')
            if item.stat().st_size>64*1024*1024:raise ValueError('Worktree content capacity exceeded')
            with item.open('rb') as source:data=source.read(64*1024*1024+1)
            total+=len(data)
            if len(data)>64*1024*1024 or total>256*1024*1024:raise ValueError('Worktree content capacity exceeded')
            digest.update(str(item.relative_to(path)).encode());digest.update(b'\0')
            digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def install(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS temporal_agent_processes(
        node_id TEXT PRIMARY KEY,graph_id TEXT NOT NULL,lease TEXT NOT NULL,
        pid INTEGER NOT NULL,process TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS temporal_dag_operations(
        id TEXT PRIMARY KEY,graph_id TEXT NOT NULL,node_id TEXT NOT NULL,action TEXT NOT NULL,
        snapshot TEXT NOT NULL,state TEXT NOT NULL,pid INTEGER,process TEXT,result TEXT,
        updated REAL NOT NULL,UNIQUE(graph_id,node_id,action))''')


def register_process(store, node):
    if not node['payload'].get('temporal_graph'):
        return
    with closing(store.connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        permit = conn.execute("SELECT * FROM temporal_agent_runs WHERE node_id=? AND lease=? AND state='started'",
                              (node['id'], node['lease'])).fetchone()
        if not permit or process_stamp(permit['pid']) != permit['process']:
            raise PermissionError('DAG Activity process no longer owns this node')
        old = conn.execute('SELECT * FROM temporal_agent_processes WHERE node_id=?', (node['id'],)).fetchone()
        stamp = process_stamp(os.getpid())
        if old and (old['lease'], old['pid'], old['process']) != (node['lease'], os.getpid(), stamp):
            raise PermissionError('DAG node already has a process identity')
        conn.execute('INSERT OR IGNORE INTO temporal_agent_processes VALUES(?,?,?,?,?)',
            (node['id'], permit['graph_id'], node['lease'], os.getpid(), stamp))


def operation_ids(conn, graph_id):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_dag_operations'").fetchone():
        return []
    return [r[0] for r in conn.execute('SELECT id FROM temporal_dag_operations WHERE graph_id=? ORDER BY id',(graph_id,))]


def live(conn, graph_id, node_id=None, exclude=None):
    for table, fields in [('temporal_agent_processes', 'pid,process'),
                          ('temporal_dag_operations', 'pid,process')]:
        if not conn.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():
            continue
        sql = f'SELECT {fields} FROM {table} WHERE graph_id=?'
        args = [graph_id]
        if node_id:
            sql += ' AND node_id=?'
            args.append(node_id)
        if table == 'temporal_dag_operations':
            sql += " AND state IN ('pending','started','uncertain')"
            if exclude:
                sql += ' AND id!=?'
                args.append(exclude)
        for pid, stamp in conn.execute(sql, args):
            if pid and process_stamp(pid) == stamp:
                return True
    return False


def merge_guard(store, node, conn=None, pending=None):
    if not node['payload'].get('temporal_graph'):
        return
    if conn is None:
        with closing(store.connect()) as db:
            return merge_guard(store, node, db, pending)
    graphs = Graphs(store)
    graph = graphs.get(node['payload']['temporal_graph'], conn=conn)
    graphs.authority(conn, graph, admission=False)
    contract = graphs.contract(conn, graph)
    root = conn.execute('SELECT state FROM tasks WHERE owner=? AND id=?', (graph['owner'], graph['root'])).fetchone()
    if graph['stop_reason'] or graph['state'] in {'cancelling', 'cancelled'} or not root or root[0] in {'cancelled', 'failed'} or contract['deadline'] <= time.time():
        raise PermissionError('DAG merge fenced by cancellation or deadline')
    intent = conn.execute("SELECT id,state FROM temporal_dag_operations WHERE node_id=? AND action='merge'", (node['id'],)).fetchone()
    allowed = intent and ((intent['id'] == _permit.get() and intent['state'] == 'started') or
                          (intent['id'] == pending and intent['state'] == 'pending'))
    if intent and not allowed:
        raise PermissionError('DAG merge belongs to a durable lifecycle intent')


class Lifecycle:
    def __init__(self, graphs=None):
        self.graphs = graphs or Graphs()
        self.store = self.graphs.coordinator

    @property
    def queue(self):
        return self.graphs.queue + '-lifecycle-v1'

    @staticmethod
    def workflow_id(key):
        return 'pythonai/dag-operation/' + key

    def get(self, key):
        with closing(self.store.connect()) as conn:
            row = conn.execute('SELECT * FROM temporal_dag_operations WHERE id=?', (key,)).fetchone()
        if row is None:
            raise KeyError('DAG operation not found')
        return dict(row)

    def cancel_pending(self,key):
        with closing(self.store.connect()) as conn,conn:
            conn.execute("UPDATE temporal_dag_operations SET state='cancelled',pid=NULL,process=NULL,updated=? WHERE id=? AND state='pending'",(time.time(),key))

    def observe(self,key,status):
        if status not in {'CANCELED','TERMINATED','FAILED','TIMED_OUT','COMPLETED'}:return
        row=self.get(key)
        if row['state']=='started' and status!='COMPLETED':
            self.graphs.stop(row['graph_id'])
        self.cancel_pending(key)

    def request(self, graph_id, node_id, action):
        if action not in {'merge', 'archive'}:
            raise ValueError('Unknown DAG lifecycle action')
        graph = self.graphs.get(graph_id, operator=True)
        with closing(self.store.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_dag_gc'").fetchone() and conn.execute(
                    "SELECT 1 FROM temporal_dag_gc WHERE graph_id=?", (graph_id,)).fetchone():
                raise PermissionError('DAG is fenced by completed or pending History retention')
            self.graphs.authority(conn, graph, admission=False)
            node = self.store.get(graph['owner'], node_id)
            if node['root'] != graph['root']:
                raise PermissionError('Node belongs to another DAG')
            old = conn.execute('SELECT id FROM temporal_dag_operations WHERE graph_id=? AND node_id=? AND action=?',
                               (graph_id, node_id, action)).fetchone()
            if old:
                return old[0]
            if conn.execute('SELECT count(*) FROM temporal_dag_operations').fetchone()[0] >= 10000:
                raise ValueError('DAG operation capacity reached')
            snapshot = self.validate(node, action, conn)
            key = uuid.uuid4().hex
            conn.execute("INSERT INTO temporal_dag_operations VALUES(?,?,?,?,?,'pending',NULL,NULL,NULL,?)",
                         (key, graph_id, node_id, action, canonical(snapshot), time.time()))
            return key

    def validate(self, node, action, conn, pending=None):
        graph = self.graphs.get(node['payload']['temporal_graph'], conn=conn)
        anchor=self.graphs.authority(conn, graph, admission=False)
        for field in ('source','identity','session','project','role','repository','storage','budget_owner','budget_root','temporal_graph'):
            if node['payload'].get(field)!=anchor.get(field):
                raise PermissionError('Lifecycle node authority differs from its DAG')
        if live(conn, graph['id'], node['id'], pending):
            raise PermissionError('DAG node still has a live process')
        run = conn.execute('SELECT pid,process,state FROM temporal_agent_runs WHERE node_id=?', (node['id'],)).fetchone()
        if run and run['state'] in {'started', 'uncertain'} and process_stamp(run['pid']) == run['process']:
            raise PermissionError('DAG Activity has not drained')
        child = self.store.child_task(node)
        if (node['expires'] or 0) > time.time() or (child and (child['lease_until'] or 0) > time.time()):
            raise PermissionError('DAG node lease has not drained')
        if action == 'merge':
            merge_guard(self.store, node, conn, pending)
            if node['parent'] is not None or node['state'] != 'reviewed' or not node['reviewed_by']:
                raise PermissionError('Only a reviewed immediate-root Coder can be merged by this operator')
            review = self.store.get(graph['owner'], node['reviewed_by'])
            if review['review_of']!=node['id'] or review['root']!=node['root'] or review['payload'].get('temporal_graph')!=graph['id']:
                raise PermissionError('Reviewer belongs to another result')
            if review['state'] != 'completed' or not review['result'].get('verified') or review['result'].get('review') != {'approve': True, 'findings': []}:
                raise PermissionError('Independent Reviewer did not approve')
        else:
            if node['state'] not in STOPPED | {'merged', 'completed'}:
                raise PermissionError('Stop and inspect the node before archiving its worktree')
            from .dag import TERMINAL
            if graph['state'] not in TERMINAL:
                raise PermissionError('DAG must finish and all processes drain before archival')
            for other in self.graphs.nodes(conn,graph):
                if (other['expires'] or 0)>time.time() or live(conn,graph['id'],other['id'],pending):
                    raise PermissionError('DAG must finish and all processes drain before archival')
            for host in conn.execute("SELECT pid,process FROM temporal_agent_runs WHERE graph_id=? AND state IN ('started','uncertain')",(graph['id'],)):
                if process_stamp(host['pid'])==host['process']:
                    raise PermissionError('DAG Activity has not drained')
        worktrees = WorktreeStore(node['payload']['repository'], node['payload']['storage'])
        worktree = worktrees.get(graph['owner'], node['id'])
        if worktree['state'] != 'ready' or not worktrees.registered(Path(worktree['path'])):
            raise PermissionError('Worktree is no longer registered and ready')
        path = Path(worktree['path'])
        return {'base': worktree['base'], 'head': git(path, 'rev-parse', 'HEAD').strip(),
                'status': hashlib.sha256(git(path, 'status', '--porcelain=v1', '--untracked-files=all', '--ignored').encode()).hexdigest(),
                'content': fingerprint(path),
                'result': hashlib.sha256(canonical(node['result']).encode()).hexdigest(),
                'reviewer':node['reviewed_by'],
                'review':hashlib.sha256(canonical(review['result']).encode()).hexdigest() if action=='merge' else None}

    def execute(self, key):
        row = self.get(key)
        graph = self.graphs.get(row['graph_id'])
        with closing(self.store.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            assert_owner_active(conn, graph['owner'])
            current = self.get(key)
            if current['state'] in {'completed', 'failed', 'uncertain', 'cancelled'}:
                return {'operation_id': key}
            if current['state'] != 'pending':
                raise PermissionError('DAG lifecycle I/O was already started; inspect it, never replay')
            node = self.store.get(graph['owner'], row['node_id'])
            try:
                snapshot = self.validate(node, row['action'], conn, key)
                if canonical(snapshot) != row['snapshot']:
                    raise PermissionError('DAG lifecycle target changed after authorization')
            except Exception:
                conn.execute("UPDATE temporal_dag_operations SET state='failed',updated=? WHERE id=?", (time.time(), key))
                return {'operation_id': key}
            conn.execute("UPDATE temporal_dag_operations SET state='started',pid=?,process=?,updated=? WHERE id=? AND state='pending'",
                         (os.getpid(), process_stamp(os.getpid()), time.time(), key))
        p = node['payload']
        token = _permit.set(key)
        try:
            with execution_scope(source=p['source'], identity=p['identity'], session=p['session'], project=p['project'],
                    task_id=graph['root'], workspace=Path(p['workspace']), repository_workspace=None,
                    coordinator_node=None, coordinator_owner=None, lifecycle_owner=None,
                    check_cancelled=lambda: bool(self.graphs.get(graph['id'])['stop_reason']) if row['action']=='merge' else False), set_current_role('admin'):
                if row['action'] == 'merge':
                    from agent_execution.coordinator import merge_node
                    result = merge_node(self.store, graph['owner'], node['id'])
                else:
                    with closing(self.store.connect()) as conn:
                        assert_owner_active(conn, graph['owner'])
                    result = {'archive': str(WorktreeStore(p['repository'], p['storage']).archive(graph['owner'], node['id'],
                        expected_content=json.loads(row['snapshot'])['content']))}
            state = 'completed'
        except BaseException:
            with closing(self.store.connect()) as conn, conn:
                conn.execute("UPDATE temporal_dag_operations SET state='uncertain',pid=NULL,process=NULL,updated=? WHERE id=?", (time.time(), key))
            raise
        finally:
            _permit.reset(token)
        with closing(self.store.connect()) as conn, conn:
            conn.execute('UPDATE temporal_dag_operations SET state=?,result=?,updated=? WHERE id=?',
                         (state, canonical({'kind':row['action'],'digest':hashlib.sha256(canonical(result).encode()).hexdigest()}), time.time(), key))
        return {'operation_id': key}

    async def dispatch(self, key, client):
        from datetime import timedelta
        from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
        from temporalio.exceptions import WorkflowAlreadyStartedError
        row = self.get(key)
        graph = self.graphs.get(row['graph_id'])
        self.graphs.configuration(graph)
        with closing(self.store.connect()) as conn:
            assert_owner_active(conn, graph['owner'])
        if client.namespace != graph['namespace']:
            raise PermissionError('Wrong DAG lifecycle namespace')
        if row['state']!='pending':return client.get_workflow_handle(self.workflow_id(key))
        stamp=process_stamp(os.getpid())
        with closing(self.store.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            assert_owner_active(conn,graph['owner'])
            current=self.get(key)
            if current['state']!='pending':return client.get_workflow_handle(self.workflow_id(key))
            if current['pid'] and process_stamp(current['pid'])==current['process']:
                return client.get_workflow_handle(self.workflow_id(key))
            conn.execute('UPDATE temporal_dag_operations SET pid=?,process=? WHERE id=?',(os.getpid(),stamp,key))
        try:
            return await client.start_workflow('CoordinatorLifecycleV1', {'operation_id': key},
                id=self.workflow_id(key), task_queue=self.queue, execution_timeout=timedelta(seconds=360),
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE, rpc_timeout=timedelta(seconds=10))
        except WorkflowAlreadyStartedError:
            return client.get_workflow_handle(self.workflow_id(key))
        finally:
            with closing(self.store.connect()) as conn,conn:
                conn.execute("UPDATE temporal_dag_operations SET pid=NULL,process=NULL WHERE id=? AND state='pending' AND pid=? AND process=?",(key,os.getpid(),stamp))


def reap(graphs):
    """Fence dead owners without deleting files or reminting attempts/budgets."""
    store = graphs.coordinator
    with closing(store.connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        runs = conn.execute("SELECT * FROM temporal_agent_runs WHERE state IN ('started','uncertain') ORDER BY updated LIMIT 32").fetchall()
        for run in runs:
            if process_stamp(run['pid']) == run['process']:
                conn.execute('UPDATE temporal_agent_runs SET updated=? WHERE node_id=?',(time.time(),run['node_id']))
                continue
            if live(conn,run['graph_id'],run['node_id']):
                conn.execute("UPDATE agent_nodes SET state='cancel_requested',updated=? WHERE id=? AND state='running'",(time.time(),run['node_id']))
                conn.execute("UPDATE temporal_agent_runs SET state='uncertain',updated=? WHERE node_id=?",(time.time(),run['node_id']))
                continue
            node = store.get(graphs.get(run['graph_id'], conn=conn)['owner'], run['node_id'])
            if node['state'] in {'running', 'cancel_requested'}:
                conn.execute("UPDATE agent_nodes SET state='needs_resolution',lease=NULL,expires=NULL,updated=? WHERE id=?", (time.time(), node['id']))
                conn.execute("UPDATE tasks SET state='uncertain',lease_owner=NULL,lease_until=NULL WHERE owner=? AND id=? AND state IN ('running','suspended')",
                             (node_task_owner(node['payload']), 'subagent:'+node['id']))
            conn.execute("UPDATE temporal_agent_runs SET state='completed',updated=? WHERE node_id=?", (time.time(), node['id']))
        for op in conn.execute("SELECT * FROM temporal_dag_operations WHERE state='started' ORDER BY updated LIMIT 32").fetchall():
            if process_stamp(op['pid']) == op['process']:
                conn.execute('UPDATE temporal_dag_operations SET updated=? WHERE id=?',(time.time(),op['id']))
                continue
            conn.execute("UPDATE temporal_dag_operations SET state='uncertain',updated=? WHERE id=?", (time.time(), op['id']))
            conn.execute("UPDATE agent_nodes SET state='needs_resolution',lease=NULL,expires=NULL WHERE id=? AND state='merging'", (op['node_id'],))
