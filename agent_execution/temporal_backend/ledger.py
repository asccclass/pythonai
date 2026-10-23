"""Owner-scoped operation fences and atomic, local JSON artifact references.

No SDK imports or automatic retries. Effects belong to the supplied executor.
"""
from contextlib import closing
import hashlib
import json
import time
import uuid

from agent_execution.tasks import assert_owner_active, task_owner
from .store import canonical, digest


class OperationUncertain(RuntimeError):
    """An operation may have executed; inspection is required before recovery."""


def fence_operations(conn, owner, task):
    """Called inside the submission cancellation/failure transaction, if installed."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='operation_ledger'").fetchone():
        conn.execute("UPDATE operation_ledger SET state=CASE WHEN state='prepared' THEN 'rejected_no_effect' ELSE 'uncertain' END,updated=? WHERE owner=? AND task_id=? AND state IN ('prepared','running')",
                     (time.time(), owner, task))


class Ledger:
    def __init__(self, store, submission_id, submission_fence):
        from .config import operation_limits
        operation_limits()
        self.store, self.key, self.fence = store, submission_id, submission_fence
        row = store.get(submission_id)
        self.owner, self.task = row['owner'], row['task_id']
        self.scope_digest = digest([row['input_digest'], row['context'], row['role']])
        with closing(store.tasks.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self._scope(conn, active=False)
            conn.execute('CREATE TABLE IF NOT EXISTS temporal_operation_schema(version INTEGER PRIMARY KEY)')
            versions = [r[0] for r in conn.execute('SELECT version FROM temporal_operation_schema')]
            if versions and versions != [1]:
                raise ValueError('Unsupported Temporal operation schema')
            conn.execute('INSERT OR IGNORE INTO temporal_operation_schema VALUES(1)')
            conn.execute('''CREATE TABLE IF NOT EXISTS operation_ledger(
                owner TEXT NOT NULL, task_id TEXT NOT NULL, step_id TEXT NOT NULL,
                kind TEXT NOT NULL, arguments_digest TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('prepared','running','completed','uncertain','rejected_no_effect')),
                fence TEXT, result_ref TEXT, evidence_ref TEXT, updated REAL NOT NULL,
                PRIMARY KEY(owner,task_id,step_id))''')
            conn.execute('''CREATE TABLE IF NOT EXISTS temporal_artifacts(
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, task_id TEXT NOT NULL,
                sha256 TEXT NOT NULL, byte_count INTEGER NOT NULL, data BLOB NOT NULL)''')

    def _scope(self, conn, *, active=True):
        from agent_execution.policy import current_context
        context = current_context()
        if task_owner(context) != self.owner or context.task_id != self.task:
            raise PermissionError('Operation is outside this task scope')
        assert_owner_active(conn, self.owner)
        row = conn.execute('SELECT state,fence,input_digest,context,role FROM temporal_submissions WHERE id=? AND owner=? AND task_id=?',
                           (self.key, self.owner, self.task)).fetchone()
        task = conn.execute('SELECT state FROM tasks WHERE owner=? AND id=?', (self.owner, self.task)).fetchone()
        binding = conn.execute('SELECT backend FROM execution_bindings WHERE owner=? AND task_id=?',
                               (self.owner, self.task)).fetchone()
        states = {'running'} if active else {'running', 'completed', 'failed'}
        if (not row or not self.fence or row['fence'] != self.fence or row['state'] not in states
                or not task or task['state'] not in ({'pending', 'running'} if active else {'pending', 'running', 'completed', 'failed', 'suspended'})
                or not binding or binding[0] != 'temporal'):
            raise PermissionError('Operation fenced by task or submission state')
        if digest([row['input_digest'], row['context'], row['role']]) != self.scope_digest:
            raise PermissionError('Stored operation scope changed')
        from pathlib import Path
        from agent_execution import execution_scope
        from agent_execution.sessions import project_workspace
        saved = json.loads(row['context'])
        with execution_scope(workspace=None, repository_workspace=None):
            configured = project_workspace(context.project).resolve()
        if context.workspace is None or Path(saved['workspace']).resolve() != configured:
            raise PermissionError('Operation workspace configuration changed')
        actual = Path(context.workspace).resolve()
        binding = conn.execute('SELECT repository FROM task_workspaces WHERE owner=? AND task_id=?',
                               (self.owner, self.task)).fetchone()
        if active and binding and actual == configured:
            raise PermissionError('Operation must enter this task managed worktree')
        if actual != configured:
            # task_workspace may enter a retained, owner-bound Git worktree.
            # Trust its existing TaskStore/lifecycle registrations, never an
            # arbitrary context path or a model-supplied workspace reference.
            has_resources = conn.execute("SELECT 1 FROM sqlite_master WHERE name='wl_resources'").fetchone()
            resource = conn.execute('''SELECT 1 FROM wl_resources r
                JOIN wl_resource_refs f ON f.resource=r.id
                JOIN wl_session_owners o ON o.session=f.session
                WHERE r.kind='worktree' AND r.path=? AND r.task=? AND o.task_owner=?''',
                (str(actual), self.task, self.owner)).fetchone() if has_resources else None
            if (not binding or Path(binding['repository']).resolve() != configured
                    or context.repository_workspace is None or Path(context.repository_workspace).resolve() != configured
                    or not resource or not (actual / '.git').is_file()):
                raise PermissionError('Operation workspace is not this task managed worktree')
        if context.check_cancelled and context.check_cancelled():
            raise InterruptedError('Task cancelled before operation access')

    @staticmethod
    def _step(step):
        if not isinstance(step, str) or not step or len(step.encode('utf-8')) > 512:
            raise ValueError('Logical step ID must contain 1..512 UTF-8 bytes')

    def _row(self, conn, step):
        row = conn.execute('SELECT * FROM operation_ledger WHERE owner=? AND task_id=? AND step_id=?',
                           (self.owner, self.task, step)).fetchone()
        if not row:
            raise KeyError('Operation not found in this task')
        return dict(row)

    def get(self, step):
        self._step(step)
        with closing(self.store.tasks.connect()) as conn:
            self._scope(conn, active=False)
            return self._row(conn, step)

    @staticmethod
    def _capacity(conn, extra):
        from agent_execution.delivery import positive_limit, CapacityError
        used = conn.execute('''SELECT COALESCE(SUM(length(data)+length(CAST(owner AS BLOB))+
            length(CAST(task_id AS BLOB))+32+64+8),0) FROM temporal_artifacts''').fetchone()[0]
        # Reserve fence/reference metadata up front, including for prepared rows.
        used += conn.execute('''SELECT COALESCE(SUM(length(CAST(owner AS BLOB))+
            length(CAST(task_id AS BLOB))+length(CAST(step_id AS BLOB))+length(kind)+64+512),0)
            FROM operation_ledger''').fetchone()[0]
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_step_runs'").fetchone():
            used += conn.execute('SELECT COUNT(*)*1024 FROM temporal_step_runs').fetchone()[0]
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_human_answers'").fetchone():
            used += conn.execute("SELECT COALESCE(SUM(length(CAST(COALESCE(answer,'') AS BLOB))+1024),0) FROM temporal_human_answers").fetchone()[0]
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_approval_waits'").fetchone():
            used += conn.execute("SELECT COALESCE(SUM(length(CAST(payload AS BLOB))+length(CAST(binding AS BLOB))+2048),0) FROM temporal_approval_waits").fetchone()[0]
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_file_writes'").fetchone():
            used += conn.execute("SELECT COALESCE(SUM(length(CAST(plan AS BLOB))+length(CAST(COALESCE(observation,'') AS BLOB))+2048),0) FROM temporal_file_writes").fetchone()[0]
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_write_resolutions'").fetchone():
            used += conn.execute("SELECT COALESCE(SUM(length(CAST(data AS BLOB))+2048),0) FROM temporal_write_resolutions").fetchone()[0]
        if used + extra > positive_limit('AGENT_TEMPORAL_LEDGER_STORAGE_BYTES', 256*1024*1024):
            raise CapacityError('Temporal operation storage capacity reached')

    def prepare(self, step, kind, arguments):
        self._step(step)
        if kind not in {'model', 'tool', 'evaluation'}:
            raise ValueError('Unknown operation kind')
        fingerprint = digest([self.scope_digest, kind, arguments])
        with closing(self.store.tasks.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self._scope(conn, active=False)
            old = conn.execute('SELECT * FROM operation_ledger WHERE owner=? AND task_id=? AND step_id=?',
                               (self.owner, self.task, step)).fetchone()
            if old:
                if old['kind'] != kind or old['arguments_digest'] != fingerprint:
                    raise ValueError('Logical step cannot be reused with different arguments')
                if old['state'] != 'completed':
                    self._scope(conn)
                return dict(old)
            self._scope(conn)
            from agent_execution.delivery import positive_limit, CapacityError
            if conn.execute('SELECT COUNT(*) FROM operation_ledger').fetchone()[0] >= positive_limit('AGENT_TEMPORAL_OPERATION_LIMIT', 100000):
                raise CapacityError('Temporal operation count limit reached')
            self._capacity(conn, len((self.owner+self.task+step+kind).encode('utf-8'))+64+512)
            conn.execute('INSERT INTO operation_ledger(owner,task_id,step_id,kind,arguments_digest,state,updated) VALUES(?,?,?,?,?,?,?)',
                         (self.owner, self.task, step, kind, fingerprint, 'prepared', time.time()))
            return self._row(conn, step)

    def claim(self, step):
        self._step(step)
        with closing(self.store.tasks.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self._scope(conn, active=False)
            row = self._row(conn, step)
            if row['state'] == 'completed':
                return row
            self._scope(conn)
            if row['state'] != 'prepared':
                raise OperationUncertain('Operation already started or rejected; inspect effects before recovery')
            fence = uuid.uuid4().hex
            conn.execute("UPDATE operation_ledger SET state='running',fence=?,updated=? WHERE owner=? AND task_id=? AND step_id=?",
                         (fence, time.time(), self.owner, self.task, step))
            return self._row(conn, step)

    def uncertain(self, step, fence):
        with closing(self.store.tasks.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self._scope(conn)
            if conn.execute("UPDATE operation_ledger SET state='uncertain',updated=? WHERE owner=? AND task_id=? AND step_id=? AND fence=? AND state='running'",
                            (time.time(), self.owner, self.task, step, fence)).rowcount != 1:
                raise PermissionError('Operation result fence is stale')

    def reject(self, step):
        """Record a verified pre-execution refusal; started work is never relabeled."""
        self._step(step)
        with closing(self.store.tasks.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self._scope(conn)
            if conn.execute("UPDATE operation_ledger SET state='rejected_no_effect',updated=? WHERE owner=? AND task_id=? AND step_id=? AND state='prepared'",
                            (time.time(), self.owner, self.task, step)).rowcount != 1:
                raise PermissionError('Only a prepared operation can be rejected without effects')

    def _artifact(self, value):
        from agent_execution.delivery import positive_limit, CapacityError
        data = canonical(value).encode('utf-8')
        if len(data) > positive_limit('AGENT_TEMPORAL_ARTIFACT_MAX_BYTES', 2*1024*1024):
            raise CapacityError('Temporal artifact exceeds per-result byte limit')
        ref = {'schema_version': 1, 'id': uuid.uuid4().hex,
               'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
        return ref, data

    def complete(self, step, fence, result, evidence):
        result_ref, result_data = self._artifact(result)
        evidence_ref, evidence_data = self._artifact(evidence)
        with closing(self.store.tasks.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self._scope(conn)
            row = self._row(conn, step)
            if row['state'] != 'running' or row['fence'] != fence:
                raise PermissionError('Operation result fence is stale')
            artifact_metadata = 2 * (len((self.owner+self.task).encode('utf-8'))+32+64+8)
            self._capacity(conn, len(result_data) + len(evidence_data) + artifact_metadata)
            for ref, data in [(result_ref, result_data), (evidence_ref, evidence_data)]:
                conn.execute('INSERT INTO temporal_artifacts VALUES(?,?,?,?,?,?)',
                             (ref['id'], self.owner, self.task, ref['sha256'], ref['bytes'], data))
            conn.execute("UPDATE operation_ledger SET state='completed',result_ref=?,evidence_ref=?,updated=? WHERE owner=? AND task_id=? AND step_id=?",
                         (canonical(result_ref), canonical(evidence_ref), time.time(), self.owner, self.task, step))
        return result_ref

    def load(self, reference):
        if (not isinstance(reference, dict) or set(reference) != {'schema_version', 'id', 'sha256', 'bytes'}
                or type(reference['schema_version']) is not int or reference['schema_version'] != 1
                or type(reference['bytes']) is not int or reference['bytes'] < 0
                or not isinstance(reference['id'], str) or len(reference['id']) != 32
                or not isinstance(reference['sha256'], str) or len(reference['sha256']) != 64):
            raise ValueError('Invalid artifact reference')
        with closing(self.store.tasks.connect()) as conn:
            self._scope(conn, active=False)
            row = conn.execute('SELECT * FROM temporal_artifacts WHERE id=? AND owner=? AND task_id=?',
                               (reference['id'], self.owner, self.task)).fetchone()
            if not row:
                raise KeyError('Artifact not found in this task')
            data = row['data']
            if (not isinstance(data, bytes) or len(data) != reference['bytes'] or row['byte_count'] != len(data)
                    or row['sha256'] != reference['sha256'] or hashlib.sha256(data).hexdigest() != reference['sha256']):
                raise ValueError('Artifact integrity check failed')
            return json.loads(data)

    def run(self, step, kind, arguments, execute, *, encode=lambda value: value,
            decode=lambda value: value, evidence=lambda value: {}):
        self.prepare(step, kind, arguments)
        row = self.claim(step)
        if row['state'] == 'completed':
            # Validate both references before exposing a cached execution result.
            self.load(json.loads(row['evidence_ref']))
            return decode(self.load(json.loads(row['result_ref'])))
        try:
            value = execute()
            self.complete(step, row['fence'], encode(value), evidence(value))
            return value
        except BaseException:
            try:
                self.uncertain(step, row['fence'])
            except Exception:
                pass  # Cancellation/deletion must never be undone by error reporting.
            raise
