"""Versioned directory registry in TaskStore's database."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import time
import uuid

from agent_execution.policy import current_context
from agent_execution.tasks import TaskStore
from .paths import directory, identity, snapshot


def session_key(context=None):
    context = context or current_context()
    value = [context.source, context.identity, context.project, context.conversation or context.session]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def principal(context=None):
    context = context or current_context()
    return json.dumps([context.source, context.identity, context.project], ensure_ascii=False)


class LifecycleStore:
    def __init__(self, path=None):
        self.tasks = TaskStore(path)
        self.path = self.tasks.path
        with closing(self.connect()) as conn, conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS wl_version (version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO wl_version VALUES(1);
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
            ''')
            if conn.execute('SELECT MAX(version) FROM wl_version').fetchone()[0] != 1:
                raise ValueError('Unsupported workspace lifecycle schema')

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
            for row in conn.execute('SELECT relative FROM wl_directories WHERE root=?', (root,)):
                other = Path(row['relative'])
                if Path(normalized) == other or Path(normalized).is_relative_to(other) or other.is_relative_to(normalized):
                    raise ValueError('Directory overlaps an existing registry entry')
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
            rows = [dict(r) for r in conn.execute('SELECT id,relative,purpose,state,generation FROM wl_directories WHERE root=? AND principal=?', (root, principal()))]
            known = {Path(r[0]).parts[0] for r in conn.execute('SELECT relative FROM wl_directories WHERE root=?', (root,))}
            unmanaged = [item.name for item in path.iterdir() if item.is_dir() and item.name not in known and item.name != '.git']
            return {'managed': rows, 'unmanaged': sorted(unmanaged), 'retention': 'until_session_deleted'}

    def assert_writable(self, target):
        with closing(self.connect()) as conn, conn:
            session = self.ensure_session(conn)
            root, path = self.root(conn)
            for row in conn.execute('SELECT * FROM wl_directories WHERE root=?', (root,)):
                location = path / row['relative']
                if target == location or target.is_relative_to(location):
                    if row['principal'] != principal() or row['state'] != 'active':
                        raise PermissionError('Managed directory is not writable by this session')
                    if not conn.execute('SELECT 1 FROM wl_refs WHERE directory=? AND session=?', (row['id'],session)).fetchone():
                        raise PermissionError('Attach this directory to the conversation before writing')

    def attach(self, key):
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            session = self.ensure_session(conn)
            row = self.get(key, conn)
            if row['state'] != 'active':raise ValueError('Only active directories may be shared')
            conn.execute('INSERT OR IGNORE INTO wl_refs VALUES(?,?)', (key,session))
            conn.execute('UPDATE wl_directories SET generation=generation+1 WHERE id=?', (key,))

    def retain(self, key):
        with closing(self.connect()) as conn, conn:
            self.ensure_session(conn)
            row = self.get(key,conn)
            if row['state'] not in {'active','archived'}:raise ValueError('Directory operation in progress')
            conn.execute("UPDATE wl_directories SET purpose='retained',generation=generation+1 WHERE id=?", (key,))

    def preview(self, action, key=None):
        if action not in {'archive','restore','delete_session'}:raise ValueError('Unknown cleanup action')
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            session = self.ensure_session(conn)
            self.root(conn)
            rows = ([self.get(key,conn)] if key else [self.get(r[0],conn) for r in conn.execute('SELECT directory FROM wl_refs WHERE session=?', (session,))])
            items, kept = [], []
            for row in rows:
                refs = conn.execute('SELECT COUNT(*) FROM wl_refs WHERE directory=?', (row['id'],)).fetchone()[0]
                if row['purpose'] != 'temporary' or refs != 1:
                    kept.append({'id':row['id'],'path':row['relative'],'reason':'retained' if row['purpose']!='temporary' else 'shared'})
                    if action != 'delete_session':raise ValueError('Retained/shared directories cannot be moved by temporary cleanup')
                    continue
                expected = 'archived' if action=='restore' else 'active'
                if (action=='delete_session' and row['state'] not in {'active','archived'}) or (action!='delete_session' and row['state']!=expected):
                    raise ValueError('Directory is busy or needs recovery')
                location = self.locate(row)
                manifest = snapshot(location)
                if action=='restore' and directory(Path(row['root_path']),row['relative']).exists():raise FileExistsError('Restore destination exists')
                items.append({'id':row['id'],'path':row['relative'],'generation':row['generation'],'state':row['state'],
                    'digest':manifest['digest'],'files':manifest['files'],'bytes':manifest['bytes']})
            owners = [r[0] for r in conn.execute('SELECT task_owner FROM wl_session_owners WHERE session=?',(session,))]
            blocking = []
            for owner in owners:
                for task in conn.execute('SELECT id,state,lease_until FROM tasks WHERE owner=?',(owner,)):
                    if task['state'] not in {'completed','failed','cancelled'} or (task['lease_until'] or 0)>time.time():blocking.append(task['id'])
            payload = {'session':session,'action':action,'items':items,'kept':kept,'blocking_tasks':blocking}
            plan = uuid.uuid4().hex
            conn.execute('INSERT INTO wl_plans(id,session,principal,action,payload,expires) VALUES(?,?,?,?,?,?)',
                (plan,session,principal(),action,json.dumps(payload),time.time()+300))
            return {'plan_id':plan,**payload,'expires_in':300}
