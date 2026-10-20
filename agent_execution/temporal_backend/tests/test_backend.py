from contextlib import closing
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock, patch

import pytest

from agent_execution import execution_scope
from agent_execution.tasks import TaskStore, task_owner
from agent_execution.policy import current_context
from agent_execution.temporal_backend.config import Settings, selected_backend
from agent_execution.temporal_backend.contracts import validate_envelope, READ_TOOLS
from agent_execution.temporal_backend.store import Store


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_TASK_DB', str(tmp_path / 'tasks.db'))
    monkeypatch.setenv('AGENT_TENANT_ROOT', str(tmp_path / 'tenants'))
    monkeypatch.setenv('AGENT_WORKSPACE_ROOT', str(tmp_path))
    monkeypatch.setenv('AGENT_EXECUTION_BACKEND', 'sqlite')
    monkeypatch.setenv('AGENT_TRACE_ENABLED', '0')
    monkeypatch.setenv('AGENT_TASK_WORKTREE', '0')
    monkeypatch.delenv('AGENT_PROJECT_WORKSPACES', raising=False)
    with execution_scope(source='local', identity='local', session='temporal-test', project='default',
                         task_id='test-task', workspace=tmp_path, repository_workspace=None):
        yield


def submission(store=None, messages=None):
    store = store or Store()
    context = current_context()
    owner = task_owner(context)
    store.bind(owner, context.task_id, 'temporal')
    row = store.submit(owner, context.task_id, messages or [{'role': 'user', 'content': 'private task'}],
                       {'source': context.source, 'identity': context.identity, 'session': context.session,
                        'conversation': '', 'project': 'default', 'workspace': str(context.workspace),
                        'allowed_tools': sorted(READ_TOOLS)}, 'admin', Settings.load())
    return store, row


