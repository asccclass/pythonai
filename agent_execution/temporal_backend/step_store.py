"""Fenced continuation projections; payloads remain in owner-scoped SQLite artifacts."""
from contextlib import closing
import json
import time
import uuid

from agent_execution.tasks import assert_owner_active
from .contracts import STEP_WORKFLOW_NAME, HUMAN_WORKFLOW_NAME, STEP_LIMIT, validate_cursor, validate_snapshot
from .ledger import Ledger, OperationUncertain
from .store import canonical


class StepStore:
    def __init__(self, store):
        self.store = store
        from .decisions import Decisions
        self.decisions = Decisions(store)
        with closing(store.tasks.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('CREATE TABLE IF NOT EXISTS temporal_step_schema(version INTEGER PRIMARY KEY)')
            versions = [r[0] for r in conn.execute('SELECT version FROM temporal_step_schema')]
            if versions and versions != [1]:
                raise ValueError('Unsupported Temporal continuation schema')
            conn.execute('INSERT OR IGNORE INTO temporal_step_schema VALUES(1)')
            conn.execute('''CREATE TABLE IF NOT EXISTS temporal_step_runs(
                submission_id TEXT NOT NULL, revision INTEGER NOT NULL, next_kind TEXT NOT NULL,
                state_ref TEXT NOT NULL, status TEXT NOT NULL, fence TEXT, output TEXT,
                PRIMARY KEY(submission_id,revision))''')

    def _row(self, conn, cursor):
        validate_cursor(cursor)
        row = conn.execute('SELECT * FROM temporal_step_runs WHERE submission_id=? AND revision=?',
                           (cursor['submission_id'], cursor['revision'])).fetchone()
        if not row or row['state_ref'] != canonical(cursor['state_ref']) or row['next_kind'] != cursor['next_kind']:
            raise PermissionError('Continuation does not match its persisted cursor')
        return row

    @staticmethod
    def snapshot(task):
        return validate_snapshot({'schema_version': 1, 'phase': task['phase'], 'checkpoint': task['checkpoint'],
                                  'task_state': task['state'], 'reply': task['reply']})

    @staticmethod
    def _put(conn, ledger, value):
        ledger._scope(conn, active=False)
        ref, data = ledger._artifact(value)
        ledger._capacity(conn, len(data)+len((ledger.owner+ledger.task).encode())+104+1024)
        conn.execute('INSERT INTO temporal_artifacts VALUES(?,?,?,?,?,?)',
                     (ref['id'], ledger.owner, ledger.task, ref['sha256'], ref['bytes'], data))
        return ref

    @staticmethod
    def cursor(key, revision, kind, ref):
        return validate_cursor({'schema_version': 1, 'submission_id': key, 'revision': revision,
                                'next_kind': kind, 'state_ref': ref})

    def bootstrap_claim(self, key, settings):
        """Only bootstrap may adopt a pre-cursor running claim; it performs no business I/O."""
        row = self.store.get(key)
        kind = self.store.workflow_type(row)
        if kind not in {STEP_WORKFLOW_NAME, HUMAN_WORKFLOW_NAME}:
            raise PermissionError('Submission is not bound to the step protocol')
        queue = self.store.queue_for('human' if kind == HUMAN_WORKFLOW_NAME else 'steps')
        if (row['address'], row['namespace'], row['queue']) != (settings.address, settings.namespace, queue):
            raise PermissionError('Step submission targets another host or namespace')
        with closing(self.store.tasks.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            assert_owner_active(conn, row['owner'])
            task = conn.execute('SELECT state FROM tasks WHERE owner=? AND id=?', (row['owner'], row['task_id'])).fetchone()
            if not task or task['state'] == 'cancelled':
                raise PermissionError('Step task was cancelled or removed')
            current = conn.execute('SELECT * FROM temporal_submissions WHERE id=?', (key,)).fetchone()
            if current['state'] in {'pending', 'dispatched'}:
                conn.execute("UPDATE temporal_submissions SET state='running',fence=?,updated=? WHERE id=?",
                             (uuid.uuid4().hex, time.time(), key))
            elif current['state'] not in {'running', 'completed', 'failed'}:
                raise OperationUncertain('Step submission requires inspection')
        return self.store.get(key)

    def bootstrap(self, row, ledger):
        if row['id'] != ledger.key:
            raise PermissionError('Continuation is outside this task')
        with closing(self.store.tasks.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            ledger._scope(conn, active=False)
            old = conn.execute('SELECT * FROM temporal_step_runs WHERE submission_id=? AND revision=0', (row['id'],)).fetchone()
            if old:
                cursor = self.cursor(row['id'], 0, old['next_kind'], json.loads(old['state_ref']))
                ledger.load(cursor['state_ref'])
                return cursor
            task = self.store.tasks.get(row['owner'], row['task_id'])
            if task['state'] != 'pending':
                raise PermissionError('Cannot bootstrap a previously executed task')
            ref = self._put(conn, ledger, self.snapshot(task))
            conn.execute("INSERT INTO temporal_step_runs VALUES(?,?,?,?,'ready',NULL,NULL)",
                         (row['id'], 0, 'model', canonical(ref)))
            return self.cursor(row['id'], 0, 'model', ref)

    def current(self, cursor, fence):
        with closing(self.store.tasks.connect()) as conn:
            row = self._row(conn, cursor)
            return row['status'] == 'running' and row['fence'] == fence

    def claim(self, cursor, ledger):
        if cursor['submission_id'] != ledger.key:
            raise PermissionError('Continuation is outside this task')
        validate_snapshot(ledger.load(cursor['state_ref']))
        with closing(self.store.tasks.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            ledger._scope(conn, active=False)
            row = self._row(conn, cursor)
            if row['status'] == 'completed':
                result = validate_cursor(json.loads(row['output']))
                ledger.load(result['state_ref'])
                return None, result
            if cursor['revision'] >= STEP_LIMIT:
                raise ValueError('Continuation step limit reached before execution')
            ledger._scope(conn)
            if row['status'] != 'ready':
                raise OperationUncertain('Step already started; use verified projection recovery')
            fence = uuid.uuid4().hex
            conn.execute("UPDATE temporal_step_runs SET status='running',fence=? WHERE submission_id=? AND revision=?",
                         (fence, cursor['submission_id'], cursor['revision']))
            return fence, None

    def _commit(self, conn, cursor, fence, ledger, kind, task):
        row = self._row(conn, cursor)
        if row['status'] == 'completed':
            result = validate_cursor(json.loads(row['output']))
            ledger.load(result['state_ref'])
            return result
        if row['status'] != 'running' or row['fence'] != fence:
            raise PermissionError('Continuation result fence is stale')
        if kind == 'human':
            if task['state'] != 'suspended' or task['phase'] != 'waiting_user':
                raise PermissionError('Human wait requires a confirmed suspension')
        elif kind == 'finalize':
            if task['state'] not in {'completed', 'failed'}:
                raise PermissionError('Cannot finalize a nonterminal task')
        else:
            from agent_execution.turn_control.continuation import next_kind
            if task['state'] != 'pending' or next_kind(task['phase'], task['checkpoint']) != kind:
                raise PermissionError('Continuation result does not match its saved boundary')
        revision = cursor['revision']+1
        if revision > STEP_LIMIT:
            raise ValueError('Continuation step limit reached')
        ref = self._put(conn, ledger, self.snapshot(task))
        result = self.cursor(cursor['submission_id'], revision, kind, ref)
        if kind == 'human':
            self.decisions.create(conn, result, task)
        conn.execute("INSERT INTO temporal_step_runs VALUES(?,?,?,?,'ready',NULL,NULL)",
                     (cursor['submission_id'], revision, kind, canonical(ref)))
        conn.execute("UPDATE temporal_step_runs SET status='completed',output=? WHERE submission_id=? AND revision=? AND fence=?",
                     (canonical(result), cursor['submission_id'], cursor['revision'], fence))
        return result

    def commit(self, cursor, fence, ledger, kind):
        if cursor['submission_id'] != ledger.key:
            raise PermissionError('Continuation is outside this task')
        with closing(self.store.tasks.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            ledger._scope(conn, active=False)
            task = self.store.tasks.get(ledger.owner, ledger.task)
            return self._commit(conn, cursor, fence, ledger, kind, task)

    def recover(self, cursor, ledger):
        """Recover only complete boundaries or proven pre-I/O state; never rerun unknown effects."""
        if cursor['submission_id'] != ledger.key:
            raise PermissionError('Continuation is outside this task')
        validate_snapshot(ledger.load(cursor['state_ref']))
        with closing(self.store.tasks.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            ledger._scope(conn, active=False)
            run = self._row(conn, cursor)
            if run['status'] == 'completed':
                result = validate_cursor(json.loads(run['output']))
                ledger.load(result['state_ref'])
                return result
            if run['status'] == 'ready':
                return cursor
            task = self.store.tasks.get(ledger.owner, ledger.task)
            phase, saved = task['phase'], task['checkpoint']
            if task['state'] in {'completed', 'failed'}:
                return self._commit(conn, cursor, run['fence'], ledger, 'finalize', task)
            if task['state'] == 'suspended' and phase == 'waiting_user':
                return self._commit(conn, cursor, run['fence'], ledger, 'human', task)
            if task['state'] not in {'pending', 'running'}:
                raise OperationUncertain('Task requires human resolution')
            if phase == 'step_ready':
                from agent_execution.turn_control.continuation import next_kind
                kind = next_kind(phase, saved)
            elif phase in {'model_received', 'tool_done', 'evaluation_done'}:
                from agent_execution.turn_control.continuation import next_kind
                kind = next_kind(phase, saved)
            else:
                operation = None
                if phase == 'model_pending':
                    operation = f"model:{saved['iteration']}:attempt:{saved.get('model_attempt', 1)}"
                elif phase == 'evaluation_pending':
                    operation = f"evaluation:{saved['iteration']}:{saved.get('eval_retries', 0)}"
                elif phase == 'tool_started':
                    raise OperationUncertain('Tool or its completion hooks may have produced effects')
                elif phase == 'step_preparing':
                    raise OperationUncertain('Preparation hooks may have produced effects')
                elif phase != 'temporal_pending':
                    raise OperationUncertain('Unsupported recovery boundary')
                op = conn.execute('SELECT state FROM operation_ledger WHERE owner=? AND task_id=? AND step_id=?',
                                  (ledger.owner, ledger.task, operation)).fetchone() if operation else None
                if op and op['state'] != 'prepared':
                    raise OperationUncertain('Operation or completion hooks may have executed')
                # Invalidate the old Activity before it can begin any model/tool I/O.
                conn.execute("UPDATE temporal_step_runs SET status='ready',fence=NULL WHERE submission_id=? AND revision=?",
                             (cursor['submission_id'], cursor['revision']))
                conn.execute("UPDATE tasks SET state='pending',lease_owner=NULL,lease_until=NULL,reply=NULL WHERE owner=? AND id=?",
                             (ledger.owner, ledger.task))
                return cursor
            conn.execute("UPDATE tasks SET state='pending',lease_owner=NULL,lease_until=NULL,reply=NULL WHERE owner=? AND id=?",
                         (ledger.owner, ledger.task))
            task = {**task, 'state': 'pending'}
            return self._commit(conn, cursor, run['fence'], ledger, kind, task)

    def human(self, cursor, ledger):
        """Inspect or consume once; waiting itself occupies no Activity thread."""
        if cursor['submission_id'] != ledger.key or cursor['next_kind'] != 'human':
            raise PermissionError('Not this task human cursor')
        validate_snapshot(ledger.load(cursor['state_ref']))
        with closing(self.store.tasks.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            ledger._scope(conn, active=False)
            run = self._row(conn, cursor)
            if run['status'] == 'completed':
                result = validate_cursor(json.loads(run['output']))
                ledger.load(result['state_ref'])
                return {'cursor': result, 'expires': 0}
            answer = conn.execute('SELECT * FROM temporal_human_answers WHERE submission_id=? AND revision=?',
                                  (ledger.key, cursor['revision'])).fetchone()
            task = self.store.tasks.get(ledger.owner, ledger.task)
            if (not answer or task['state'] != 'suspended' or task['phase'] != 'waiting_user'
                    or task['checkpoint'].get('suspended_tool_call_id') != answer['call_id']):
                raise PermissionError('Human wait no longer matches its task')
            if answer['state'] == 'waiting' and answer['expires'] > time.time():
                return {'cursor': cursor, 'expires': answer['expires']}
            saved = task['checkpoint']
            # Delivery after TTL does not extend root deadlines or authorize I/O.
            if answer['expires'] <= time.time():
                task = {**task, 'state': 'failed', 'reply': 'Error: Human answer wait expired'}
                kind, decision_state = 'finalize', 'expired'
            elif answer['state'] == 'answered':
                saved['messages'].append({'role': 'tool', 'tool_call_id': saved.pop('suspended_tool_call_id'),
                                          'content': answer['answer']})
                from agent_execution.turn_control.continuation import next_kind
                kind = next_kind('tool_done', saved)
                task = {**task, 'state': 'pending', 'phase': 'tool_done', 'reply': None}
                decision_state = 'consumed'
            else:
                raise PermissionError('Human decision already consumed without a projection')
            data = canonical(saved)
            self.store.tasks.capacity(conn, len(data.encode())-len(canonical(self.store.tasks.get(ledger.owner, ledger.task)['checkpoint']).encode()))
            conn.execute('UPDATE tasks SET state=?,phase=?,checkpoint=?,reply=?,updated=? WHERE owner=? AND id=?',
                         (task['state'], task['phase'], data, task['reply'], time.time(), ledger.owner, ledger.task))
            fence = uuid.uuid4().hex
            conn.execute("UPDATE temporal_step_runs SET status='running',fence=? WHERE submission_id=? AND revision=?",
                         (fence, ledger.key, cursor['revision']))
            result = self._commit(conn, cursor, fence, ledger, kind, task)
            conn.execute('UPDATE temporal_human_answers SET state=? WHERE submission_id=? AND revision=?',
                         (decision_state, ledger.key, cursor['revision']))
            return {'cursor': result, 'expires': 0}
