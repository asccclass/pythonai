"""Versioned directory registry in TaskStore's database."""
from contextlib import closing
import hashlib
import json
import os
import sqlite3
from pathlib import Path
import time
import uuid

from agent_execution.policy import current_context
from agent_execution.tasks import TaskStore
from .paths import directory, identity, snapshot


def session_key(context=None):
    context = context or current_context()
    root_owner=context.coordinator_owner or context.lifecycle_owner
    if root_owner:
        root=Path(os.environ.get('LOCALAPPDATA') or Path.home()/'.local'/'share')/'PythonAI'
        path=Path(os.environ.get('AGENT_TASK_DB') or root/'tasks.db')
        with closing(sqlite3.connect(path)) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='wl_sessions'").fetchone():
                context_parts(context)
                row=conn.execute('SELECT session FROM wl_session_owners WHERE task_owner=?',(root_owner,)).fetchone()
                if row:return row[0]
    parts=context_parts(context)
    value = [*parts[:3],context.conversation or parts[3]]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def principal(context=None):
    context = context or current_context()
    return json.dumps(context_parts(context)[:3],ensure_ascii=False)


def context_parts(context):
    root_owner=context.coordinator_owner or context.lifecycle_owner
    if root_owner:
        value=json.loads(root_owner)
        if len(value)!=4 or value[1]!=context.identity or value[2]!=context.project:
            raise PermissionError('Invalid delegated workspace owner')
        return value
    return [context.source,context.identity,context.project,context.session]


