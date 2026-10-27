"""Persistent collaboration graph. SQL leases fence workers and file delivery."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
import uuid

from .budgets import TaskBudget,BudgetExhausted,active_budget,_depth
from .policy import current_context,execution_scope,PolicyDenied
from .tasks import TaskStore,task_owner,_active
from .worktrees import git,selected_files,snapshot_files,WorktreeStore
from .process import run_process


PERSONAS = frozenset(('planner', 'coder', 'reviewer'))
READ_TOOLS=frozenset({'read_file','list_files','git_status','git_diff','search_codebase','code_symbols','go_to_definition','find_references','read_image'})
TOOLS_BY_ROLE={
    'planner':READ_TOOLS,
    'reviewer':READ_TOOLS|{'run_test_suite'},
    'coder':READ_TOOLS|{'write_file','edit_file','ast_edit','run_command','run_test_suite','git_commit',
        'ask_user','run_skill','invoke_subagent','check_subagent','manage_subagent',
        'prepare_project_patch','apply_project_patch','project_delivery','refresh_code_index'},
}
SUCCESS={'completed','reviewed','merged'}
STOPPED={'failed','rejected','cancelled','timed_out','needs_input','needs_resolution','conflict'}


def scope_owner():
    return current_context().coordinator_owner or task_owner(current_context())


def node_task_owner(payload):
    return json.dumps(['subagent',payload['identity'],payload['project'],payload['session']],separators=(',',':'))


def snapshot_commit(repository):
    """Snapshot working bytes using a private index; source HEAD/index stay untouched."""
    root=Path(repository).resolve()
    candidates=git(root,'ls-files','--cached','--others','--exclude-standard','-z').split('\x00')
    total=0;count=0
    for name in set(candidates)-{''}:
        path=root/name
        if root not in path.resolve().parents:raise ValueError('Snapshot path escapes workspace')
        for component in (path,*path.parents):
            if component==root:break
            if component.is_symlink() or (hasattr(component,'is_junction') and component.is_junction()):raise ValueError('Snapshot links require manual preservation')
        if not path.exists():continue
        if not path.is_file():raise ValueError('Snapshot requires regular files')
        size=path.stat().st_size;total+=size;count+=1
        if size>64*1024*1024 or total>256*1024*1024 or count>10000:raise ValueError('Snapshot exceeds file/count/size limits')
    with tempfile.TemporaryDirectory(prefix='pythonai-snapshot-') as directory:
        env={**os.environ,'GIT_INDEX_FILE':str(Path(directory)/'index'),'GIT_LITERAL_PATHSPECS':'1',
            'GIT_AUTHOR_NAME':'PythonAI','GIT_AUTHOR_EMAIL':'agent@example.invalid',
            'GIT_COMMITTER_NAME':'PythonAI','GIT_COMMITTER_EMAIL':'agent@example.invalid'}
        head=git(repository,'rev-parse','HEAD').strip()
        git(repository,'read-tree',head,env=env)
        git(repository,'add','--all','--','.',env=env)
        # No links/submodules may be delivered as ordinary files.
        entries=git(repository,'ls-files','--stage','-z',env=env).split('\x00')
        if any(entry and entry.split()[0] not in {'100644','100755'} for entry in entries):
            raise ValueError('Collaboration snapshots require regular files')
        tree=git(repository,'write-tree',env=env).strip()
        return git(repository,'commit-tree',tree,'-p',head,'-m','PythonAI collaboration snapshot',env=env).strip()


class Coordinator:
    def __init__(self,path=None):
        self.tasks=TaskStore(path)
        self.path=self.tasks.path
        with closing(self.connect()) as conn,conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS agent_nodes (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, root TEXT NOT NULL, parent TEXT,
                    persona TEXT NOT NULL, goal TEXT NOT NULL, dependencies TEXT NOT NULL,
                    payload TEXT NOT NULL, state TEXT NOT NULL, result TEXT,
                    review_of TEXT, reviewed_by TEXT, lease TEXT, expires REAL,
                    deadline REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_events (
                    id INTEGER PRIMARY KEY, node TEXT NOT NULL, owner TEXT NOT NULL,
                    event TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_agent_review ON agent_nodes(review_of) WHERE review_of IS NOT NULL;
            ''')
            from .temporal_backend.dag import install
            install(conn)

    def connect(self):
        conn=sqlite3.connect(self.path,timeout=10);conn.row_factory=sqlite3.Row;return conn

    @staticmethod
    def decode(row):
        value=dict(row)
        for key in ('payload','dependencies','result'):
            value[key]=json.loads(value[key]) if value[key] is not None else None
        return value

    def get(self,owner,key):
        if not isinstance(key,str) or not re.fullmatch('[0-9a-f]{32}',key):raise ValueError('Invalid agent ID')
        with closing(self.connect()) as conn:
            row=conn.execute('SELECT * FROM agent_nodes WHERE owner=? AND id=?',(owner,key)).fetchone()
        if row is None:raise KeyError(key)
        return self.decode(row)

    @staticmethod
    def capacity(conn,extra):
        used=conn.execute('SELECT COALESCE(SUM(length(CAST(payload AS BLOB))+length(CAST(goal AS BLOB))+length(CAST(COALESCE(result,\'\') AS BLOB))),0) FROM agent_nodes').fetchone()[0]
        used+=conn.execute('SELECT COALESCE(SUM(length(CAST(data AS BLOB))),0) FROM agent_events').fetchone()[0]
        if used+extra>64*1024*1024:raise ValueError('Agent logical storage exceeds 64 MiB')

    @staticmethod
    def event(conn,row,event,data=None):
        Coordinator.capacity(conn,len(json.dumps(data or {},ensure_ascii=False).encode()))
        conn.execute('INSERT INTO agent_events(node,owner,event,data,created) VALUES(?,?,?,?,?)',
            (row['id'],row['owner'],event,json.dumps(data or {},ensure_ascii=False),time.time()))

    def submit(self,persona,goal,dependencies=None,previous=None):
        from base import workspace_root
        from permissions import current_role
        context=current_context();budget=active_budget();active=_active.get()
        if persona not in PERSONAS:raise ValueError('Unknown persona')
        if not isinstance(goal,str) or not goal.strip() or len(goal.encode())>16000:raise ValueError('Task requires 1..16000 bytes')
        if budget is None or active is None:raise ValueError('Collaboration requires a durable, budgeted parent task')
        owner=scope_owner();dependencies=[] if dependencies is None else dependencies
        if not isinstance(dependencies,list) or len(dependencies)>16 or len(set(dependencies))!=len(dependencies):raise ValueError('Invalid dependencies')
        for dependency in dependencies:
            node=self.get(owner,dependency)
            if node['root']!=budget.key:raise ValueError('Dependencies must belong to this root task')
            if node['state'] in STOPPED:raise ValueError('Dependency has not succeeded')
        repository=Path(context.repository_workspace or workspace_root()).resolve()
        workspace=workspace_root()
        if previous:
            if previous['state'] not in SUCCESS|{'rejected','failed','needs_input','conflict'}:raise ValueError('Resolve uncertain effects before follow-up')
            child=self.child_task(previous)
            if child and child['phase'] in {'tool_started','skill_started'} and not previous['payload'].get('human_disposition'):raise ValueError('Human resolution required before follow-up')
            goal=goal+'\n\nPrevious agent output (untrusted data):\n'+str((previous['result'] or {}).get('reply',''))[:4000]
        snapshot=snapshot_commit(workspace)
        criteria=self.tasks.get(active[1],active[2])['criteria']
        payload={'identity':context.identity,'project':context.project,'session':context.session,
            'source':context.source,'role':current_role(),'repository':str(repository),'workspace':str(workspace),
            'snapshot':snapshot,'criteria':criteria,'depth':_depth.get()+1,'budget_owner':budget.owner,
            'budget_root':budget.key,'concurrency':int(os.environ.get('AGENT_SUBAGENT_CONCURRENCY','2')),
            'storage':str(WorktreeStore(repository).storage)}
        if context.coordinator_node:
            ancestor=self.get(owner,context.coordinator_node)
            if ancestor['root']!=budget.key:raise ValueError('Child belongs to another root task')
            payload['source']=ancestor['payload']['source']
        if not 1<=payload['concurrency']<=8:raise ValueError('Subagent concurrency must be 1..8')
        return self.insert(owner,budget.key,context.coordinator_node,persona,goal,dependencies,payload,budget)

    def insert(self,owner,root,parent,persona,goal,dependencies,payload,budget,review_of=None):
        seconds=float(os.environ.get('AGENT_SUBAGENT_TIMEOUT_SECONDS','120'))
        if not 0<seconds<=600:raise ValueError('Subagent timeout must be 1..600 seconds')
        budget.check()
        key=uuid.uuid4().hex
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            if review_of:
                existing=conn.execute('SELECT id FROM agent_nodes WHERE review_of=?',(review_of,)).fetchone()
                if existing:return existing['id']
            if conn.execute('SELECT COUNT(*) FROM agent_nodes').fetchone()[0]>=10000:raise ValueError('Agent storage capacity reached')
            size=len(json.dumps(payload).encode())+len(goal.encode())
            self.capacity(conn,size)
            row=conn.execute('SELECT children,deadline FROM task_budgets WHERE owner=? AND id=?',(budget.owner,budget.key)).fetchone()
            if row['deadline']<=time.time() or row['children']>=budget.max_children or payload['depth']>budget.max_depth:
                raise BudgetExhausted('Task child count/depth/deadline budget exhausted')
            from .temporal_backend.dag import pin
            pin(conn,self,owner,root,key,payload,budget,review_of)
            conn.execute('UPDATE task_budgets SET children=children+1 WHERE owner=? AND id=?',(budget.owner,budget.key))
            conn.execute('INSERT INTO agent_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key,owner,root,parent,persona,goal,json.dumps(dependencies),json.dumps(payload),'queued',None,review_of,None,None,None,
                 min(row['deadline'],time.time()+seconds),0,time.time()))
            self.event(conn,{'id':key,'owner':owner},'submitted',{'persona':persona,'dependencies':dependencies})
        return key

    def child_task(self,node):
        try:return self.tasks.get(node_task_owner(node['payload']),'subagent:'+node['id'])
        except KeyError:return None

    def cancelled(self,node,lease=None):
        with closing(self.connect()) as conn:
            row=conn.execute('SELECT state,lease,expires,deadline FROM agent_nodes WHERE id=?',(node['id'],)).fetchone()
            root=conn.execute('SELECT state FROM tasks WHERE owner=? AND id=?',(node['payload']['budget_owner'],node['root'])).fetchone()
        if row is None or row['state'] not in {'running','merging'} or (lease and row['lease']!=lease):return True
        if row['deadline']<=time.time() or (lease and row['expires']<=time.time()):return True
        return root is None or root['state'] in {'cancelled','failed'}

    def recover(self):
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            rows=conn.execute("""SELECT * FROM agent_nodes WHERE state IN ('queued','running','cancel_requested','merging')
                AND NOT EXISTS(SELECT 1 FROM agent_root_backends b WHERE b.owner=agent_nodes.owner
                    AND b.root=agent_nodes.root AND b.backend='temporal')""").fetchall()
            for raw in rows:
                node=self.decode(raw);child=self.child_task(node)
                try:root=self.tasks.get(node['payload']['budget_owner'],node['root'])
                except KeyError:root={'state':'cancelled'}
                stop=root['state'] in {'cancelled','failed'} or node['deadline']<=time.time()
                live=node['expires'] and node['expires']>time.time()
                if node['state']=='merging':
                    if live:continue
                    state='needs_resolution'
                elif stop or node['state']=='cancel_requested':
                    state='cancel_requested' if live else 'timed_out' if node['deadline']<=time.time() else 'cancelled'
                elif node['state']=='running' and not live:
                    if child and child['phase'] in {'tool_started','skill_started'}:state='needs_resolution'
                    elif child and child['state']=='suspended':state='needs_input'
                    elif child and child['state'] in {'failed','cancelled'}:state=child['state']
                    elif node['attempts']>=3:state='failed'
                    else:
                        state='queued'
                        self.fence_child(conn,node)
                else:continue
                conn.execute('UPDATE agent_nodes SET state=?,updated=? WHERE id=?',(state,time.time(),node['id']))
                self.event(conn,node,'recovery',{'state':state})
            conn.execute("UPDATE agent_nodes SET state='rejected',updated=? WHERE state='review_pending' AND id IN "
                "(SELECT review_of FROM agent_nodes WHERE review_of IS NOT NULL AND state IN ('failed','rejected','cancelled','timed_out','needs_input','needs_resolution'))",(time.time(),))

    @staticmethod
    def fence_child(conn,node):
        conn.execute("UPDATE tasks SET state='pending',lease_owner=NULL,lease_until=NULL,reply='Coordinator recovered a stopped worker' "
            "WHERE owner=? AND id=? AND state='running' AND phase NOT IN ('tool_started','skill_started')",
            (node_task_owner(node['payload']),'subagent:'+node['id']))

    def claim(self):
        self.recover()
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            for raw in conn.execute("""SELECT * FROM agent_nodes WHERE state='queued' AND NOT EXISTS(
                    SELECT 1 FROM agent_root_backends b WHERE b.owner=agent_nodes.owner
                    AND b.root=agent_nodes.root AND b.backend='temporal') ORDER BY updated LIMIT 128""").fetchall():
                node=self.decode(raw)
                child=self.child_task(node)
                if child and child['lease_until'] and child['lease_until']>time.time():continue
                count=conn.execute("SELECT COUNT(*) FROM agent_nodes WHERE state IN ('running','cancel_requested') AND expires>?",(time.time(),)).fetchone()[0]
                if count>=node['payload']['concurrency']:continue
                dependencies=[self.decode(r) for key in node['dependencies'] for r in conn.execute('SELECT * FROM agent_nodes WHERE id=? AND owner=?',(key,node['owner'])).fetchall()]
                if any(d['state'] in STOPPED for d in dependencies):
                    conn.execute("UPDATE agent_nodes SET state='failed' WHERE id=?",(node['id'],));continue
                if len(dependencies)!=len(node['dependencies']):continue
                if any(d['state'] not in SUCCESS and not (node['review_of']==d['id'] and d['state']=='review_pending') for d in dependencies):continue
                token=uuid.uuid4().hex
                conn.execute("UPDATE agent_nodes SET state='running',lease=?,expires=?,attempts=attempts+1,updated=? WHERE id=?",(token,time.time()+90,time.time(),node['id']))
                self.event(conn,node,'claimed')
                node.update(state='running',lease=token,expires=time.time()+90,attempts=node['attempts']+1)
                return node
        return None

    def renew(self,node):
        with closing(self.connect()) as conn,conn:
            return conn.execute("UPDATE agent_nodes SET expires=? WHERE id=? AND lease=? AND state='running' AND expires>?",
                (time.time()+90,node['id'],node['lease'],time.time())).rowcount==1

    def publish(self,node,result):
        if not isinstance(result,dict) or len(json.dumps(result).encode())>1024*1024:raise ValueError('Invalid agent result')
        verified=result.get('verified') is True and result.get('success') is True
        if node['persona']=='reviewer' and node['payload'].get('temporal_graph'):
            from .coordinator_worker import review_result
            try:review_result(json.dumps(result.get('review')))
            except (ValueError,TypeError):verified=False
        state='review_pending' if node['persona']=='coder' and verified else 'completed' if node['persona']=='planner' and result.get('success') is True else 'rejected' if node['persona']=='reviewer' else 'needs_input'
        if node['persona']=='reviewer' and verified and result.get('review',{}).get('approve') is True and result['review'].get('findings')==[]:state='completed'
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            root=conn.execute('SELECT state FROM tasks WHERE owner=? AND id=?',(node['payload']['budget_owner'],node['root'])).fetchone()
            if root is None or root['state'] in {'cancelled','failed'} or node['deadline']<=time.time():raise RuntimeError('Parent task stopped or agent deadline expired')
            old=conn.execute('SELECT length(CAST(COALESCE(result,\'\') AS BLOB)) FROM agent_nodes WHERE id=?',(node['id'],)).fetchone()
            self.capacity(conn,len(json.dumps(result,ensure_ascii=False).encode())-(old[0] if old else 0))
            if conn.execute("UPDATE agent_nodes SET state=?,result=?,lease=NULL,expires=NULL,updated=? WHERE id=? AND state='running' AND lease=? AND expires>?",
                (state,json.dumps(result,ensure_ascii=False),time.time(),node['id'],node['lease'],time.time())).rowcount!=1:raise RuntimeError('Agent lease lost')
            self.event(conn,node,'finished',{'state':state})
            if node['review_of']:
                conn.execute("UPDATE agent_nodes SET state=?,reviewed_by=?,updated=? WHERE id=? AND state='review_pending'",
                    ('reviewed' if state=='completed' else 'rejected',node['id'],time.time(),node['review_of']))
        if state=='review_pending':self.ensure_review(node['owner'],node['id'])

    def ensure_review(self,owner,key):
        node=self.get(owner,key)
        if node['state']!='review_pending':return
        with closing(self.connect()) as conn:
            if conn.execute('SELECT 1 FROM agent_nodes WHERE review_of=?',(key,)).fetchone():return
        payload={**node['payload'],'snapshot':node['result']['commit']}
        budget=TaskBudget(payload['budget_owner'],node['root'],self.path)
        try:self.insert(owner,node['root'],node['parent'],'reviewer','Evaluate the supplied result and return review JSON.',[key],payload,budget,review_of=key)
        except (ValueError,BudgetExhausted):
            with closing(self.connect()) as conn,conn:
                conn.execute("UPDATE agent_nodes SET state='needs_input' WHERE id=? AND state='review_pending'",(key,))

    def cancel(self,owner,key):
        self.get(owner,key)
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            selected={key}
            while True:
                extra={r['id'] for r in conn.execute('SELECT id,parent,review_of FROM agent_nodes WHERE owner=?',(owner,)) if r['parent'] in selected or r['review_of'] in selected}
                if extra<=selected:break
                selected.update(extra)
            for item in selected:
                raw=conn.execute('SELECT * FROM agent_nodes WHERE id=?',(item,)).fetchone();node=self.decode(raw)
                if node['state']=='merged':continue
                state='cancel_requested' if node['lease'] and node['expires']>time.time() else 'cancelled'
                conn.execute('UPDATE agent_nodes SET state=?,updated=? WHERE id=?',(state,time.time(),item))
                self.event(conn,node,'cancelled')

    def fail(self,node,state,error_type=''):
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT state FROM agent_nodes WHERE id=? AND lease=?',(node['id'],node['lease'])).fetchone()
            if row and row['state'] in {'running','cancel_requested'}:
                if row['state']=='cancel_requested':state='cancelled'
                if state=='queued':self.fence_child(conn,node)
                if state=='needs_resolution':
                    conn.execute("UPDATE tasks SET state='uncertain',lease_owner=NULL,lease_until=NULL WHERE owner=? AND id=? AND state='running' AND phase IN ('tool_started','skill_started')",
                        (node_task_owner(node['payload']),'subagent:'+node['id']))
                conn.execute('UPDATE agent_nodes SET state=?,lease=NULL,expires=NULL,updated=? WHERE id=? AND lease=?',
                    (state,time.time(),node['id'],node['lease']))
                self.event(conn,node,'worker_stopped',{'state':state,'error_type':error_type})
                if node['review_of'] and state in STOPPED:
                    conn.execute("UPDATE agent_nodes SET state='rejected',reviewed_by=?,updated=? WHERE id=? AND state='review_pending'",(node['id'],time.time(),node['review_of']))


class CoordinatorService:
    """Supervisor owns process lifetime; workers own checkpointed model execution."""
    def __init__(self,coordinator,worker_command=None):
        import sys
        self.store=coordinator
        self.command=worker_command or [sys.executable,'-m','agent_execution.coordinator_worker']
        self.stop=threading.Event();self.lock=threading.Lock();self.workers={};self.thread=None

    def run_node(self,node):
        lost=threading.Event();done=threading.Event()
        def heartbeat():
            while not done.wait(15):
                if not self.store.renew(node):lost.set();return
        watch=threading.Thread(target=heartbeat,daemon=True);watch.start()
        try:
            command=[*self.command,'--db',str(self.store.path),'--node',node['id'],'--lease',node['lease']]
            result=run_process(command,cwd=Path(__file__).resolve().parents[1],timeout=max(.1,node['deadline']-time.time()),
                check_cancelled=lambda:self.stop.is_set() or lost.is_set() or self.store.cancelled(node,node['lease']))
            if result.returncode:
                child=self.store.child_task(node)
                state='needs_resolution' if child and child['phase'] in {'tool_started','skill_started'} else 'needs_input' if child and child['state']=='suspended' else 'timed_out' if result.returncode==124 else 'queued' if node['attempts']<3 and (result.returncode not in {130,125} or self.stop.is_set()) else 'cancelled' if result.returncode==130 else 'failed'
                self.store.fail(node,state)
        except Exception:
            self.store.fail(node,'failed')
        finally:
            done.set();watch.join(1)
            with self.lock:self.workers.pop(node['id'],None)

    def tick(self):
        with closing(self.store.connect()) as conn:
            waiting=conn.execute("""SELECT owner,id FROM agent_nodes WHERE state='review_pending' AND NOT EXISTS(
                SELECT 1 FROM agent_root_backends b WHERE b.owner=agent_nodes.owner
                AND b.root=agent_nodes.root AND b.backend='temporal')""").fetchall()
        for row in waiting:self.store.ensure_review(row['owner'],row['id'])
        while not self.stop.is_set():
            node=self.store.claim()
            if node is None:break
            thread=threading.Thread(target=self.run_node,args=(node,),daemon=True)
            with self.lock:self.workers[node['id']]=thread
            thread.start()

    def start(self):
        if self.thread and self.thread.is_alive():return self
        def loop():
            last_error=None
            while not self.stop.is_set():
                try:self.tick();last_error=None
                except Exception as error:
                    error_type=type(error).__name__
                    if error_type!=last_error:print(json.dumps({'coordinator':'scheduler_error','error_type':error_type}),flush=True)
                    last_error=error_type
                self.stop.wait(.2)
        self.thread=threading.Thread(target=loop,daemon=True);self.thread.start();return self

    def close(self,timeout=5):
        self.stop.set()
        if self.thread:self.thread.join(min(timeout,1))
        deadline=time.monotonic()+timeout
        with self.lock:workers=list(self.workers.values())
        for thread in workers:thread.join(max(0,deadline-time.monotonic()))


_services={}
_services_lock=threading.Lock()


def start_coordinator(path=None):
    store=Coordinator(path)
    with _services_lock:
        service=_services.get(str(store.path))
        if service is None or service.stop.is_set():
            service=CoordinatorService(store).start();_services[str(store.path)]=service
    return service


def resume_coordinator():
    from .tasks import TaskStore
    store=TaskStore()
    with closing(store.connect()) as conn:
        table=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_nodes'").fetchone()
        active=False
        if table:
            query="SELECT 1 FROM agent_nodes WHERE state IN ('queued','running','review_pending','cancel_requested')"
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='agent_root_backends'").fetchone():
                query+=" AND NOT EXISTS(SELECT 1 FROM agent_root_backends b WHERE b.owner=agent_nodes.owner AND b.root=agent_nodes.root AND b.backend='temporal')"
            active=conn.execute(query+' LIMIT 1').fetchone()
    return start_coordinator(store.path) if active else None


def close_coordinator():
    path=str(TaskStore().path)
    with _services_lock:service=_services.pop(path,None)
    if service:service.close()


def wait_node(store,owner,key,seconds):
    if type(seconds) not in {int,float} or not 0<=seconds<=30:raise ValueError('Wait must be 0..30 seconds')
    until=time.monotonic()+seconds
    while True:
        node=store.get(owner,key)
        if node['state'] in SUCCESS|STOPPED or time.monotonic()>=until:return node
        budget=active_budget()
        if budget:budget.check()
        cancelled=current_context().check_cancelled
        if cancelled and cancelled():raise InterruptedError('Parent cancelled while waiting')
        time.sleep(.05)


def blob(repository,commit,name):
    result=run_process(['git','show',commit+':'+name],cwd=repository,text=False,max_output_bytes=32*1024*1024)
    if result.returncode:raise ValueError('Cannot read bounded commit blob')
    return result.stdout


def merge_node(store,owner,key):
    from base import workspace_root
    from permissions import current_role
    node=store.get(owner,key)
    if node['state']=='merged':return node['result']['merge']
    if current_role()!='admin':raise PolicyDenied('Merging requires admin role')
    from .temporal_backend.dag_lifecycle import merge_guard
    if node['payload'].get('temporal_graph'):
        from .temporal_backend.dag import Graphs
        graphs=Graphs(store)
        with closing(store.connect()) as conn:
            graph=graphs.get(node['payload']['temporal_graph'],conn=conn)
            if graph['state'] in {'cancelling','cancelled'} or graph['stop_reason']:
                raise ValueError('DAG stopped; no new merge is allowed')
            graphs.authority(conn,graph,admission=False)
            graphs.contract(conn,graph)
    merge_guard(store,node)
    TaskBudget(node['payload']['budget_owner'],node['root'],store.path).check()
    parent=store.tasks.get(node['payload']['budget_owner'],node['root'])
    if parent['state'] in {'cancelled','failed'}:raise ValueError('Parent task has stopped')
    if node['state']!='reviewed' or not node['reviewed_by']:raise ValueError('Coder result has not passed its independent Reviewer')
    reviewer=store.get(owner,node['reviewed_by'])
    if reviewer['state']!='completed' or not reviewer['result']['verified'] or reviewer['result'].get('review')!={'approve':True,'findings':[]}:
        raise ValueError('Reviewer has not approved verified evidence')
    if node['parent']!=current_context().coordinator_node:raise ValueError('Only the immediate parent may merge this result')
    root=workspace_root()
    if root!=Path(node['payload']['workspace']).resolve():raise ValueError('Parent workspace changed')
    result=node['result'];repository=node['payload']['repository']
    records=git(repository,'diff','--name-status','--no-renames','-z',result['base'],result['commit'],'--').split('\x00')
    changes=[];total=0
    for offset in range(0,len(records)-1,2):
        status,name=records[offset:offset+2]
        if status not in {'A','M','D'}:raise ValueError('Unsupported merge file type')
        selected=selected_files(root,[name])[0];path=root/selected
        old=blob(repository,result['base'],name) if status!='A' else None
        new=blob(repository,result['commit'],name) if status!='D' else None
        current=path.read_bytes() if path.exists() else None
        if current!=old:
            with closing(store.connect()) as conn,conn:
                conn.execute("UPDATE agent_nodes SET state='conflict' WHERE id=? AND state='reviewed'",(key,))
            raise ValueError('Merge conflict; parent content changed: '+selected)
        total+=len(new or b'')+len(old or b'')
        if total>32*1024*1024 or len(changes)>=128:raise ValueError('Merge exceeds 128 files / 32 MiB')
        changes.append((selected,old,new))
    archive=Path(node['payload']['storage'])/'merges'/uuid.uuid4().hex
    archive.mkdir(parents=True)
    if changes:snapshot_files(root,archive/'before.zip',[change[0] for change in changes])
    token=uuid.uuid4().hex
    with closing(store.connect()) as conn,conn:
        conn.execute('BEGIN IMMEDIATE')
        merge_guard(store,node,conn)
        if conn.execute("UPDATE agent_nodes SET state='merging',lease=?,expires=? WHERE id=? AND state='reviewed'",(token,time.time()+90,key)).rowcount!=1:
            raise ValueError('Merge already owned or result changed')
        store.event(conn,node,'merge_started',{'archive':str(archive)})
    applied=[]
    try:
        for name,old,new in changes:
            merge_guard(store,node)
            if current_context().check_cancelled and current_context().check_cancelled():raise InterruptedError('Parent cancelled')
            path=root/name
            if (path.read_bytes() if path.exists() else None)!=old:raise ValueError('Merge conflict during apply')
            if new is None:path.unlink()
            else:
                path.parent.mkdir(parents=True,exist_ok=True)
                temporary=path.with_name(path.name+'.pythonai-'+token)
                temporary.write_bytes(new);os.replace(temporary,path)
            applied.append((name,old,new))
        evidence={'agent':key,'reviewer':node['reviewed_by'],'files':[c[0] for c in changes],'archive':str(archive),
            'base':result['base'],'commit':result['commit'],'merged_at':time.time()}
        result['merge']=evidence
        with closing(store.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            merge_guard(store,node,conn)
            if conn.execute("UPDATE agent_nodes SET state='merged',result=?,lease=NULL,expires=NULL,updated=? WHERE id=? AND state='merging' AND lease=?",
                (json.dumps(result,ensure_ascii=False),time.time(),key,token)).rowcount!=1:raise RuntimeError('Merge lease lost')
            store.event(conn,node,'merged',evidence)
        return evidence
    except Exception:
        for name,old,new in reversed(applied):
            path=root/name
            if (path.read_bytes() if path.exists() else None)!=new:continue
            if old is None:path.unlink()
            else:path.write_bytes(old)
        with closing(store.connect()) as conn,conn:
            conn.execute("UPDATE agent_nodes SET state='needs_resolution' WHERE id=? AND lease=?",(key,token))
        raise


def human_agent_command(text):
    from permissions import current_role
    parts=text.split(maxsplit=4)
    if len(parts)<2:raise ValueError('Usage: /agents <root_task> [list|cancel|answer|resolve|abandon|merge|archive|operations] ...')
    store=Coordinator();owner=scope_owner();root=parts[1]
    store.tasks.get(owner,root)
    action=parts[2] if len(parts)>2 else 'list'
    if action=='list':
        with closing(store.connect()) as conn:
            rows=conn.execute('SELECT id,parent,persona,goal,state,dependencies,review_of,reviewed_by,deadline FROM agent_nodes WHERE owner=? AND root=?',(owner,root)).fetchall()
        return json.dumps([dict(row) for row in rows],ensure_ascii=False)
    if len(parts)<4:raise ValueError('Agent ID required')
    node=store.get(owner,parts[3])
    if node['root']!=root:raise ValueError('Agent belongs to another root task')
    if action in {'merge','archive','operations'}:
        if not node['payload'].get('temporal_graph'):raise ValueError('This lifecycle command requires a native DAG')
        from .temporal_backend.dag_lifecycle import Lifecycle
        from .temporal_backend.dag import Graphs
        graphs=Graphs(store);graphs.get(node['payload']['temporal_graph'],operator=True)
        lifecycle=Lifecycle(graphs)
        if action=='operations':
            with closing(store.connect()) as conn:
                return json.dumps([dict(r) for r in conn.execute('SELECT id,action,state FROM temporal_dag_operations WHERE node_id=?',(node['id'],))])
        key=lifecycle.request(node['payload']['temporal_graph'],node['id'],action)
        return json.dumps({'operation_id':key,'state':lifecycle.get(key)['state']})
    if action=='cancel':store.cancel(owner,node['id']);return 'Cancellation requested'
    if current_role()!='admin':raise PolicyDenied('Human recovery requires admin role')
    if len(parts)<5:raise ValueError('Human observed answer/result required')
    if action=='abandon' and node['state']=='needs_resolution':
        if len(parts[4].encode())>16000:raise ValueError('Inspection note exceeds 16000 bytes')
        with closing(store.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            raw=conn.execute("SELECT * FROM agent_nodes WHERE id=? AND owner=? AND state='needs_resolution'",(node['id'],owner)).fetchone()
            if raw is None:raise ValueError('Agent recovery state changed')
            live=store.decode(raw);child=store.child_task(live)
            if (live['expires'] or 0)>time.time() or (child and (child['lease_until'] or 0)>time.time()):raise ValueError('Wait for worker leases to expire before abandoning')
            payload=dict(live['payload']);payload['human_disposition']='abandoned after inspection'
            store.capacity(conn,len(json.dumps(payload).encode())-len(json.dumps(live['payload']).encode()))
            conn.execute("UPDATE agent_nodes SET state='conflict',payload=?,lease=NULL,expires=NULL,updated=? WHERE id=?",(json.dumps(payload),time.time(),node['id']))
            conn.execute("UPDATE tasks SET state='cancelled',lease_owner=NULL,lease_until=NULL WHERE owner=? AND id=?",(node_task_owner(payload),'subagent:'+node['id']))
            store.event(conn,node,'human_abandoned',{'inspection':parts[4]})
        return 'Abandoned after human inspection; worktree and evidence retained. Follow-up uses a new node without replaying uncertain effects.'
    if action=='answer' and node['state']=='needs_input':store.tasks.answer(node_task_owner(node['payload']),'subagent:'+node['id'],parts[4])
    elif action=='resolve' and node['state']=='needs_resolution':
        detail=parts[4].split(maxsplit=1)
        if len(detail)!=2:raise ValueError('Usage: /agents <root_task> resolve <node_id> <tool_call_id> <observed_result>')
        store.tasks.mark_uncertain(node_task_owner(node['payload']),'subagent:'+node['id'])
        store.tasks.resolve(node_task_owner(node['payload']),'subagent:'+node['id'],detail[0],detail[1])
    else:raise ValueError('No matching suspended/uncertain action; merge/preparation conflicts need manual inspection and follow-up')
    with closing(store.connect()) as conn,conn:
        conn.execute("UPDATE agent_nodes SET state='queued',lease=NULL,expires=NULL WHERE id=?",(node['id'],))
    start_coordinator(store.path)
    return 'Human result saved; checkpoint recovery queued without replaying the tool'
