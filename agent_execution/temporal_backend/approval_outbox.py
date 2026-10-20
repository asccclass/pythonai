"""SDK-free approval bindings and decision outbox in the approval database."""
from contextlib import closing
import hashlib
import json
import re
import time
import uuid


def binding_data(binding):
    fields = {'submission_id', 'queue', 'operation_id', 'owner', 'task_id'}
    if (not isinstance(binding, dict) or set(binding) != fields
            or any(not isinstance(v, str) or not v or len(v.encode()) > 1024 for v in binding.values())
            or not re.fullmatch('[a-f0-9]{32}', binding['submission_id'])):
        raise ValueError('Invalid approval operation binding')
    return json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS temporal_approval_bindings(
        binding TEXT PRIMARY KEY, approval_id TEXT NOT NULL UNIQUE)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS temporal_approval_outbox(
        id TEXT PRIMARY KEY, approval_id TEXT NOT NULL, state TEXT NOT NULL,
        delivery TEXT NOT NULL, created REAL NOT NULL, UNIQUE(approval_id,state))""")


def get_binding(conn, approval_id):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_approval_bindings'").fetchone():
        return None
    row = conn.execute('SELECT binding FROM temporal_approval_bindings WHERE approval_id=?', (approval_id,)).fetchone()
    return row[0] if row else None

def validate_target(target):
    """Human transports still verify their identity/session/admin role first."""
    binding_data(target)
    from .store import Store
    from .contracts import APPROVAL_WORKFLOW_NAME
    from agent_execution.tasks import assert_owner_active
    store = Store()
    row = store.get(target['submission_id'], target['owner'])
    if (store.workflow_type(row) != APPROVAL_WORKFLOW_NAME or row['queue'] != target['queue']
            or row['task_id'] != target['task_id'] or row['state'] != 'running'):
        raise PermissionError('Approval target is no longer active')
    with closing(store.tasks.connect()) as conn:
        assert_owner_active(conn, target['owner'])
        task = conn.execute('SELECT state FROM tasks WHERE owner=? AND id=?',
                            (target['owner'], target['task_id'])).fetchone()
        intent = conn.execute('SELECT binding FROM temporal_approval_waits WHERE submission_id=? AND operation_id=?',
                              (target['submission_id'], target['operation_id'])).fetchone()
        if not task or task['state'] not in {'pending', 'suspended'} or not intent or intent[0] != binding_data(target):
            raise PermissionError('Approval wait was cancelled, deleted, or already executed')


def request_bound(store, identity, session, action, arguments, ttl, binding):
    from agent_execution.approvals import canonical
    from agent_execution.delivery import positive_limit, CapacityError
    import math
    if type(ttl) not in {int, float} or not math.isfinite(ttl) or ttl <= 0:
        raise ValueError('Invalid approval lifetime')
    target = binding_data(binding)
    data = canonical(arguments)
    if len(data.encode()) > 1024*1024:
        raise CapacityError('Approval arguments exceed 1 MiB')
    fingerprint = hashlib.sha256(data.encode()).hexdigest()
    with closing(store.connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        schema(conn)
        old = conn.execute("""SELECT a.* FROM temporal_approval_bindings b
            JOIN approvals a ON a.id=b.approval_id WHERE b.binding=?""", (target,)).fetchone()
        if old:
            if (old['identity'], old['session'], old['action'], old['digest']) != (identity, session, action, fingerprint):
                raise PermissionError('Approval binding cannot change its operation')
            return old['id']  # Includes decided/expired/consumed: never mint another grant.
        if conn.execute('SELECT COUNT(*) FROM temporal_approval_bindings').fetchone()[0] >= positive_limit('AGENT_TEMPORAL_OPERATION_LIMIT', 100000):
            raise CapacityError('Approval binding capacity reached')
        used = conn.execute("SELECT COALESCE(SUM(length(CAST(a.arguments AS BLOB))+length(CAST(b.binding AS BLOB))+1024),0) FROM temporal_approval_bindings b JOIN approvals a ON a.id=b.approval_id").fetchone()[0]
        used += conn.execute('SELECT COUNT(*)*512 FROM temporal_approval_outbox').fetchone()[0]
        if used+len(data.encode())+len(target.encode())+3072 > positive_limit('AGENT_TEMPORAL_LEDGER_STORAGE_BYTES', 256*1024*1024):
            raise CapacityError('Approval authority storage capacity reached')
        key = uuid.uuid4().hex
        conn.execute('INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?)',
                     (key, identity, session, action, data, fingerprint, time.time()+ttl, 'pending'))
        conn.execute('INSERT INTO temporal_approval_bindings VALUES(?,?)', (target, key))
        store.audit(conn, key, identity, session, 'requested')
        return key


def record_decision(conn, approval_id, state):
    if get_binding(conn, approval_id) is not None:
        if conn.execute('SELECT 1 FROM temporal_approval_outbox WHERE approval_id=? AND state=?', (approval_id, state)).fetchone():
            return
        from agent_execution.delivery import positive_limit, CapacityError
        used = conn.execute("SELECT COALESCE(SUM(length(CAST(a.arguments AS BLOB))+length(CAST(b.binding AS BLOB))+1024),0) FROM temporal_approval_bindings b JOIN approvals a ON a.id=b.approval_id").fetchone()[0]
        used += conn.execute('SELECT COUNT(*)*512 FROM temporal_approval_outbox').fetchone()[0]
        if used+512 > positive_limit('AGENT_TEMPORAL_LEDGER_STORAGE_BYTES', 256*1024*1024):
            raise CapacityError('Approval decision outbox capacity reached')
        conn.execute("INSERT OR IGNORE INTO temporal_approval_outbox VALUES(?,?,?,'pending',?)",
                     (uuid.uuid4().hex, approval_id, state, time.time()))


def same_decision(conn, approval_id, identity, session, state):
    if get_binding(conn, approval_id) is None:
        return False
    row = conn.execute('SELECT state,expires FROM approvals WHERE id=? AND identity=? AND session=?',
                       (approval_id, identity, session)).fetchone()
    return bool(row and row['state'] == state and row['expires'] > time.time())


def validate_consumption(conn, approval_id, binding):
    expected = get_binding(conn, approval_id)
    actual = binding_data(binding) if binding is not None else None
    if actual != expected:
        raise PermissionError('Approval requires its original durable operation binding')


def pending(store, *, queues=None):
    with closing(store.connect()) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_approval_outbox'").fetchone():
            return []
        condition, parameters = '', ()
        if queues is not None:
            if not queues:
                return []
            condition = " AND json_extract(b.binding,'$.queue') IN (" + ','.join('?' for _ in queues) + ')'
            parameters = tuple(queues)
        return [dict(row) for row in conn.execute("""SELECT e.*,b.binding FROM temporal_approval_outbox e
            JOIN temporal_approval_bindings b ON b.approval_id=e.approval_id
            WHERE e.delivery='pending'""" + condition + ' ORDER BY e.created LIMIT 100', parameters)]


def sent(store, event_id):
    with closing(store.connect()) as conn, conn:
        conn.execute("UPDATE temporal_approval_outbox SET delivery='sent' WHERE id=? AND delivery='pending'", (event_id,))
