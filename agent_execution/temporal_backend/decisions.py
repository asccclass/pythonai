"""Owner-scoped human answers and a transactional notification outbox.

Signals carry no authority or answer text. Only a metadata Activity may consume
the answer while atomically advancing the persisted continuation.
"""
from contextlib import closing
import time

from agent_execution.tasks import assert_owner_active
from .contracts import HUMAN_SIGNAL, SCHEMA_VERSION, HUMAN_WORKFLOW_NAME, APPROVAL_WORKFLOW_NAME


class Decisions:
    def __init__(self, store):
        self.store = store
        with closing(store.tasks.connect()) as conn, conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS temporal_human_answers(
                submission_id TEXT NOT NULL, revision INTEGER NOT NULL,
                call_id TEXT NOT NULL, expires REAL NOT NULL, answer TEXT,
                state TEXT NOT NULL, delivery TEXT NOT NULL,
                PRIMARY KEY(submission_id,revision))''')

    def create(self, conn, cursor, task):
        from .ledger import Ledger
        Ledger._capacity(conn, 2048)  # New wait row plus the following cursor.
        call = task['checkpoint'].get('suspended_tool_call_id')
        if not isinstance(call, str) or not call:
            raise ValueError('Missing suspended tool call')
        if self.store.workflow_type(self.store.get(cursor['submission_id'])) not in {HUMAN_WORKFLOW_NAME, APPROVAL_WORKFLOW_NAME}:
            raise PermissionError('Human waiting requires V2 or a later step protocol')
        contract = conn.execute('SELECT contract FROM temporal_budget_contracts WHERE submission_id=?',
                                (cursor['submission_id'],)).fetchone()
        if contract is None:
            raise PermissionError('Human waiting requires a pinned root deadline')
        import json
        expires = min(time.time()+300, json.loads(contract[0])['deadline'])
        conn.execute("INSERT INTO temporal_human_answers VALUES(?,?,?,?,NULL,'waiting','none')",
                     (cursor['submission_id'], cursor['revision'], call, expires))

    def answer(self, key, owner, call_id, answer):
        if not isinstance(answer, str) or not answer.strip() or len(answer.encode()) > 1024*1024:
            raise ValueError('Human answer must contain text and fit within 1 MiB')
        submission = self.store.get(key, owner)
        if self.store.workflow_type(submission) not in {HUMAN_WORKFLOW_NAME, APPROVAL_WORKFLOW_NAME}:
            raise PermissionError('Human answers require the step protocol')
        with closing(self.store.tasks.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            assert_owner_active(conn, owner)
            current = conn.execute('SELECT state FROM temporal_submissions WHERE id=?', (key,)).fetchone()
            task = conn.execute('SELECT state FROM tasks WHERE owner=? AND id=?',
                                (owner, submission['task_id'])).fetchone()
            if current['state'] not in {'running', 'completed'} or not task or task['state'] in {'cancelled', 'failed', 'uncertain'}:
                raise PermissionError('Human answer fenced by task state')
            row = conn.execute('SELECT * FROM temporal_human_answers WHERE submission_id=? AND call_id=? ORDER BY revision DESC LIMIT 1',
                               (key, call_id)).fetchone()
            if row is None:
                raise PermissionError('Answer does not match a persisted human wait')
            if row['state'] in {'answered', 'consumed'} and row['answer'] == answer:
                return  # An identical transport retry never creates another event.
            if row['state'] != 'waiting' or row['expires'] <= time.time() or task['state'] != 'suspended':
                raise PermissionError('Human wait expired or already decided')
            from .ledger import Ledger
            Ledger._capacity(conn, len(answer.encode()))
            conn.execute("UPDATE temporal_human_answers SET answer=?,state='answered',delivery='pending' WHERE submission_id=? AND revision=?",
                         (answer, key, row['revision']))

    def pending(self):
        with closing(self.store.tasks.connect()) as conn:
            return [dict(row) for row in conn.execute('''SELECT s.*,a.revision FROM temporal_human_answers a
                JOIN temporal_submissions s ON s.id=a.submission_id
                WHERE a.delivery='pending' AND s.state='running' ORDER BY s.updated LIMIT 100''')]

    async def dispatch(self, client, row):
        # Do not use signal-with-start: a deleted/closed task must never revive.
        await client.get_workflow_handle(row['workflow_id']).signal(HUMAN_SIGNAL,
            {'schema_version': SCHEMA_VERSION, 'submission_id': row['id']})
        with closing(self.store.tasks.connect()) as conn, conn:
            conn.execute("UPDATE temporal_human_answers SET delivery='sent' WHERE submission_id=? AND revision=? AND delivery='pending'",
                         (row['id'], row['revision']))
