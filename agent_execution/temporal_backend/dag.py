"""SDK-free coordinator backend pins, local DAG authority and at-most-once node I/O."""
from contextlib import closing
import asyncio
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from agent_execution.coordinator import Coordinator,SUCCESS,STOPPED,node_task_owner,scope_owner
from agent_execution.policy import current_context,ExecutionContext,execution_scope
from agent_execution.tasks import task_owner,assert_owner_active
from permissions import current_role,get_user_role
from .store import canonical
from .config import Settings
from .dag_contracts import PARENT

QUEUE_SUFFIX='-dag-v1'
TERMINAL={'completed','needs_input','cancelled'}


class ConcurrencyFull(RuntimeError):
    """Known-no-effect admission wait; never a retry of node execution."""


def install(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS temporal_host(id INTEGER PRIMARY KEY CHECK(id=1),token TEXT NOT NULL)')
    conn.execute('INSERT OR IGNORE INTO temporal_host VALUES(1,?)',(uuid.uuid4().hex,))
    conn.execute('CREATE TABLE IF NOT EXISTS execution_bindings(owner TEXT NOT NULL,task_id TEXT NOT NULL,backend TEXT NOT NULL,PRIMARY KEY(owner,task_id))')
    for sql in [
        """CREATE TABLE IF NOT EXISTS agent_root_backends(
            owner TEXT NOT NULL,root TEXT NOT NULL,backend TEXT NOT NULL,PRIMARY KEY(owner,root))""",
        """CREATE TABLE IF NOT EXISTS temporal_agent_graphs(
            id TEXT PRIMARY KEY,owner TEXT NOT NULL,root TEXT NOT NULL,anchor TEXT NOT NULL,
            contract TEXT NOT NULL,address TEXT NOT NULL,namespace TEXT NOT NULL,queue TEXT NOT NULL,
            state TEXT NOT NULL,sealed INTEGER NOT NULL DEFAULT 0,dispatched INTEGER NOT NULL DEFAULT 0,
            stop_reason TEXT,updated REAL NOT NULL,UNIQUE(owner,root))""",
        """CREATE TABLE IF NOT EXISTS temporal_agent_runs(
            node_id TEXT PRIMARY KEY,graph_id TEXT NOT NULL,state TEXT NOT NULL,lease TEXT NOT NULL,
            pid INTEGER NOT NULL,process TEXT NOT NULL,updated REAL NOT NULL)""",
        'CREATE TABLE IF NOT EXISTS temporal_erased_dags(token TEXT PRIMARY KEY,session_hash TEXT NOT NULL,receipt TEXT NOT NULL)'
    ]:conn.execute(sql)
    if 'remote_deleted' not in {r[1] for r in conn.execute('PRAGMA table_info(temporal_agent_graphs)')}:
        conn.execute('ALTER TABLE temporal_agent_graphs ADD COLUMN remote_deleted INTEGER NOT NULL DEFAULT 0')
    from .dag_lifecycle import install as install_lifecycle
    install_lifecycle(conn)


def queue_name(conn):
    return 'pythonai-host-'+conn.execute('SELECT token FROM temporal_host WHERE id=1').fetchone()[0]+QUEUE_SUFFIX


def pin(conn,store,owner,root,key,payload,budget,review_of):
    old=conn.execute('SELECT backend FROM agent_root_backends WHERE owner=? AND root=?',(owner,root)).fetchone()
    backend=old[0] if old else None
    if backend is None:
        # An existing unpinned root is legacy; environment changes never migrate it.
        existing=conn.execute('SELECT 1 FROM agent_nodes WHERE owner=? AND root=?',(owner,root)).fetchone()
        backend='sqlite' if existing else os.environ.get('AGENT_COORDINATOR_BACKEND','sqlite').strip().lower()
        if backend not in {'sqlite','temporal'}:raise ValueError('Unknown coordinator backend')
        conn.execute('INSERT INTO agent_root_backends VALUES(?,?,?)',(owner,root,backend))
    if backend=='sqlite':return
    from .admission import new_backend
    if new_backend(owner,'temporal')!='temporal':raise PermissionError('DAG owner is outside the Temporal trial')
    if current_role()!='admin' or payload['role']!='admin':
        raise PermissionError('Temporal DAG requires explicit admin authority')
    if payload['budget_owner']!=owner or payload['budget_root']!=root:
        raise PermissionError('DAG must retain the original root budget')
    graph=conn.execute('SELECT * FROM temporal_agent_graphs WHERE owner=? AND root=?',(owner,root)).fetchone()
    if graph and graph['contract']!=canonical(budget.contract()):
        raise PermissionError('DAG root budget contract changed')
    if graph:
        settings=Settings.load()
        if (graph['address'],graph['namespace'],graph['queue'])!=(settings.address,settings.namespace,queue_name(conn)):
            raise PermissionError('DAG host configuration changed')
    if graph and (graph['state'] in TERMINAL|{'cancelling'} or (graph['sealed'] and not review_of)):
        raise PermissionError('DAG admission is closed; use a new root task')
    if conn.execute('SELECT count(*) FROM agent_nodes WHERE owner=? AND root=?',(owner,root)).fetchone()[0]>=128:
        raise ValueError('DAG is limited to 128 nodes including Reviewers')
    if not graph:
        context=current_context()
        if context.coordinator_node or context.coordinator_owner or context.lifecycle_owner:
            raise PermissionError('A delegated context cannot create a DAG root')
        if payload['source'] not in {'local','api','websocket','communication'}:
            raise PermissionError('Untrusted DAG source')
        if payload['identity']=='local' and payload['source']!='local':
            raise PermissionError('Invalid local DAG identity')
        if conn.execute('SELECT count(*) FROM temporal_agent_graphs').fetchone()[0]>=1000:
            raise ValueError('DAG capacity reached')
        settings=Settings.load()
        contract=budget.contract()
        if len(canonical(contract).encode())>65536:raise ValueError('DAG budget contract exceeds 64 KiB')
        if not 0<contract['deadline']-time.time()<=3600:
            raise ValueError('DAG root requires a remaining deadline within one hour')
        group=uuid.uuid4().hex
        queue=queue_name(conn)
        conn.execute("""INSERT INTO temporal_agent_graphs(
            id,owner,root,anchor,contract,address,namespace,queue,state,updated)
            VALUES(?,?,?,?,?,?,?,?,'pending',?)""",
            (group,owner,root,key,canonical(contract),settings.address,settings.namespace,queue,time.time()))
    else:group=graph['id']
    payload['temporal_graph']=group
    if not review_of:
        allowed=current_context().allowed_tools
        payload['parent_tools']=None if allowed is None else sorted(allowed)


def busy(conn,owners):
    from .references import process_stamp
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_agent_runs'").fetchone():return False
    for owner in owners:
        from .dag_lifecycle import live
        for graph_id, in conn.execute('SELECT id FROM temporal_agent_graphs WHERE owner=?', (owner,)):
            if live(conn, graph_id):return True
        for pid,stamp in conn.execute("""SELECT r.pid,r.process FROM temporal_agent_runs r
                JOIN temporal_agent_graphs g ON g.id=r.graph_id
                WHERE g.owner=? AND r.state IN ('started','uncertain')""",(owner,)):
            if process_stamp(pid)==stamp:return True
    return False


class Graphs:
    def __init__(self,coordinator=None):
        self.coordinator=coordinator or Coordinator()
        self.path=self.coordinator.path

    @property
    def queue(self):
        with closing(self.coordinator.connect()) as conn:return queue_name(conn)

    @staticmethod
    def workflow_id(key):
        return 'pythonai/dag/'+key

    @classmethod
    def child_id(cls,graph,node):
        return cls.workflow_id(graph)+'/node/'+node

    def get(self,key,*,operator=False,conn=None):
        if conn is None:
            with closing(self.coordinator.connect()) as db:return self.get(key,operator=operator,conn=db)
        row=conn.execute('SELECT * FROM temporal_agent_graphs WHERE id=?',(key,)).fetchone()
        if row is None:raise KeyError('DAG not found')
        row=dict(row)
        if operator and (current_role()!='admin' or row['owner']!=scope_owner() or
                current_context().coordinator_owner or current_context().coordinator_node or current_context().lifecycle_owner):
            raise PermissionError('DAG is outside this admin owner scope')
        return row

    def configuration(self,graph):
        settings=Settings.load()
        if (graph['address'],graph['namespace'],graph['queue'])!=(settings.address,settings.namespace,self.queue):
            raise PermissionError('DAG host configuration changed')

    def contract(self,conn,graph):
        row=conn.execute('SELECT deadline,limits FROM task_budgets WHERE owner=? AND id=?',
            (graph['owner'],graph['root'])).fetchone()
        if not row:raise PermissionError('DAG root budget disappeared')
        current={'schema_version':1,'root_ref':hashlib.sha256((graph['owner']+'\0'+graph['root']).encode()).hexdigest(),
            'deadline':row['deadline'],'limits':json.loads(row['limits'])}
        if canonical(current)!=graph['contract']:raise PermissionError('DAG root budget contract changed')
        return current

    def authority(self,conn,graph,*,admission=True):
        from agent_execution.sessions import project_workspace
        from agent_execution.worktrees import WorktreeStore
        self.configuration(graph)
        assert_owner_active(conn,graph['owner'])
        node=self.coordinator.get(graph['owner'],graph['anchor'])
        p=node['payload']
        if p.get('temporal_graph')!=graph['id'] or p.get('role')!='admin':
            raise PermissionError('DAG anchor authority changed')
        source=p['source']
        if source not in {'local','api','websocket','communication'} or (p['identity']=='local' and source!='local'):
            raise PermissionError('Invalid DAG source identity')
        if p['identity']!='local' and get_user_role(p['identity'])!='admin':
            raise PermissionError('DAG owner is no longer admin')
        saved=ExecutionContext(source=source,identity=p['identity'],session=p['session'],project=p['project'])
        if task_owner(saved)!=graph['owner'] or p['budget_owner']!=graph['owner'] or p['budget_root']!=graph['root']:
            raise PermissionError('DAG owner or budget binding changed')
        with execution_scope(workspace=None,repository_workspace=None):
            expected=project_workspace(p['project']).resolve()
        repository=Path(p['repository']).resolve()
        if expected!=repository:raise PermissionError('DAG project workspace changed')
        if str(WorktreeStore(repository).storage)!=p['storage']:
            raise PermissionError('DAG worktree storage changed')
        parent=Path(p['workspace']).resolve()
        if parent!=repository:
            binding=self.coordinator.tasks.workspace_binding(graph['owner'],graph['root'])
            if not binding or Path(binding['repository']).resolve()!=repository:
                raise PermissionError('Unregistered DAG parent worktree')
            worktrees=WorktreeStore(repository,binding['storage'])
            if parent!=worktrees.location(graph['owner'],graph['root']) or not worktrees.registered(parent):
                raise PermissionError('DAG parent worktree changed')
        if admission:
            from .admission import new_backend
            if new_backend(graph['owner'],'temporal')!='temporal':raise PermissionError('DAG trial admission closed')
        return p

    def nodes(self,conn,graph):
        return [self.coordinator.decode(r) for r in conn.execute(
            'SELECT * FROM agent_nodes WHERE owner=? AND root=? ORDER BY updated,id',(graph['owner'],graph['root']))]

    @staticmethod
    def ready(node,nodes):
        if node['state']!='queued':return False
        by_id={n['id']:n for n in nodes}
        return all(key in by_id and (by_id[key]['state'] in SUCCESS or
            (node['review_of']==key and by_id[key]['state']=='review_pending')) for key in node['dependencies'])

    def stop(self,key,reason='cancelled'):
        if reason not in {'cancelled','needs_input'}:raise ValueError('Invalid DAG stop reason')
        with closing(self.coordinator.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            graph=self.get(key,conn=conn)
            if graph['state'] in TERMINAL:
                if graph['state']=='completed':
                    conn.execute('UPDATE temporal_agent_graphs SET stop_reason=?,sealed=1,updated=? WHERE id=?',
                                 (reason,time.time(),key))
                return
            conn.execute("UPDATE temporal_agent_graphs SET sealed=1,state='cancelling',stop_reason=?,updated=? WHERE id=?",
                (reason,time.time(),key))
            for node in self.nodes(conn,graph):
                if node['state'] in {'queued','running','review_pending','merging'}:
                    state='cancel_requested' if node['lease'] else 'cancelled'
                    conn.execute('UPDATE agent_nodes SET state=?,updated=? WHERE id=?',(state,time.time(),node['id']))

    def seal(self,key):
        self.get(key,operator=True)
        with closing(self.coordinator.connect()) as conn,conn:
            conn.execute('UPDATE temporal_agent_graphs SET sealed=1,updated=? WHERE id=?',(time.time(),key))

    def cancel(self,key):
        self.get(key,operator=True)
        self.stop(key)

    def snapshot(self,key):
        with closing(self.coordinator.connect()) as conn:
            graph=self.get(key,conn=conn)
            if graph['state'] in TERMINAL:
                return {'done':True,'stop':graph['state']!='completed','ready':[],'concurrency':1}
            root=conn.execute('SELECT state FROM tasks WHERE owner=? AND id=?',(graph['owner'],graph['root'])).fetchone()
            try:
                self.authority(conn,graph)
                contract=self.contract(conn,graph)
                expired=contract['deadline']<=time.time()
            except (PermissionError,KeyError,ValueError):
                expired=True
        if not root or root['state'] in {'cancelled','failed'} or expired:
            self.stop(key,'cancelled' if root and root['state']=='cancelled' else 'needs_input')
        # Recover Reviewer creation only; native I/O is never replayed by lease expiry.
        with closing(self.coordinator.connect()) as conn:
            graph=self.get(key,conn=conn)
            nodes=self.nodes(conn,graph)
        if graph['state']!='cancelling':
            for node in nodes:
                if node['state']=='review_pending':self.coordinator.ensure_review(graph['owner'],node['id'])
        with closing(self.coordinator.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            graph=self.get(key,conn=conn)
            nodes=self.nodes(conn,graph)
            by_id={n['id']:n for n in nodes}
            for node in nodes:
                if node['state']=='queued' and any(k not in by_id or by_id[k]['state'] in STOPPED for k in node['dependencies']):
                    conn.execute("UPDATE agent_nodes SET state='failed',updated=? WHERE id=?",(time.time(),node['id']))
                    node['state']='failed'
            stop=graph['state']=='cancelling'
            terminal=all(n['state'] in SUCCESS|STOPPED for n in nodes)
            done=bool(terminal and (stop or graph['sealed'] or not root or root['state'] in {'completed','failed','cancelled'}))
            if done:
                state=graph['stop_reason'] or ('completed' if all(n['state'] in SUCCESS for n in nodes) else 'needs_input')
                conn.execute('UPDATE temporal_agent_graphs SET state=?,sealed=1,updated=? WHERE id=?',(state,time.time(),key))
                stop=state!='completed'
            concurrency=min(int(n['payload'].get('concurrency',1)) for n in nodes) if nodes else 1
            return {'done':done,'stop':stop,'ready':[] if stop or done else [n['id'] for n in nodes if self.ready(n,nodes)],
                'concurrency':concurrency}

    def claim(self,key,node_id):
        from .references import process_stamp
        with closing(self.coordinator.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            graph=self.get(key,conn=conn)
            self.authority(conn,graph)
            contract=self.contract(conn,graph)
            if graph['state'] not in {'pending','running'} or contract['deadline']<=time.time():
                raise PermissionError('DAG has stopped')
            nodes=self.nodes(conn,graph)
            node=next((n for n in nodes if n['id']==node_id),None)
            if node is None:raise PermissionError('Node belongs to another DAG')
            if conn.execute('SELECT 1 FROM temporal_agent_runs WHERE node_id=?',(node_id,)).fetchone():return None
            if not self.ready(node,nodes):raise PermissionError('DAG dependency has not succeeded')
            if node['deadline']<=time.time():raise PermissionError('Node deadline expired')
            anchor=self.coordinator.get(graph['owner'],graph['anchor'])['payload']
            p=node['payload']
            for name in ('source','identity','project','session','role','repository','storage','budget_owner','budget_root','temporal_graph'):
                if p.get(name)!=anchor.get(name):raise PermissionError('Node authority differs from root')
            if p['depth']>contract['limits']['depth']:raise PermissionError('Node depth changed')
            active=conn.execute("SELECT count(*) FROM agent_nodes WHERE state IN ('running','cancel_requested') AND expires>?",(time.time(),)).fetchone()[0]
            if active>=p['concurrency']:raise ConcurrencyFull('Coordinator concurrency is full')
            child_owner=node_task_owner(p)
            child_task='subagent:'+node_id
            binding=conn.execute('SELECT backend FROM execution_bindings WHERE owner=? AND task_id=?',(child_owner,child_task)).fetchone()
            if binding and binding[0]!='temporal':raise PermissionError('Legacy child task cannot migrate into this DAG')
            if not binding and conn.execute('SELECT 1 FROM tasks WHERE owner=? AND id=?',(child_owner,child_task)).fetchone():
                raise PermissionError('Existing child task cannot migrate into this DAG')
            conn.execute("INSERT OR IGNORE INTO execution_bindings VALUES(?,?,'temporal')",(child_owner,child_task))
            lease=uuid.uuid4().hex
            conn.execute("UPDATE agent_nodes SET state='running',lease=?,expires=?,attempts=attempts+1,updated=? WHERE id=?",
                (lease,time.time()+90,time.time(),node_id))
            conn.execute('INSERT INTO temporal_agent_runs VALUES(?,?,?, ?,?,?,?)',
                (node_id,key,'started',lease,os.getpid(),process_stamp(os.getpid()),time.time()))
            self.coordinator.event(conn,node,'temporal_claimed')
            node.update(state='running',lease=lease,expires=time.time()+90,attempts=node['attempts']+1)
            return node

    def node_stopped(self,key,node_id):
        with closing(self.coordinator.connect()) as conn,conn:
            graph=self.get(key,conn=conn)
            node=self.coordinator.get(graph['owner'],node_id)
            if node['root']!=graph['root']:raise PermissionError('Node belongs to another DAG')
            run=conn.execute('SELECT 1 FROM temporal_agent_runs WHERE node_id=?',(node_id,)).fetchone()
            if node['state'] in {'running','cancel_requested','queued'} or (node['state']=='failed' and run):
                conn.execute('UPDATE agent_nodes SET state=?,updated=? WHERE id=?',
                    ('needs_resolution' if run else 'cancelled',time.time(),node_id))
            conn.execute("UPDATE temporal_agent_runs SET state='uncertain',updated=? WHERE node_id=? AND state='started'",
                (time.time(),node_id))

    def process_done(self,key,node):
        with closing(self.coordinator.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            graph=self.get(key,conn=conn)
            current=self.coordinator.get(graph['owner'],node['id'])
            if current['root']!=graph['root']:raise PermissionError('Node belongs to another DAG')
            if current['state'] in {'running','cancel_requested','queued'}:
                state='cancelled' if current['state']=='cancel_requested' else 'needs_resolution'
                conn.execute("UPDATE agent_nodes SET state=?,updated=? WHERE id=? AND lease=?",
                    (state,time.time(),node['id'],node['lease']))
            conn.execute("UPDATE temporal_agent_runs SET state='completed',updated=? WHERE node_id=? AND lease=?",
                (time.time(),node['id'],node['lease']))
            conn.execute('UPDATE agent_nodes SET lease=NULL,expires=NULL WHERE id=? AND lease=?',
                (node['id'],node['lease']))

    async def dispatch(self,key,client=None):
        from datetime import timedelta
        from temporalio.common import WorkflowIDConflictPolicy,WorkflowIDReusePolicy
        from temporalio.exceptions import WorkflowAlreadyStartedError
        from .client import connect
        graph=self.get(key)
        self.configuration(graph)
        if graph['state'] in TERMINAL:return None
        with closing(self.coordinator.connect()) as conn:
            contract=self.contract(conn,graph)
        if contract['deadline']<=time.time():
            self.stop(key,'needs_input')
            return None
        client=client or await connect(Settings.load())
        if client.namespace!=graph['namespace']:raise PermissionError('Wrong DAG namespace')
        try:
            handle=await client.start_workflow(PARENT,{'graph_id':key},id=self.workflow_id(key),
                task_queue=graph['queue'],execution_timeout=timedelta(seconds=contract['deadline']-time.time()+60),
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                rpc_timeout=timedelta(seconds=10))
        except WorkflowAlreadyStartedError:
            handle=client.get_workflow_handle(self.workflow_id(key))
        with closing(self.coordinator.connect()) as conn,conn:
            conn.execute("UPDATE temporal_agent_graphs SET dispatched=1,state=CASE WHEN state='pending' THEN 'running' ELSE state END,updated=? WHERE id=?",
                (time.time(),key))
        return handle


async def reconcile(graphs,client):
    from temporalio.client import WorkflowExecutionStatus
    from datetime import timedelta
    from .dag_lifecycle import reap,Lifecycle
    await asyncio.to_thread(reap,graphs)
    life=Lifecycle(graphs)
    with closing(graphs.coordinator.connect()) as conn:
        operations=[r[0] for r in conn.execute("SELECT id FROM temporal_dag_operations WHERE state IN ('pending','started') ORDER BY updated LIMIT 8")]
    lifecycle_deadline=time.monotonic()+10
    for operation in operations:
        if time.monotonic()>=lifecycle_deadline:break
        try:
            handle=await life.dispatch(operation,client)
            status=(await handle.describe(rpc_timeout=timedelta(seconds=10))).status
            await asyncio.to_thread(life.observe,operation,status.name)
        except Exception as error:
            with closing(graphs.coordinator.connect()) as conn,conn:
                conn.execute('UPDATE temporal_dag_operations SET updated=? WHERE id=?',(time.time(),operation))
            print(canonical({'temporal':'dag_lifecycle_pending','error_type':type(error).__name__}),flush=True)
    with closing(graphs.coordinator.connect()) as conn:
        keys=[r[0] for r in conn.execute("SELECT id FROM temporal_agent_graphs WHERE state IN ('pending','running','cancelling') ORDER BY updated LIMIT 32")]
    deadline=time.monotonic()+10
    for key in keys:
        if time.monotonic()>=deadline:break
        try:
            await asyncio.to_thread(graphs.snapshot,key)
            graph=graphs.get(key)
            if graph['state'] in TERMINAL:continue
            if not graph['dispatched']:
                await graphs.dispatch(key,client)
            else:
                graphs.configuration(graph)
                status=(await client.get_workflow_handle(graphs.workflow_id(key)).describe(rpc_timeout=timedelta(seconds=10))).status
                if status in {WorkflowExecutionStatus.CANCELED,WorkflowExecutionStatus.TERMINATED,WorkflowExecutionStatus.FAILED,WorkflowExecutionStatus.TIMED_OUT}:
                    graphs.stop(key,'needs_input')
            # Local fences also apply when the remote worker/service is unavailable.
            await asyncio.to_thread(graphs.snapshot,key)
        except Exception as error:
            with closing(graphs.coordinator.connect()) as conn,conn:
                conn.execute('UPDATE temporal_agent_graphs SET updated=? WHERE id=?',(time.time(),key))
            print(canonical({'temporal':'dag_pending','error_type':type(error).__name__}),flush=True)