def test_optional_backend_can_execute_legacy_with_sdk_import_blocked():
    code = '''
import builtins, os, tempfile
old = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.startswith('temporalio'): raise ImportError('SDK intentionally absent')
    return old(name, *args, **kwargs)
builtins.__import__ = guarded
from agent_execution.temporal_backend.router import route
from agent_execution import execution_scope
with execution_scope(task_id='sdk-absent'):
    assert route([{'role':'user','content':'task'}]) is None
print('optional import passed')
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('setting,value', [('AGENT_EXECUTION_BACKEND', 'other'),
    ('AGENT_TEMPORAL_ADDRESS', 'public.example:7233'), ('AGENT_TEMPORAL_ADDRESS', 'localhost:0'),
    ('AGENT_TEMPORAL_NAMESPACE', '../bad'), ('AGENT_TEMPORAL_ACTIVITY_CONCURRENCY', '0'),
    ('AGENT_TEMPORAL_ARTIFACT_MAX_BYTES', '0'), ('AGENT_TEMPORAL_LEDGER_STORAGE_BYTES', '-1'),
    ('AGENT_TEMPORAL_OPERATION_LIMIT', 'invalid'), ('AGENT_TEMPORAL_ENGINE', 'unknown')])
def test_invalid_configuration_fails_closed(setting, value, monkeypatch):
    monkeypatch.setenv(setting, value)
    with pytest.raises(ValueError):
        selected_backend() if setting == 'AGENT_EXECUTION_BACKEND' else Settings.load()


def test_envelope_cannot_carry_prompt_or_override_identity():
    assert validate_envelope({'schema_version': 1, 'submission_id': 'a'*32}) == 'a'*32
    for value in [{'schema_version': True, 'submission_id': 'a'*32},
                  {'schema_version': 1, 'submission_id': '../file'},
                  {'schema_version': 1, 'submission_id': 'a'*32, 'identity': 'local'}]:
        with pytest.raises(ValueError):
            validate_envelope(value)


def test_binding_survives_configuration_changes_and_never_migrates_existing_task():
    store = Store()
    owner = task_owner(current_context())
    assert store.bind(owner, 'one', 'sqlite') == 'sqlite'
    assert store.bind(owner, 'one', 'temporal') == 'sqlite'
    assert store.bind(owner, 'two', 'temporal') == 'temporal'
    assert store.bind(owner, 'two', 'sqlite') == 'temporal'
    token = store.tasks.claim(owner, 'legacy', 'goal', [])
    assert token
    assert store.bind(owner, 'legacy', 'temporal') == 'sqlite'


def test_submission_dedup_changed_input_and_owner_isolation():
    store, row = submission()
    _, duplicate = submission(store)
    assert duplicate['id'] == row['id']
    with pytest.raises(ValueError, match='different input'):
        submission(store, [{'role': 'user', 'content': 'different task'}])
    with pytest.raises(KeyError):
        store.get(row['id'], 'other owner')
    assert store.tasks.get(row['owner'], row['task_id'])['phase'] == 'temporal_pending'
    assert len(store.pending()) == 1


def test_sqlite_claim_cannot_steal_temporal_submission():
    store, row = submission()
    from agent_execution.temporal_backend.router import assert_claim_backend
    with closing(store.tasks.connect()) as conn, pytest.raises(PermissionError):
        assert_claim_backend(conn, row['owner'], row['task_id'])
    with pytest.raises(PermissionError):
        store.tasks.claim(row['owner'], row['task_id'], 'private task', [])


def test_activity_claim_once_cancel_and_late_publication_fenced():
    store, row = submission()
    started = store.claim(row['id'], Settings.load())
    with pytest.raises(RuntimeError):
        store.claim(row['id'], Settings.load())
    store.cancel(row['id'], row['owner'])
    with pytest.raises(PermissionError):
        store.finish(row['id'], started['fence'], 'completed', {'reply': 'late'})
    assert store.tasks.get(row['owner'], row['task_id'])['state'] == 'cancelled'


def test_worker_rejects_other_host_and_cancelled_task_before_model():
    store, row = submission()
    with pytest.raises(PermissionError):
        store.claim(row['id'], Settings('127.0.0.1:7999', 'pythonai-dev'))
    store.tasks.cancel(row['owner'], row['task_id'])
    with pytest.raises(PermissionError):
        store.claim(row['id'], Settings.load())


def test_legacy_route_runs_locally_and_temporal_route_never_falls_back(monkeypatch):
    from agent_execution.tasks import durable_invoke
    function = Mock(return_value='legacy')
    assert durable_invoke(function, [{'role': 'user', 'content': 'task'}], None) == 'legacy'
    function.assert_called_once()
    monkeypatch.setenv('AGENT_EXECUTION_BACKEND', 'temporal')
    # An existing task remains legacy after the default changes.
    assert durable_invoke(function, [{'role': 'user', 'content': 'task'}], None) == 'legacy'
    function.reset_mock()
    with execution_scope(task_id='fresh'):
        with patch('agent_execution.temporal_backend.backend.invoke', side_effect=ConnectionError):
            with pytest.raises(ConnectionError):
                durable_invoke(function, [{'role': 'user', 'content': 'new task'}], None)
    function.assert_not_called()


def test_readonly_worker_rebuilds_policy_budget_and_profile():
    pytest.importorskip('temporalio')
    from agent_execution.temporal_backend.activities import run_submission
    from agent_execution.budgets import active_budget
    from permissions import current_role
    store, row = submission()
    def runner(messages, saved):
        assert current_role() == 'admin'
        assert current_context().allowed_tools == READ_TOOLS
        assert 'run_command' not in current_context().allowed_tools
        assert active_budget().key == row['task_id']
        assert any('[PythonAI current user profile]' in str(m.get('content')) for m in messages)
        assert saved['messages'] is messages
        return 'read-only result'
    result = run_submission(row['id'], Settings.load(), runner=runner)
    assert result == {'submission_id': row['id']}
    assert store.get(row['id'])['state'] == 'completed'
    assert store.tasks.get(row['owner'], row['task_id'])['state'] == 'completed'
    assert current_context().allowed_tools is None
    with pytest.raises(RuntimeError):
        run_submission(row['id'], Settings.load(), runner=runner)


def test_worker_checks_new_remote_role_and_workspace_mapping(monkeypatch, tmp_path):
    pytest.importorskip('temporalio')
    from agent_execution.temporal_backend.activities import run_submission
    from permissions import current_role
    with execution_scope(identity='telegram:42', source='communication', task_id='remote'):
        store, row = submission()
    with patch('permissions.get_user_role', return_value='viewer'):
        def runner(messages, saved):
            assert current_role() == 'viewer'
            return 'viewer'
        run_submission(row['id'], Settings.load(), runner=runner)
    with execution_scope(task_id='relocated'):
        store, row = submission()
    monkeypatch.setenv('AGENT_PROJECT_WORKSPACES', json.dumps({'default': str(tmp_path / 'other')}))
    runner = Mock()
    with pytest.raises(PermissionError, match='Workspace configuration changed'):
        run_submission(row['id'], Settings.load(), runner=runner)
    runner.assert_not_called()


def test_no_id_skill_and_resume_are_not_silently_executed(monkeypatch):
    from agent_execution.temporal_backend.router import route
    monkeypatch.setenv('AGENT_EXECUTION_BACKEND', 'temporal')
    with execution_scope(task_id=None), pytest.raises(ValueError, match='durable task ID'):
        route([])
    with pytest.raises(ValueError, match='new read-only'):
        route([], skill=True)


def test_unknown_schema_is_not_downgraded():
    store = Store()
    with closing(store.tasks.connect()) as conn, conn:
        conn.execute('DELETE FROM temporal_schema')
        conn.execute('INSERT INTO temporal_schema VALUES(2)')
    with pytest.raises(ValueError, match='Unsupported'):
        Store()
    with closing(store.tasks.connect()) as conn:
        assert [r[0] for r in conn.execute('SELECT version FROM temporal_schema')] == [2]


@pytest.mark.parametrize('engine,mode', [('bridge','read_only_bridge'), ('steps','read_only_steps'),
                                        ('human','read_only_human_steps'), ('approval','read_only_approval_steps')])
def test_preflight_does_not_create_a_database(tmp_path, monkeypatch, engine, mode):
    from agent_execution.temporal_backend.preflight import inspect
    path = tmp_path / 'not-created.db'
    monkeypatch.setenv('AGENT_TASK_DB', str(path))
    monkeypatch.setenv('AGENT_TEMPORAL_ENGINE', engine)
    result = inspect()
    assert result['connected'] is False
    assert result['mode'] == mode and result['new_submission_engine'] == engine
    assert result['backup_inventory']['tasks']['exists'] is False
    assert not path.exists()


def test_terminal_workflow_failure_fences_ongoing_activity():
    store, row = submission()
    started = store.claim(row['id'], Settings.load())
    store.workflow_failed(row['id'], row['owner'], 'TIMED_OUT')
    assert store.get(row['id'])['state'] == 'uncertain'
    assert store.tasks.get(row['owner'], row['task_id'])['state'] == 'uncertain'
    with pytest.raises(PermissionError):
        store.finish(row['id'], started['fence'], 'completed', {'reply': 'late'})


def test_client_loop_timeout_and_shutdown_release_coroutines():
    import asyncio
    import threading
    from agent_execution.temporal_backend.client import LoopBridge
    loop = LoopBridge()
    stopped = threading.Event()
    async def pending():
        try:
            await asyncio.sleep(30)
        finally:
            stopped.set()
    try:
        with pytest.raises(TimeoutError):
            loop.call(pending(), .05)
    finally:
        loop.close()
    assert stopped.wait(1)
    assert not loop.thread.is_alive()


def test_cancel_through_existing_task_api_fences_activity_result():
    store, row = submission()
    started = store.claim(row['id'], Settings.load())
    store.tasks.cancel(row['owner'], row['task_id'])
    with pytest.raises(PermissionError, match='task cancellation'):
        store.finish(row['id'], started['fence'], 'completed', {'reply': 'late'})


def test_finished_task_result_is_retrievable_without_service_or_sdk():
    from agent_execution.temporal_backend.router import route
    store, row = submission()
    started = store.claim(row['id'], Settings.load())
    result_messages = [{'role': 'user', 'content': 'private task'}, {'role': 'assistant', 'content': 'cached'}]
    store.finish(row['id'], started['fence'], 'completed', {'reply': 'cached', 'messages': result_messages})
    messages = []
    with patch('agent_execution.temporal_backend.client.connect', side_effect=AssertionError):
        assert route(messages, resume=True) == 'cached'
    assert messages == result_messages


def test_legacy_answer_and_resolution_cannot_mutate_temporal_task():
    store, row = submission()
    with pytest.raises(PermissionError, match='legacy executor'):
        store.tasks.answer(row['owner'], row['task_id'], 'answer')
    with pytest.raises(PermissionError, match='legacy executor'):
        store.tasks.resolve(row['owner'], row['task_id'], 'call', 'resolution')


def test_activity_rejects_foreign_workflow_before_claim():
    pytest.importorskip('temporalio')
    from types import SimpleNamespace
    from temporalio.exceptions import ApplicationError
    from agent_execution.temporal_backend.activities import execute_readonly
    store, row = submission()
    with patch('agent_execution.temporal_backend.activities.activity.info', return_value=SimpleNamespace(workflow_id='foreign')):
        with pytest.raises(ApplicationError):
            execute_readonly({'schema_version': 1, 'submission_id': row['id']})
    assert store.get(row['id'])['state'] == 'pending'


def test_disabled_backend_does_not_migrate_optional_schema():
    from agent_execution.temporal_backend.router import route
    assert route([{'role': 'user', 'content': 'legacy'}]) is None
    with closing(TaskStore().connect()) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_schema'").fetchone() is None
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='execution_bindings'").fetchone() is None


def test_submission_failure_rolls_back_task_and_outbox_but_keeps_reservation():
    import sqlite3
    store = Store()
    owner = task_owner(current_context())
    store.bind(owner, current_context().task_id, 'temporal')
    with closing(store.tasks.connect()) as conn, conn:
        conn.execute("CREATE TRIGGER inject_outbox_failure BEFORE INSERT ON temporal_submissions BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        submission(store)
    assert store.pending() == []
    with pytest.raises(KeyError):
        store.tasks.get(owner, current_context().task_id)
    assert store.bind(owner, current_context().task_id, 'sqlite') == 'temporal'
    with pytest.raises(PermissionError):
        store.tasks.claim(owner, current_context().task_id, 'private task', [])
    with closing(store.tasks.connect()) as conn, conn:
        conn.execute('DROP TRIGGER inject_outbox_failure')
    _, row = submission(store)
    assert row['state'] == 'pending'
