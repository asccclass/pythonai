"""Owner-scoped, conservative retention for closed DAG Workflow History chains."""
from contextlib import closing
import json
import re
import time

from .store import canonical, digest

TERMINAL_NODES = {'completed', 'merged'}


def install(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS temporal_dag_gc(
        graph_id TEXT PRIMARY KEY,owner_hash TEXT NOT NULL,target_digest TEXT NOT NULL,
        state TEXT NOT NULL,updated REAL NOT NULL,receipt TEXT NOT NULL)""")


def _tables(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _eligible(graphs, conn, graph, owner_hash, before, *, check_age=True):
    from agent_execution.temporal_backend.admission import owner_key
    from agent_execution.temporal_backend.dag import busy

    if owner_key(graph['owner']) != owner_hash or graph['state'] != 'completed' or not graph['sealed']:
        return False
    if check_age and graph['updated'] >= before:
        return False
    if graph['remote_deleted']:
        return False
    root = conn.execute('SELECT state,lease_until FROM tasks WHERE owner=? AND id=?',
                        (graph['owner'], graph['root'])).fetchone()
    if not root or root['state'] != 'completed' or (root['lease_until'] or 0) > time.time():
        return False
    if busy(conn, [graph['owner']]):
        return False

    nodes = graphs.nodes(conn, graph)
    if not nodes or any(node['state'] not in TERMINAL_NODES or (node['expires'] or 0) > time.time()
                        for node in nodes):
        return False
    from agent_execution.coordinator import node_task_owner
    for node in nodes:
        child = conn.execute('SELECT state,lease_until FROM tasks WHERE owner=? AND id=?',
            (node_task_owner(node['payload']), 'subagent:'+node['id'])).fetchone()
        if not child or child['state'] != 'completed' or (child['lease_until'] or 0) > time.time():
            return False
    names = _tables(conn)
    if 'temporal_dag_operations' in names and conn.execute(
            "SELECT 1 FROM temporal_dag_operations WHERE graph_id=? AND state NOT IN ('completed','cancelled') LIMIT 1",
            (graph['id'],)).fetchone():
        return False
    if 'wl_sessions' in names:
        anchor = graphs.coordinator.get(graph['owner'], graph['anchor'])
        session = anchor['payload'].get('session')
        lifecycle = conn.execute('SELECT state FROM wl_sessions WHERE id=?', (session,)).fetchone()
        if lifecycle and lifecycle['state'] in {'deleting', 'deleted'}:
            return False
    return True


def candidates(graphs, owner_hash, before, limit=100):
    if not re.fullmatch('[0-9a-f]{64}', owner_hash or ''):
        raise ValueError('DAG retention requires an exact owner hash')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('DAG retention limit must be 1..100')
    if type(before) not in {int, float} or not 0 < before < time.time():
        raise ValueError('DAG retention cutoff must be in the past')
    with closing(graphs.coordinator.connect()) as conn:
        names = _tables(conn)
        if 'temporal_agent_graphs' not in names:
            return []
        result = []
        query = """SELECT * FROM temporal_agent_graphs g
            WHERE state='completed' AND sealed=1 AND (updated<? OR EXISTS(
              SELECT 1 FROM temporal_dag_gc d WHERE d.graph_id=g.id AND d.state='pending'))
            ORDER BY updated,id"""
        if 'temporal_dag_gc' not in names:
            query = """SELECT * FROM temporal_agent_graphs
                WHERE state='completed' AND sealed=1 AND updated<? ORDER BY updated,id"""
        rows = conn.execute(query, (before,))
        for value in rows:
            graph = dict(value)
            intent = conn.execute('SELECT owner_hash,state FROM temporal_dag_gc WHERE graph_id=?',
                                  (graph['id'],)).fetchone() if 'temporal_dag_gc' in names else None
            if intent and intent['owner_hash'] != owner_hash:
                continue
            if _eligible(graphs, conn, graph, owner_hash, before, check_age=not bool(intent)):
                result.append(graph['id'])
                if len(result) == limit:
                    break
        return result


def _targets(graphs, conn, graph):
    from .dag import Graphs
    from .dag_lifecycle import operation_ids, Lifecycle

    nodes = graphs.nodes(conn, graph)
    operations = operation_ids(conn, graph['id'])
    return sorted([Graphs.workflow_id(graph['id'])]
        + [Graphs.child_id(graph['id'], node['id']) for node in nodes]
        + [Lifecycle.workflow_id(operation) for operation in operations])


async def collect(graphs, graph_id, owner_hash, before):
    """Delete aged closed remote histories; retain local task evidence for normal retention."""
    from datetime import timedelta
    from .dag import busy
    from .dag_erasure import ErasurePending, delete_history
    from .config import Settings
    from .client import connect

    with closing(graphs.coordinator.connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM temporal_agent_graphs WHERE id=?', (graph_id,)).fetchone()
        if not row:
            raise PermissionError('DAG is no longer eligible for History retention')
        graph = dict(row)
        targets = _targets(graphs, conn, graph)
        target_digest = digest(targets)
        install(conn)
        intent = conn.execute('SELECT * FROM temporal_dag_gc WHERE graph_id=?', (graph_id,)).fetchone()
        if not _eligible(graphs, conn, graph, owner_hash, before, check_age=not bool(intent)):
            raise PermissionError('DAG is no longer eligible for History retention')
        if intent and (intent['owner_hash'] != owner_hash or intent['target_digest'] != target_digest):
            raise ErasurePending('DAG retention target set changed; preserve for review')
        if intent and intent['state'] != 'pending':
            raise PermissionError('DAG History retention is already complete')
        if not intent:
            conn.execute("INSERT INTO temporal_dag_gc VALUES(?,?,?,'pending',?,'{}')",
                         (graph_id, owner_hash, target_digest, time.time()))

    graphs.configuration(graph)
    client = await connect(Settings.load())
    if client.namespace != graph['namespace']:
        raise PermissionError('DAG History retention connected to the wrong namespace')
    timeout = timedelta(seconds=10)
    for workflow_id in targets:
        await delete_history(client, graph['namespace'], workflow_id, timeout)

    with closing(graphs.coordinator.connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM temporal_agent_graphs WHERE id=?', (graph_id,)).fetchone()
        if not row:
            # A concurrent, already-authorized session erasure may have scrubbed it.
            return {'graph_ref': digest(graph_id), 'state': 'superseded_by_erasure'}
        current = dict(row)
        if not _eligible(graphs, conn, current, owner_hash, before, check_age=False) or busy(conn, [current['owner']]):
            raise ErasurePending('DAG changed or became active during History retention')
        if digest(_targets(graphs, conn, current)) != target_digest:
            raise ErasurePending('DAG retention target set changed; preserve for review')
        receipt = {'version': 1, 'history': 'not_found_verified',
            'visibility': 'not_found_verified', 'target_count': len(targets),
            'targets_sha256': target_digest, 'archive': 'external_policy_required',
            'backups': 'external_policy_required', 'checked': time.time()}
        conn.execute('UPDATE temporal_agent_graphs SET remote_deleted=1,updated=? WHERE id=?',
                     (time.time(), graph_id))
        conn.execute("UPDATE temporal_dag_gc SET state='completed',updated=?,receipt=? WHERE graph_id=?",
                     (time.time(), canonical(receipt), graph_id))
    return {'graph_ref': digest(graph_id), 'state': 'history_collected', 'receipt': receipt}
