"""Same-database backend bindings and submission outbox, containing no SDK objects."""
from contextlib import closing
import hashlib
import json
import sqlite3
import time
import uuid

from .config import Settings


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Store:
    def __init__(self, path=None):
        from agent_execution.tasks import TaskStore
        self.tasks = TaskStore(path)
        self.path = self.tasks.path
        with closing(self.tasks.connect()) as conn, conn:
            conn.execute('CREATE TABLE IF NOT EXISTS temporal_schema(version INTEGER PRIMARY KEY)')
            versions = [r[0] for r in conn.execute('SELECT version FROM temporal_schema')]
            if versions and versions != [1]:
                raise ValueError('Unsupported Temporal database schema')
            had_cancellations = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_cancellations'").fetchone())
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS temporal_schema(version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO temporal_schema VALUES(1);
                CREATE TABLE IF NOT EXISTS temporal_host(id INTEGER PRIMARY KEY CHECK(id=1), token TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_bindings(
                    owner TEXT NOT NULL, task_id TEXT NOT NULL, backend TEXT NOT NULL,
                    PRIMARY KEY(owner,task_id));
                CREATE TABLE IF NOT EXISTS temporal_submissions(
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, task_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL UNIQUE, input_digest TEXT NOT NULL,
                    input TEXT NOT NULL, context TEXT NOT NULL, role TEXT NOT NULL,
                    address TEXT NOT NULL, namespace TEXT NOT NULL, queue TEXT NOT NULL,
                    state TEXT NOT NULL, fence TEXT, result TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT, updated REAL NOT NULL, UNIQUE(owner,task_id));
                CREATE TABLE IF NOT EXISTS temporal_protocols(
                    submission_id TEXT PRIMARY KEY, workflow_type TEXT NOT NULL, version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS temporal_cancellations(
                    submission_id TEXT PRIMARY KEY, delivery TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS temporal_budget_contracts(
                    submission_id TEXT PRIMARY KEY, contract TEXT NOT NULL);
            ''')
            versions = [r[0] for r in conn.execute('SELECT version FROM temporal_schema')]
            if versions != [1]:
                raise ValueError('Unsupported Temporal database schema')
            conn.execute('INSERT OR IGNORE INTO temporal_host VALUES(1,?)', (uuid.uuid4().hex,))
            if not had_cancellations:
                conn.execute("INSERT OR IGNORE INTO temporal_cancellations SELECT id,'pending' FROM temporal_submissions WHERE state='cancelled'")

    @property
    def queue(self):
        return self.queue_for('bridge')

    def queue_for(self, engine):
        if engine not in {'bridge', 'steps', 'human', 'approval'}:
            raise ValueError('Unknown Temporal engine')
        with closing(self.tasks.connect()) as conn:
            token = conn.execute('SELECT token FROM temporal_host WHERE id=1').fetchone()[0]
        suffix = {'bridge': '-readonly-v1', 'steps': '-steps-readonly-v1', 'human': '-steps-readonly-v2', 'approval': '-steps-readonly-v3'}[engine]
        return 'pythonai-host-' + token + suffix

    def workflow_type(self, row):
        from .contracts import WORKFLOW_NAME, STEP_WORKFLOW_NAME, HUMAN_WORKFLOW_NAME, APPROVAL_WORKFLOW_NAME
        with closing(self.tasks.connect()) as conn:
            protocol = conn.execute('SELECT * FROM temporal_protocols WHERE submission_id=?', (row['id'],)).fetchone()
        if protocol is None:
            if row['queue'] != self.queue:
                raise ValueError('Missing execution protocol for non-legacy submission')
            return WORKFLOW_NAME  # Existing M0 submissions keep their protocol.
        versions = {WORKFLOW_NAME: 1, STEP_WORKFLOW_NAME: 1, HUMAN_WORKFLOW_NAME: 2, APPROVAL_WORKFLOW_NAME: 3}
        if protocol['workflow_type'] not in versions or protocol['version'] != versions[protocol['workflow_type']]:
            raise ValueError('Unsupported Temporal execution protocol')
        engine = {WORKFLOW_NAME: 'bridge', STEP_WORKFLOW_NAME: 'steps', HUMAN_WORKFLOW_NAME: 'human', APPROVAL_WORKFLOW_NAME: 'approval'}[protocol['workflow_type']]
        if row['queue'] != self.queue_for(engine):
            raise ValueError('Execution protocol does not match its persisted queue')
        return protocol['workflow_type']

    def bind(self, owner, task, requested):
        from agent_execution.tasks import assert_owner_active
        if not isinstance(task, str) or not 1 <= len(task) <= 256:
            raise ValueError('Task ID must contain 1..256 characters')
        with closing(self.tasks.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            assert_owner_active(conn, owner)
            row = conn.execute('SELECT backend FROM execution_bindings WHERE owner=? AND task_id=?', (owner, task)).fetchone()
            if row:
                return row['backend']
            if requested not in {'sqlite', 'temporal'}:
                raise ValueError('Unknown task backend')
            from agent_execution.delivery import positive_limit
            if conn.execute('SELECT COUNT(*) FROM execution_bindings').fetchone()[0] >= positive_limit('AGENT_TASK_LIMIT', 10000):
                raise ValueError('Execution binding capacity reached')
            # Existing legacy work is never silently migrated by an environment variable.
            exists = conn.execute('SELECT 1 FROM tasks WHERE owner=? AND id=?', (owner, task)).fetchone()
            backend = 'sqlite' if exists else requested
            conn.execute('INSERT INTO execution_bindings VALUES(?,?,?)', (owner, task, backend))
            return backend

    def submit(self, owner, task, messages, context, role, settings):
        from agent_execution.tasks import assert_owner_active
        from agent_execution.budgets import TaskBudget
        budget_contract = canonical(TaskBudget(owner, task, self.path).contract())
        latest = next((m for m in reversed(messages) if m.get('role') == 'user'), None)
        if latest is None:
            raise ValueError('Temporal task requires a user input')
        fingerprint = digest(latest)
        data, scope = canonical(messages), canonical(context)
        if len(data.encode()) > 1024 * 1024 or len(scope.encode()) > 16384:
            raise ValueError('M0 Temporal input exceeds 1 MiB local storage limit')
        queue = self.queue_for(settings.engine)
        with closing(self.tasks.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            assert_owner_active(conn, owner)
            binding = conn.execute('SELECT backend FROM execution_bindings WHERE owner=? AND task_id=?', (owner, task)).fetchone()
            if binding is None or binding[0] != 'temporal':
                raise ValueError('Task is not bound to Temporal')
            old = conn.execute('SELECT * FROM temporal_submissions WHERE owner=? AND task_id=?', (owner, task)).fetchone()
            if old:
                if old['input_digest'] != fingerprint:
                    raise ValueError('Task ID cannot be reused with different input')
                saved_budget = conn.execute('SELECT contract FROM temporal_budget_contracts WHERE submission_id=?',
                                            (old['id'],)).fetchone()
                if saved_budget is not None and saved_budget['contract'] != budget_contract:
                    raise PermissionError('Root budget contract changed')
                return dict(old)
            self.tasks.capacity(conn, len(data.encode()) + len(scope.encode()))
            # Bound the new store as well as the legacy checkpoint capacity.
            size = conn.execute('SELECT COALESCE(SUM(length(CAST(input AS BLOB))+length(CAST(context AS BLOB))+length(CAST(COALESCE(result,\'\') AS BLOB))),0) FROM temporal_submissions').fetchone()[0]
            from agent_execution.delivery import positive_limit
            if conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] >= positive_limit('AGENT_TASK_LIMIT', 10000):
                raise ValueError('Task record capacity reached')
            if size + len(data.encode()) + len(scope.encode()) > positive_limit('AGENT_TASK_STORAGE_BYTES', 256*1024*1024):
                raise ValueError('Temporal local storage capacity reached')
            if context.get('publication') is not None:
                from .publication import install
                install(conn)
            key = uuid.uuid4().hex
            workflow_id = 'pythonai/' + digest(owner)[:24] + '/' + key
            if conn.execute('SELECT 1 FROM tasks WHERE owner=? AND id=?', (owner, task)).fetchone():
                raise ValueError('Legacy task cannot be resubmitted as Temporal')
            goal = latest.get('content')
            if not isinstance(goal, str):
                raise ValueError('M0 Temporal accepts text-only user tasks')
            from agent_execution.acceptance import load_contract
            conn.execute('INSERT INTO tasks(owner,id,goal,criteria,state,phase,checkpoint,episode,updated) VALUES(?,?,?,?,?,?,?,?,?)',
                         (owner, task, goal[:16000], canonical(load_contract(goal[:16000])),
                          'pending', 'temporal_pending', canonical({'messages': messages}), (context.get('publication') or {}).get('episode'), time.time()))
            conn.execute('INSERT INTO temporal_submissions(id,owner,task_id,workflow_id,input_digest,input,context,role,address,namespace,queue,state,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (key, owner, task, workflow_id, fingerprint, data, scope, role,
                          settings.address, settings.namespace, queue, 'pending', time.time()))
            from .contracts import WORKFLOW_NAME, STEP_WORKFLOW_NAME, HUMAN_WORKFLOW_NAME, APPROVAL_WORKFLOW_NAME
            kind = {'bridge': WORKFLOW_NAME, 'steps': STEP_WORKFLOW_NAME, 'human': HUMAN_WORKFLOW_NAME, 'approval': APPROVAL_WORKFLOW_NAME}[settings.engine]
            conn.execute('INSERT INTO temporal_protocols VALUES(?,?,?)',
                         (key, kind, {'bridge': 1, 'steps': 1, 'human': 2, 'approval': 3}[settings.engine]))
            conn.execute('INSERT INTO temporal_budget_contracts VALUES(?,?)', (key, budget_contract))
            return dict(conn.execute('SELECT * FROM temporal_submissions WHERE id=?', (key,)).fetchone())

    def verify_budget_contract(self, row):
        """Old M0/TP11 rows remain readable; all newly accepted rows are pinned."""
        with closing(self.tasks.connect()) as conn:
            saved = conn.execute('SELECT contract FROM temporal_budget_contracts WHERE submission_id=?',
                                 (row['id'],)).fetchone()
        if saved is None:
            return None
        from .contracts import validate_budget_contract
        contract = validate_budget_contract(json.loads(saved['contract']))
        from agent_execution.budgets import TaskBudget
        current = TaskBudget(row['owner'], row['task_id'], self.path).contract()
        if canonical(current) != canonical(contract):
            raise PermissionError('Root budget contract changed')
        return contract

    def fail_budget_before_start(self, key, owner):
        """A queued expiry has no Activity effects and can be finalized as a known failure."""
        row = self.get(key, owner)
        result = canonical({'reply': 'Error: Task deadline exhausted', 'messages': json.loads(row['input'])})
        with closing(self.tasks.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            from .ledger import fence_operations
            fence_operations(conn, owner, row['task_id'])
            changed = conn.execute("UPDATE temporal_submissions SET state='failed',result=?,last_error='BudgetExhausted',updated=? WHERE id=? AND state IN ('pending','dispatched')",
                                   (result, time.time(), key)).rowcount
            if changed:
                conn.execute("UPDATE tasks SET state='failed',reply='Error: Task deadline exhausted',lease_owner=NULL,lease_until=NULL,updated=? WHERE owner=? AND id=? AND state='pending'",
                             (time.time(), owner, row['task_id']))
        return bool(changed)

    def get(self, key, owner=None):
        with closing(self.tasks.connect()) as conn:
            row = conn.execute('SELECT * FROM temporal_submissions WHERE id=?', (key,)).fetchone()
        if row is None or (owner is not None and row['owner'] != owner):
            raise KeyError('Temporal task not found in this scope')
        return dict(row)

    def for_task(self, owner, task):
        with closing(self.tasks.connect()) as conn:
            row = conn.execute('SELECT * FROM temporal_submissions WHERE owner=? AND task_id=?', (owner, task)).fetchone()
        return dict(row) if row else None

    def pending(self):
        with closing(self.tasks.connect()) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM temporal_submissions WHERE state='pending' ORDER BY updated LIMIT 100")]

    def active(self):
        with closing(self.tasks.connect()) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM temporal_submissions WHERE state IN ('dispatched','running') ORDER BY updated LIMIT 100")]

    def workflow_failed(self, key, owner, error_type):
        """Terminal workflow failure fences activity results, without claiming no effects."""
        row = self.get(key, owner)
        with closing(self.tasks.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            changed = conn.execute("UPDATE temporal_submissions SET state='uncertain',last_error=?,updated=? WHERE id=? AND state IN ('pending','dispatched','running')",
                                   (error_type, time.time(), key)).rowcount
            if changed:
                from .ledger import fence_operations
                fence_operations(conn, owner, row['task_id'])
                from .publication import final_intent
                final_intent(conn,row)
                conn.execute("UPDATE tasks SET state='uncertain',reply='Temporal execution ended; inspect checkpoint before recovery',lease_owner=NULL,lease_until=NULL,updated=? WHERE owner=? AND id=? AND state NOT IN ('completed','failed','cancelled')",
                             (time.time(), owner, row['task_id']))

    def workflow_closed(self, key, owner, status):
        if status == 'CANCELED':
            self.cancel(key, owner)
        else:
            self.workflow_failed(key, owner, status)

    def dispatched(self, key, error=None):
        with closing(self.tasks.connect()) as conn, conn:
            conn.execute("UPDATE temporal_submissions SET attempts=attempts+1,last_error=?,state=CASE WHEN ? IS NULL THEN 'dispatched' ELSE state END,updated=? WHERE id=? AND state='pending'",
                         (error, error, time.time(), key))

    def claim(self, key, settings):
        from agent_execution.tasks import assert_owner_active
        fence = uuid.uuid4().hex
        row = self.get(key)
        if (row['address'], row['namespace'], row['queue']) != (settings.address, settings.namespace, self.queue):
            raise PermissionError('Submission does not target this host/namespace')
        with closing(self.tasks.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            assert_owner_active(conn, row['owner'])
            task = conn.execute('SELECT state FROM tasks WHERE owner=? AND id=?', (row['owner'], row['task_id'])).fetchone()
            if not task or task['state'] == 'cancelled':
                raise PermissionError('Task was cancelled or removed')
            if conn.execute("UPDATE temporal_submissions SET state='running',fence=?,updated=? WHERE id=? AND state IN ('pending','dispatched')",
                            (fence, time.time(), key)).rowcount != 1:
                raise RuntimeError('Activity already started or cancelled; inspect effects before recovery')
        return {**row, 'fence': fence}

    def finish(self, key, fence, state, result):
        data = canonical(result)
        if len(data.encode()) > 2*1024*1024:
            raise ValueError('Temporal local result exceeds 2 MiB')
        with closing(self.tasks.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = self.get(key)
            from agent_execution.tasks import assert_owner_active
            assert_owner_active(conn, row['owner'])
            task = conn.execute('SELECT state FROM tasks WHERE owner=? AND id=?', (row['owner'], row['task_id'])).fetchone()
            if task is None or task['state'] == 'cancelled':
                raise PermissionError('Activity result fenced by task cancellation')
            size = conn.execute('SELECT COALESCE(SUM(length(CAST(input AS BLOB))+length(CAST(context AS BLOB))+length(CAST(COALESCE(result,\'\') AS BLOB))),0) FROM temporal_submissions').fetchone()[0]
            from agent_execution.delivery import positive_limit
            if size + len(data.encode()) - len((row['result'] or '').encode()) > positive_limit('AGENT_TASK_STORAGE_BYTES', 256*1024*1024):
                raise ValueError('Temporal result storage capacity reached')
            if conn.execute("UPDATE temporal_submissions SET state=?,result=?,updated=? WHERE id=? AND fence=? AND state='running'",
                            (state, data, time.time(), key, fence)).rowcount != 1:
                raise PermissionError('Activity result fenced by cancellation or newer execution')
            from .publication import final_intent
            final_intent(conn,row)
        from .publication import finalize
        finalize(self,self.get(key))

    def cancel(self, key, owner):
        row = self.get(key, owner)
        with closing(self.tasks.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("UPDATE tasks SET state='cancelled',reply='Task cancelled',lease_owner=NULL,lease_until=NULL,updated=? WHERE owner=? AND id=(SELECT task_id FROM temporal_submissions WHERE id=?) AND state NOT IN ('completed','failed','cancelled')",
                         (time.time(), owner, key))
            from .cancellation import request
            request(conn, owner, row['task_id'])

        from agent_execution.delivery import CapacityError
        try:
            from .approval_gate import cancel_waits
            cancel_waits(self, row)
        except (OSError, sqlite3.Error, ValueError, CapacityError):
            pass  # Cancellation fences already committed; reconcile/TTL retires grants.

    @staticmethod
    def settings(row):
        return Settings(row['address'], row['namespace'])
