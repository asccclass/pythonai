import sqlite3

from agent_execution.temporal_backend.drain_status import inspect


def make_database(path):
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE tasks(owner TEXT,id TEXT,state TEXT);
            CREATE TABLE execution_bindings(owner TEXT,task_id TEXT,backend TEXT);
            CREATE TABLE temporal_submissions(id TEXT,state TEXT);
            CREATE TABLE temporal_protocols(submission_id TEXT,workflow_type TEXT);
            CREATE TABLE temporal_cancellations(submission_id TEXT,delivery TEXT);
        ''')
        conn.executemany('INSERT INTO tasks VALUES(?,?,?)', [
            ('private-owner', 'legacy-running', 'running'),
            ('private-owner', 'temporal-done', 'completed'),
            ('private-owner', 'legacy-done', 'completed')])
        conn.execute("INSERT INTO execution_bindings VALUES('private-owner','temporal-done','temporal')")
        conn.executemany('INSERT INTO temporal_submissions VALUES(?,?)', [
            ('opaque-bridge', 'running'), ('opaque-v1', 'completed'), ('opaque-uncertain', 'uncertain')])
        conn.execute("INSERT INTO temporal_protocols VALUES('opaque-v1','AgentTaskWorkflowV1')")
        conn.execute("INSERT INTO temporal_cancellations VALUES('opaque-bridge','pending')")


def test_drain_inventory_is_read_only_redacted_and_never_authorizes_removal(tmp_path):
    database = tmp_path/'tasks.db'
    make_database(database)
    before = database.read_bytes()
    report = inspect(database)

    assert report['open_legacy_bridge_submissions'] == 2  # Missing protocol metadata is conservatively V1.
    assert report['open_temporal_submissions'] == 2
    assert report['legacy_unbound_nonterminal_tasks'] == 1
    assert report['pending_cancellation_deliveries'] == 1
    assert report['local_inventory_drained'] is False
    assert report['safe_to_remove_legacy_worker'] is False
    assert 'private-owner' not in str(report)
    assert 'opaque-bridge' not in str(report)
    assert database.read_bytes() == before


def test_empty_local_inventory_still_requires_external_drain_evidence(tmp_path):
    database = tmp_path/'tasks.db'
    make_database(database)
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE tasks SET state='completed'")
        conn.execute("UPDATE temporal_submissions SET state='completed'")
        conn.execute("UPDATE temporal_cancellations SET delivery='delivered'")

    report = inspect(database)
    assert report['local_inventory_drained'] is True
    assert report['safe_to_remove_legacy_worker'] is False
    assert report['external_checks_required']
