"""Durable cancellation delivery; no SDK dependency on the SQLite task path."""
from contextlib import closing
from datetime import timedelta
import time
import sqlite3

from agent_execution.delivery import CapacityError


def request(conn, owner, task=None):
    # Legacy SQLite cancellation must not install any optional Temporal tables.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_cancellations'").fetchone():
        return
    rows = conn.execute("""SELECT s.* FROM temporal_submissions s JOIN tasks t
        ON t.owner=s.owner AND t.id=s.task_id WHERE s.owner=? AND t.state='cancelled'
        AND s.state IN ('pending','dispatched','running','cancelled')""" +
        (' AND s.task_id=?' if task is not None else ''), (owner, task) if task is not None else (owner,)).fetchall()
    from .ledger import fence_operations
    for row in rows:
        fence_operations(conn, owner, row['task_id'])
        conn.execute("UPDATE temporal_submissions SET state='cancelled',updated=? WHERE id=?", (time.time(), row['id']))
        from .publication import final_intent
        final_intent(conn,dict(row))
        conn.execute('INSERT OR IGNORE INTO temporal_cancellations(submission_id,delivery) VALUES(?,?)', (row['id'], 'pending'))


def pending(store, settings, queues):
    if not queues:
        return []
    with closing(store.tasks.connect()) as conn:
        return [dict(row) for row in conn.execute("""SELECT s.* FROM temporal_submissions s
            JOIN temporal_cancellations c ON c.submission_id=s.id
            WHERE c.delivery='pending' AND s.state='cancelled' AND s.address=? AND s.namespace=?
            AND s.queue IN (""" + ','.join('?' for _ in queues) + ') ORDER BY s.updated LIMIT 100',
            (settings.address, settings.namespace, *sorted(queues)))]


async def dispatch(store, row, client):
    # Re-read our persisted target; never cancel an ID supplied by a signal or model.
    current = store.get(row['id'], row['owner'])
    if current['state'] != 'cancelled':
        raise PermissionError('Cancellation requires a committed local fence')
    try:
        from .approval_gate import cancel_waits
        cancel_waits(store, current)
    except (OSError, sqlite3.Error, ValueError, CapacityError):
        pass  # Local fences remain authoritative if the approval DB is unavailable.
    try:
        await client.get_workflow_handle(current['workflow_id']).cancel(rpc_timeout=timedelta(seconds=10))
    except Exception as error:
        # Cancel is idempotent. A closed/never-started Workflow is already safe, but
        # an unavailable service or unknown RPC result must retain the outbox.
        from temporalio.service import RPCError, RPCStatusCode
        if not isinstance(error, RPCError) or error.status != RPCStatusCode.NOT_FOUND:
            raise
    with closing(store.tasks.connect()) as conn, conn:
        conn.execute("UPDATE temporal_cancellations SET delivery='sent' WHERE submission_id=?", (current['id'],))