class LifecycleStore:
    def __init__(self, path=None):
        self.tasks = TaskStore(path)
        self.path = self.tasks.path
        with closing(self.connect()) as conn, conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS wl_version (version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO wl_version VALUES(1);
                CREATE TABLE IF NOT EXISTS wl_metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS wl_sessions (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS wl_session_owners (
                    session TEXT NOT NULL, task_owner TEXT NOT NULL, PRIMARY KEY(session,task_owner));
                CREATE TABLE IF NOT EXISTS wl_roots (
                    id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, identity TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS wl_directories (
                    id TEXT PRIMARY KEY, root TEXT NOT NULL, relative TEXT NOT NULL,
                    principal TEXT NOT NULL, origin_task TEXT, purpose TEXT NOT NULL,
                    state TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 1,
                    identity TEXT, archive TEXT, manifest TEXT, UNIQUE(root,relative));
                CREATE TABLE IF NOT EXISTS wl_refs (
                    directory TEXT NOT NULL, session TEXT NOT NULL, PRIMARY KEY(directory,session));
                CREATE TABLE IF NOT EXISTS wl_plans (
                    id TEXT PRIMARY KEY, session TEXT NOT NULL, principal TEXT NOT NULL,
                    action TEXT NOT NULL, payload TEXT NOT NULL, expires REAL NOT NULL,
                    approval TEXT, state TEXT NOT NULL DEFAULT 'preview');
                CREATE TABLE IF NOT EXISTS wl_operations (
                    id TEXT PRIMARY KEY, plan TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '', updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS wl_outbox (
                    session TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS wl_leases (
                    operation TEXT PRIMARY KEY, token TEXT NOT NULL, expires REAL NOT NULL);
            ''')
            if conn.execute('SELECT MAX(version) FROM wl_version').fetchone()[0] != 1:
                raise ValueError('Unsupported workspace lifecycle schema')
            conn.execute("INSERT OR IGNORE INTO wl_metadata VALUES('instance',?)",(uuid.uuid4().hex,))
            columns={r['name'] for r in conn.execute('PRAGMA table_info(wl_directories)')}
            if 'original_relative' not in columns:conn.execute('ALTER TABLE wl_directories ADD COLUMN original_relative TEXT')
            if 'build_state' not in columns:conn.execute("ALTER TABLE wl_directories ADD COLUMN build_state TEXT NOT NULL DEFAULT 'ready'")
            conn.executescript('''CREATE TABLE IF NOT EXISTS wl_resources (
                id TEXT PRIMARY KEY,principal TEXT NOT NULL,kind TEXT NOT NULL,path TEXT NOT NULL,task TEXT);
                CREATE TABLE IF NOT EXISTS wl_resource_refs (
                resource TEXT NOT NULL,session TEXT NOT NULL,PRIMARY KEY(resource,session));''')
            conn.execute('CREATE TABLE IF NOT EXISTS wl_transports (session TEXT,owner TEXT,path TEXT,PRIMARY KEY(session,owner,path))')
            conn.execute('CREATE TABLE IF NOT EXISTS wl_audit (operation TEXT PRIMARY KEY,principal TEXT,session TEXT,result TEXT,files INTEGER,bytes INTEGER,created REAL)')

    def connect(self):
        return self.tasks.connect()

    def ensure_session(self, conn=None):
        if conn is None:
            with closing(self.connect()) as db, db:return self.ensure_session(db)
        key = session_key()
        conn.execute("INSERT OR IGNORE INTO wl_sessions VALUES(?,?,'active')", (key, principal()))
        row = conn.execute('SELECT * FROM wl_sessions WHERE id=?', (key,)).fetchone()
        if row['principal'] != principal() or row['state'] != 'active':
            raise PermissionError('Conversation is deleting or deleted')
        from agent_execution.tasks import task_owner
        conn.execute('INSERT OR IGNORE INTO wl_session_owners VALUES(?,?)', (key, task_owner(current_context())))
        context=current_context()
        if context.communication_db is not None and context_parts(context)[0]=='communication':
            owner=json.dumps(context_parts(context),separators=(',',':'))
            conn.execute('INSERT OR IGNORE INTO wl_transports VALUES(?,?,?)',(key,owner,str(Path(context.communication_db).resolve())))
        return key

    def root(self, conn):
        from base import workspace_root
        path = workspace_root()
        root = hashlib.sha256(str(path).casefold().encode()).hexdigest()
        ident = json.dumps(identity(path))
        conn.execute('INSERT OR IGNORE INTO wl_roots VALUES(?,?,?)', (root, str(path), ident))
        saved = conn.execute('SELECT * FROM wl_roots WHERE id=?', (root,)).fetchone()
        if saved['identity'] != ident:raise PermissionError('Workspace identity changed')
        return root, path

    def create(self, relative, purpose='retained', adopt=False):
        if purpose not in {'temporary', 'retained'}:raise ValueError('Unknown directory purpose')
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            session = self.ensure_session(conn)
            root, path = self.root(conn)
            target = directory(path, relative)
            normalized = str(target.relative_to(path))
            for row in conn.execute("SELECT d.relative,r.path FROM wl_directories d JOIN wl_roots r ON r.id=d.root WHERE d.state!='purged'"):
                other = (Path(row['path'])/row['relative']).resolve()
                if target.resolve() == other or target.resolve().is_relative_to(other) or other.is_relative_to(target.resolve()):
                    raise ValueError('Directory overlaps an existing registry entry')
            if conn.execute('SELECT COUNT(*) FROM wl_directories').fetchone()[0]>=1000:
                raise ValueError('Managed directory capacity reached')
            if target.exists() and not adopt:raise FileExistsError('Existing directory remains unmanaged; explicit adoption required')
            if adopt:
                from permissions import current_role
                if current_role() != 'admin':raise PermissionError('Adoption requires admin')
                snapshot(target)
            key = uuid.uuid4().hex
            conn.execute('INSERT INTO wl_directories(id,root,relative,principal,origin_task,purpose,state) VALUES(?,?,?,?,?,?,?)',
                (key, root, normalized, principal(), current_context().task_id, purpose, 'creating'))
            conn.execute('INSERT INTO wl_refs VALUES(?,?)', (key, session))
        try:
            if not adopt:target.mkdir(parents=True, exist_ok=False)
            ident = json.dumps(identity(target))
            with closing(self.connect()) as conn, conn:
                conn.execute("UPDATE wl_directories SET state='active',identity=? WHERE id=?", (ident, key))
        except Exception:
            with closing(self.connect()) as conn, conn:conn.execute("UPDATE wl_directories SET state='creating_failed' WHERE id=?", (key,))
            raise
        return key

    def get(self, key, conn=None):
        if conn is None:
            with closing(self.connect()) as db:return self.get(key, db)
        row = conn.execute('SELECT d.*,r.path AS root_path,r.identity AS root_identity FROM wl_directories d JOIN wl_roots r ON r.id=d.root WHERE d.id=?', (key,)).fetchone()
        if row is None or row['principal'] != principal():raise PermissionError('Directory not owned by this user')
        return dict(row)

    def locate(self, row):
        root = Path(row['root_path'])
        if json.dumps(identity(root)) != row['root_identity']:raise PermissionError('Workspace identity changed')
        target = Path(row['archive']) if row['state'] in {'archived','purging','purge_pending'} and row['archive'] else directory(root, row['relative'])
        if json.dumps(identity(target)) != row['identity']:raise PermissionError('Directory identity changed')
        return target

    def inventory(self):
        with closing(self.connect()) as conn, conn:
            self.ensure_session(conn)
            root, path = self.root(conn)
            rows = [dict(r) for r in conn.execute("SELECT id,relative,purpose,state,generation,build_state FROM wl_directories WHERE root=? AND principal=? AND state!='purged'", (root, principal()))]
            known = {Path(r[0]).parts[0] for r in conn.execute('SELECT relative FROM wl_directories WHERE root=?', (root,))}
            unmanaged = [item.name for item in path.iterdir() if item.is_dir() and item.name not in known and item.name != '.git']
            return {'managed': rows, 'unmanaged': sorted(unmanaged), 'resources':self.resources(conn), 'retention': 'until_session_deleted'}

    def resources(self,conn):
        return [dict(r) for r in conn.execute('SELECT r.id,r.kind,r.path,r.task FROM wl_resources r JOIN wl_resource_refs f ON f.resource=r.id WHERE f.session=? AND r.principal=?',(session_key(),principal()))]

    def register_worktree(self,path):
        """Record Git-managed resources as retained; never route them through native purge."""
        path=Path(path).resolve(strict=True)
        identity(path)
        if not (path/'.git').is_file():raise ValueError('Expected a Git worktree')
        key=hashlib.sha256(json.dumps([principal(),str(path)]).encode()).hexdigest()
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            session=self.ensure_session(conn)
            conn.execute('INSERT OR IGNORE INTO wl_resources VALUES(?,?,?,?,?)',(key,principal(),'worktree',str(path),current_context().task_id))
            conn.execute('INSERT OR IGNORE INTO wl_resource_refs VALUES(?,?)',(key,session))
        return key

    def build_status(self,key,state):
        if state not in {'building','ready','failed'}:raise ValueError('Invalid build state')
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            self.ensure_session(conn)
            row=self.get(key,conn)
            if row['state']!='active':raise ValueError('Directory operation in progress')
            if state=='building' and row['build_state']=='building':raise ValueError('Program build needs completion or explicit recovery')
            conn.execute('UPDATE wl_directories SET build_state=?,generation=generation+1 WHERE id=?',(state,key))

    def containing_directory(self,target):
        with closing(self.connect()) as conn:
            for row in conn.execute("SELECT d.id,d.relative,r.path FROM wl_directories d JOIN wl_roots r ON r.id=d.root WHERE d.principal=? AND d.state='active'",(principal(),)):
                location=(Path(row['path'])/row['relative']).resolve()
                if Path(target).resolve().is_relative_to(location):return row['id']
        return None

    def recover_build(self,key):
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            self.ensure_session(conn)
            row=self.get(key,conn)
            if row['state']!='active' or row['build_state']!='building':raise ValueError('No interrupted build to recover')
            owners=[r[0] for r in conn.execute('SELECT o.task_owner FROM wl_session_owners o JOIN wl_refs f ON f.session=o.session WHERE f.directory=?',(key,))]
            for owner in owners:
                if conn.execute("SELECT 1 FROM tasks WHERE owner=? AND (state NOT IN ('completed','failed','cancelled') OR COALESCE(lease_until,0)>?)",(owner,time.time())).fetchone():raise ValueError('A task still needs termination')
            from .erasure import external_blockers
            if external_blockers(owners,self.path):raise ValueError('A worker is still active')
            conn.execute("UPDATE wl_directories SET build_state='failed',generation=generation+1 WHERE id=?",(key,))

    def assert_writable(self, target):
        self.assert_access(target,True)

    def inaccessible_paths(self):
        with closing(self.connect()) as conn:
            return [(Path(row['path'])/row['relative']).resolve() for row in conn.execute(
                "SELECT d.relative,d.principal,d.state,r.path FROM wl_directories d JOIN wl_roots r ON r.id=d.root WHERE d.state!='purged'")
                if row['principal']!=principal() or row['state']!='active']

    def assert_access(self,target,write=False):
        with closing(self.connect()) as conn, conn:
            session = self.ensure_session(conn)
            root, path = self.root(conn)
            target=Path(target).resolve()
            for row in conn.execute("SELECT d.*,r.path AS root_path,r.identity AS root_identity FROM wl_directories d JOIN wl_roots r ON r.id=d.root WHERE d.state!='purged'"):
                location = (Path(row['root_path']) / row['relative']).resolve()
                if target == location or target.is_relative_to(location):
                    if row['principal'] != principal() or row['state'] != 'active':
                        raise PermissionError('Managed directory is not accessible by this session')
                    if json.dumps(identity(Path(row['root_path'])))!=row['root_identity']:
                        raise PermissionError('Registered workspace was replaced')
                    if write and not conn.execute('SELECT 1 FROM wl_refs WHERE directory=? AND session=?', (row['id'],session)).fetchone():
                        raise PermissionError('Attach this directory to the conversation before writing')

    def attach(self, key):
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            session = self.ensure_session(conn)
            row = self.get(key, conn)
            if row['state'] not in {'active','archived'}:raise ValueError('Directory operation in progress')
            if conn.execute("SELECT 1 FROM wl_refs r JOIN wl_sessions s ON s.id=r.session WHERE r.directory=? AND s.state!='active'",(key,)).fetchone():
                raise ValueError('A referencing conversation is being deleted')
            conn.execute('INSERT OR IGNORE INTO wl_refs VALUES(?,?)', (key,session))
            conn.execute('UPDATE wl_directories SET generation=generation+1 WHERE id=?', (key,))

    def retain(self, key):
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self.ensure_session(conn)
            row = self.get(key,conn)
            if row['state'] not in {'active','archived'}:raise ValueError('Directory operation in progress')
            if conn.execute("SELECT 1 FROM wl_refs r JOIN wl_sessions s ON s.id=r.session WHERE r.directory=? AND s.state!='active'",(key,)).fetchone():
                raise ValueError('A referencing conversation is being deleted')
            conn.execute("UPDATE wl_directories SET purpose='retained',generation=generation+1 WHERE id=?", (key,))

    def preview(self, action, key=None):
        if action not in {'archive','restore','delete_session'}:raise ValueError('Unknown cleanup action')
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            session = self.ensure_session(conn)
            root,path=self.root(conn)
            rows = ([self.get(key,conn)] if key else [self.get(r[0],conn) for r in conn.execute('SELECT directory FROM wl_refs WHERE session=?', (session,))])
            items, kept = [], []
            for row in rows:
                refs = conn.execute('SELECT COUNT(*) FROM wl_refs WHERE directory=?', (row['id'],)).fetchone()[0]
                if (row['purpose'] != 'temporary' and action!='restore') or refs != 1:
                    kept.append({'id':row['id'],'path':row['relative'],'generation':row['generation'],
                        'references':sorted(r[0] for r in conn.execute('SELECT session FROM wl_refs WHERE directory=?',(row['id'],))),
                        'reason':'retained' if row['purpose']!='temporary' else 'shared'})
                    if action != 'delete_session':raise ValueError('Retained/shared directories cannot be moved by temporary cleanup')
                    continue
                expected = 'archived' if action=='restore' else 'active'
                if (action=='delete_session' and row['state'] not in {'active','archived'}) or (action!='delete_session' and row['state']!=expected):
                    raise ValueError('Directory is busy or needs recovery')
                if row['build_state']=='building':raise ValueError('Program creation is still in progress')
                location = self.locate(row)
                manifest = snapshot(location)
                if action=='restore' and directory(Path(row['root_path']),row['relative']).exists():raise FileExistsError('Restore destination exists')
                items.append({'id':row['id'],'path':row['relative'],'generation':row['generation'],'state':row['state'],
                    'digest':manifest['digest'],'files':manifest['files'],'bytes':manifest['bytes'],'manifest':manifest['records']})
            owners = [r[0] for r in conn.execute('SELECT task_owner FROM wl_session_owners WHERE session=?',(session,))]
            blocking = []
            for owner in owners:
                for task in conn.execute('SELECT id,state,lease_until FROM tasks WHERE owner=?',(owner,)):
                    if task['state'] not in {'completed','failed','cancelled'} or (task['lease_until'] or 0)>time.time():blocking.append(task['id'])
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='agent_nodes'").fetchone():
                from agent_execution.coordinator import SUCCESS,STOPPED
                for owner in owners:
                    for node in conn.execute('SELECT id,state,expires FROM agent_nodes WHERE owner=?',(owner,)):
                        if node['state'] not in SUCCESS|STOPPED or (node['expires'] or 0)>time.time():blocking.append('agent:'+node['id'])
            from .erasure import external_blockers,schedules,transports
            from communication_store import CommunicationStore
            blocking.extend(external_blockers(owners,self.path))
            known={Path(r[0]).parts[0] for r in conn.execute('SELECT relative FROM wl_directories WHERE root=?',(root,))}
            unmanaged_count=sum(p.is_dir() and p.name not in known and p.name!='.git' for p in path.iterdir())
            payload = {'session':session,'action':action,'items':items,'kept':kept,'blocking_tasks':blocking,'unmanaged_count':unmanaged_count,
                'resources':self.resources(conn),
                'transports':[CommunicationStore(path).lifecycle_summary(owner) for owner,path in transports(owners,self.path)],
                'schedules':schedules(owners) if action=='delete_session' else []}
            plan = uuid.uuid4().hex
            conn.execute('INSERT INTO wl_plans(id,session,principal,action,payload,expires) VALUES(?,?,?,?,?,?)',
                (plan,session,principal(),action,json.dumps(payload),time.time()+300))
            return {'plan_id':plan,**public_payload(payload),'expires_in':300}

    def plan(self, key, conn=None):
        if conn is None:
            with closing(self.connect()) as db:return self.plan(key,db)
        row=conn.execute('SELECT * FROM wl_plans WHERE id=?',(key,)).fetchone()
        if not row or row['principal']!=principal() or row['session']!=session_key():
            raise PermissionError('Plan does not belong to this conversation')
        return {**dict(row),'payload':json.loads(row['payload'])}

    def request(self, key):
        from agent_execution.approvals import ApprovalStore
        plan=self.plan(key)
        if plan['expires']<=time.time():raise ValueError('Preview expired; preview again')
        if plan['payload']['blocking_tasks']:raise ValueError('Finish/cancel active tasks before cleanup')
        context=current_context()
        approval=ApprovalStore().request(context.identity,context.session,'workspace:'+key,public_payload(plan['payload']))
        with closing(self.connect()) as conn,conn:conn.execute('UPDATE wl_plans SET approval=? WHERE id=?',(approval,key))
        return {'plan_id':key,'approval_id':approval,'preview':public_payload(plan['payload'])}

    def execute(self, key, approval):
        from .operations import execute
        return execute(self,key,approval)

    def confirm(self,key):
        """Only human transport dispatchers may call this method."""
        from agent_execution.approvals import ApprovalStore
        plan=self.plan(key)
        with closing(self.connect()) as conn:
            existing=conn.execute('SELECT state FROM wl_operations WHERE plan=?',(key,)).fetchone()
        if existing:
            return self.execute(key,plan['approval'])
        if not plan['approval']:
            self.request(key)
            plan=self.plan(key)
        context=current_context()
        ApprovalStore().decide(plan['approval'],context.identity,context.session,True)
        return self.execute(key,plan['approval'])


def public_payload(payload):
    return {**payload,'items':[{k:v for k,v in item.items() if k!='manifest'} for item in payload['items']]}
