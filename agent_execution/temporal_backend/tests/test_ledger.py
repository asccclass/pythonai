from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from contextvars import copy_context
import json
import sqlite3
import subprocess
import sys
import threading
from unittest.mock import Mock, patch

import pytest

from agent_execution import execution_scope
from agent_execution.delivery import CapacityError
from agent_execution.policy import current_context
from agent_execution.tasks import task_owner
from agent_execution.temporal_backend.config import Settings
from agent_execution.temporal_backend.ledger import Ledger, OperationUncertain
from agent_execution.temporal_backend.store import Store


def new_ledger():
    store = Store()
    context = current_context()
    owner = task_owner(context)
    store.bind(owner, context.task_id, 'temporal')
    row = store.submit(owner, context.task_id, [{'role': 'user', 'content': 'private input'}],
        {'workspace': str(context.workspace)}, 'admin', Settings.load())
    claimed = store.claim(row['id'], Settings.load())
    return Ledger(store, row['id'], claimed['fence'])


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_TASK_DB', str(tmp_path / 'tasks.db'))
    monkeypatch.setenv('AGENT_TENANT_ROOT', str(tmp_path / 'tenants'))
    monkeypatch.setenv('AGENT_WORKSPACE_ROOT', str(tmp_path))
    monkeypatch.setenv('AGENT_TRACE_ENABLED', '0')
    monkeypatch.delenv('AGENT_PROJECT_WORKSPACES', raising=False)
    with execution_scope(source='local', identity='local', session='ledger', project='default',
                         task_id='ledger-task', workspace=tmp_path, check_cancelled=None):
        yield new_ledger()


def rows(ledger, sql):
    with closing(ledger.store.tasks.connect()) as conn:
        return [dict(row) for row in conn.execute(sql)]


def completed(ledger):
    return ledger.run('step', 'tool', {'path': 'file'}, lambda: {'output': 'private output'})


def test_completed_reuses_verified_result_after_store_restart(ledger):
    execute = Mock(return_value={'output': 'private output'})
    assert ledger.run('step', 'tool', {'a': 1, 'b': 2}, execute) == execute.return_value
    old = ledger.get('step')
    restored = Ledger(Store(ledger.store.path), ledger.key, ledger.fence)
    assert restored.run('step', 'tool', {'b': 2, 'a': 1}, execute) == execute.return_value
    assert execute.call_count == 1
    assert restored.get('step')['result_ref'] == old['result_ref']
    assert len(rows(ledger, 'SELECT * FROM temporal_artifacts')) == 2
    assert 'private output' not in json.dumps(old)


def test_terminal_task_allows_cached_result_but_cannot_start_another_operation(ledger):
    completed(ledger)
    with closing(ledger.store.tasks.connect()) as conn, conn:
        conn.execute("UPDATE tasks SET state='completed' WHERE owner=? AND id=?", (ledger.owner, ledger.task))
    ledger.store.finish(ledger.key, ledger.fence, 'completed', {'reply': 'done'})
    restored = Ledger(Store(ledger.store.path), ledger.key, ledger.fence)
    execute = Mock()
    assert restored.run('step', 'tool', {'path': 'file'}, execute) == {'output': 'private output'}
    with pytest.raises(PermissionError):
        restored.run('new-step', 'tool', {}, execute)
    execute.assert_not_called()


@pytest.mark.parametrize('kind,args', [('tool', {'path': 'other'}), ('model', {'path': 'file'})])
def test_step_identity_rejects_changed_arguments_or_kind(ledger, kind, args):
    completed(ledger)
    execute = Mock()
    with pytest.raises(ValueError, match='different arguments'):
        ledger.run('step', kind, args, execute)
    execute.assert_not_called()


def test_prepared_operation_can_be_claimed_after_restart(ledger):
    ledger.prepare('step', 'tool', {})
    restored = Ledger(Store(ledger.store.path), ledger.key, ledger.fence)
    assert restored.run('step', 'tool', {}, lambda: 'done') == 'done'


def test_pre_execution_rejection_cannot_be_retried_or_hide_started_work(ledger):
    ledger.prepare('rejected', 'tool', {})
    ledger.reject('rejected')
    assert ledger.get('rejected')['state'] == 'rejected_no_effect'
    with pytest.raises(OperationUncertain):
        ledger.run('rejected', 'tool', {}, Mock())
    ledger.prepare('running', 'tool', {})
    ledger.claim('running')
    with pytest.raises(PermissionError):
        ledger.reject('running')
    assert ledger.get('running')['state'] == 'running'


@pytest.mark.parametrize('uncertain', [False, True])
def test_started_or_unknown_operation_is_never_reexecuted(ledger, uncertain):
    ledger.prepare('step', 'tool', {})
    row = ledger.claim('step')
    if uncertain:
        ledger.uncertain('step', row['fence'])
    execute = Mock()
    restored = Ledger(Store(ledger.store.path), ledger.key, ledger.fence)
    with pytest.raises(OperationUncertain):
        restored.run('step', 'tool', {}, execute)
    execute.assert_not_called()


def test_lost_result_marks_uncertain_without_retry(ledger):
    execute = Mock(side_effect=TimeoutError('effect may have happened'))
    with pytest.raises(TimeoutError):
        ledger.run('step', 'tool', {}, execute)
    assert ledger.get('step')['state'] == 'uncertain'
    with pytest.raises(OperationUncertain):
        ledger.run('step', 'tool', {}, execute)
    assert execute.call_count == 1


def test_unserializable_result_cannot_be_marked_completed(ledger):
    with pytest.raises(ValueError):
        ledger.run('step', 'tool', {}, lambda: {'value': float('nan')})
    assert ledger.get('step')['state'] == 'uncertain'
    assert rows(ledger, 'SELECT * FROM temporal_artifacts') == []


def test_concurrent_claim_only_executes_one_operation(ledger):
    started, release = threading.Event(), threading.Event()
    execute = Mock(side_effect=lambda: (started.set(), release.wait(5), 'done')[-1])

    def run():
        try:
            return ledger.run('step', 'tool', {}, execute)
        except OperationUncertain:
            return 'busy'

    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(copy_context().run, run)
        try:
            assert started.wait(5)
            others = [pool.submit(copy_context().run, run) for _ in range(3)]
            assert [future.result(timeout=5) for future in others] == ['busy'] * 3
        finally:
            release.set()
        assert first.result(timeout=5) == 'done'
    assert execute.call_count == 1


def test_result_and_evidence_roll_back_with_operation_completion(ledger):
    with closing(ledger.store.tasks.connect()) as conn, conn:
        conn.execute("CREATE TRIGGER injected_failure BEFORE UPDATE OF state ON operation_ledger WHEN NEW.state='completed' BEGIN SELECT RAISE(ABORT,'injected completion failure'); END")
    execute = Mock(return_value={'output': 'effect already occurred'})
    with pytest.raises(sqlite3.IntegrityError):
        ledger.run('step', 'tool', {}, execute)
    assert rows(ledger, 'SELECT * FROM temporal_artifacts') == []
    assert ledger.get('step')['state'] == 'uncertain'
    with pytest.raises(OperationUncertain):
        ledger.run('step', 'tool', {}, execute)
    assert execute.call_count == 1


def test_stale_operation_fence_cannot_save_result(ledger):
    ledger.prepare('step', 'tool', {})
    ledger.claim('step')
    with pytest.raises(PermissionError, match='stale'):
        ledger.complete('step', 'wrong-fence', {'output': 'late'}, {})
    assert rows(ledger, 'SELECT * FROM temporal_artifacts') == []


@pytest.mark.parametrize('reason', ['cancel', 'workflow_failed', 'task_cancel', 'new_fence'])
def test_late_result_cannot_publish_after_parent_is_fenced(ledger, reason):
    ledger.prepare('step', 'tool', {})
    row = ledger.claim('step')
    if reason == 'cancel':
        ledger.store.cancel(ledger.key, ledger.owner)
    elif reason == 'workflow_failed':
        ledger.store.workflow_failed(ledger.key, ledger.owner, 'TimeoutError')
    elif reason == 'task_cancel':
        ledger.store.tasks.cancel(ledger.owner, ledger.task)
    else:
        with closing(ledger.store.tasks.connect()) as conn, conn:
            conn.execute("UPDATE temporal_submissions SET fence='new-fence' WHERE id=?", (ledger.key,))
    with pytest.raises(PermissionError):
        ledger.complete('step', row['fence'], 'late result', {})
    assert rows(ledger, 'SELECT * FROM temporal_artifacts') == []
    if reason in {'cancel', 'workflow_failed'}:
        assert rows(ledger, 'SELECT state FROM operation_ledger')[0]['state'] == 'uncertain'


def test_cancellation_distinguishes_prepared_from_started(ledger):
    ledger.prepare('prepared', 'tool', {})
    ledger.prepare('running', 'tool', {})
    ledger.claim('running')
    ledger.store.cancel(ledger.key, ledger.owner)
    assert {row['step_id']: row['state'] for row in rows(ledger, 'SELECT * FROM operation_ledger')} == {
        'prepared': 'rejected_no_effect', 'running': 'uncertain'}


@pytest.mark.parametrize('mutation', ['data', 'size', 'hash', 'missing_result', 'missing_evidence'])
def test_corrupt_or_missing_artifact_never_reexecutes_completed_operation(ledger, mutation):
    completed(ledger)
    row = ledger.get('step')
    ref = json.loads(row['evidence_ref' if mutation == 'missing_evidence' else 'result_ref'])
    with closing(ledger.store.tasks.connect()) as conn, conn:
        if mutation.startswith('missing'):
            conn.execute('DELETE FROM temporal_artifacts WHERE id=?', (ref['id'],))
        else:
            column, value = {'data': ('data', b'corrupted'), 'size': ('byte_count', 999),
                             'hash': ('sha256', '0'*64)}[mutation]
            conn.execute(f'UPDATE temporal_artifacts SET {column}=? WHERE id=?', (value, ref['id']))
    execute = Mock()
    with pytest.raises((ValueError, KeyError)):
        ledger.run('step', 'tool', {'path': 'file'}, execute)
    execute.assert_not_called()


def test_artifact_reference_cannot_cross_owner_or_task(ledger):
    completed(ledger)
    reference = json.loads(ledger.get('step')['result_ref'])
    with execution_scope(identity='other'), pytest.raises(PermissionError):
        ledger.load(reference)
    with execution_scope(task_id='other-task'):
        other = new_ledger()
        with pytest.raises(KeyError):
            other.load(reference)


@pytest.mark.parametrize('field,value', [('schema_version', True), ('bytes', True),
                                        ('bytes', -1), ('id', '../escape')])
def test_malformed_reference_is_rejected(ledger, field, value):
    completed(ledger)
    ref = json.loads(ledger.get('step')['result_ref'])
    ref[field] = value
    with pytest.raises(ValueError):
        ledger.load(ref)


def test_deleted_owner_cannot_load_or_publish_results(ledger):
    from agent_execution.workspace_lifecycle import LifecycleStore
    lifecycle = LifecycleStore()
    lifecycle.ensure_session()
    completed(ledger)
    reference = json.loads(ledger.get('step')['result_ref'])
    ledger.prepare('running', 'tool', {})
    row = ledger.claim('running')
    with closing(ledger.store.tasks.connect()) as conn, conn:
        conn.execute("UPDATE wl_sessions SET state='deleting'")
    with pytest.raises(PermissionError):
        ledger.load(reference)
    with pytest.raises(PermissionError):
        ledger.complete('running', row['fence'], 'late', {})


@pytest.mark.parametrize('setting,value', [('AGENT_TEMPORAL_ARTIFACT_MAX_BYTES', '1'),
    ('AGENT_TEMPORAL_LEDGER_STORAGE_BYTES', '700')])
def test_capacity_failure_after_execution_remains_uncertain(ledger, monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    with pytest.raises(CapacityError):
        ledger.run('step', 'tool', {}, lambda: 'result')
    assert ledger.get('step')['state'] == 'uncertain'
    assert rows(ledger, 'SELECT * FROM temporal_artifacts') == []


def test_operation_count_limit_rejects_before_execution(ledger, monkeypatch):
    monkeypatch.setenv('AGENT_TEMPORAL_OPERATION_LIMIT', '1')
    ledger.prepare('first', 'tool', {})
    execute = Mock()
    with pytest.raises(CapacityError):
        ledger.run('second', 'tool', {}, execute)
    execute.assert_not_called()


def test_unknown_operation_schema_is_not_downgraded(ledger):
    with closing(ledger.store.tasks.connect()) as conn, conn:
        conn.execute('UPDATE temporal_operation_schema SET version=99')
    with pytest.raises(ValueError, match='schema'):
        Ledger(ledger.store, ledger.key, ledger.fence)
    assert rows(ledger, 'SELECT version FROM temporal_operation_schema') == [{'version': 99}]


def test_workspace_relocation_blocks_cached_result(ledger, monkeypatch, tmp_path):
    completed(ledger)
    monkeypatch.setenv('AGENT_PROJECT_WORKSPACES', json.dumps({'default': str(tmp_path / 'relocated')}))
    with pytest.raises(PermissionError, match='workspace'):
        ledger.run('step', 'tool', {'path': 'file'}, Mock())


def test_unregistered_workspace_override_is_rejected(ledger, tmp_path):
    with execution_scope(workspace=tmp_path / 'unregistered', repository_workspace=tmp_path):
        with pytest.raises(PermissionError, match='worktree'):
            ledger.run('step', 'tool', {}, Mock())


def test_existing_task_managed_worktree_can_use_operation_ledger(ledger, monkeypatch, tmp_path):
    from agent_execution.tasks import durable_invoke
    from agent_execution.temporal_backend.router import activity_scope
    for args in [['init'], ['config', 'user.name', 'Temporal Test'],
                 ['config', 'user.email', 'temporal@example.invalid']]:
        subprocess.run(['git', *args], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / 'note.txt').write_text('worktree evidence', encoding='utf-8')
    subprocess.run(['git', 'add', '--', 'note.txt'], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(['git', 'commit', '-m', 'test fixture'], cwd=tmp_path, check=True, capture_output=True)
    monkeypatch.setenv('AGENT_TASK_WORKTREE', '1')
    monkeypatch.setenv('AGENT_WORKTREE_ROOT', str(tmp_path.parent / (tmp_path.name + '-worktrees')))
    def execute(saved):
        assert current_context().workspace != tmp_path
        return ledger.run('read', 'tool', {'path': 'note.txt'},
                          lambda: (current_context().workspace / 'note.txt').read_text(encoding='utf-8'))
    with activity_scope(ledger.owner, ledger.task):
        assert durable_invoke(execute, [{'role': 'user', 'content': 'private input'}], None) == 'worktree evidence'
    assert ledger.load(json.loads(ledger.get('read')['result_ref'])) == 'worktree evidence'


def test_cached_tool_result_rechecks_allowlist(ledger):
    from openai.types.chat import ChatCompletionMessageToolCall
    from agent_execution.temporal_backend.operations import operation_scope
    from agent_execution.turn_control.steps import tool_step
    call = ChatCompletionMessageToolCall(id='read', type='function', function={
        'name': 'read_file', 'arguments': '{"path":"note.txt"}'})
    execute = Mock(return_value='private evidence')
    with operation_scope(ledger):
        assert str(tool_step(call, invoke=execute, operation_id='read')) == 'private evidence'
        assert str(tool_step(call, invoke=execute, operation_id='read')) == 'private evidence'
        with execution_scope(allowed_tools=frozenset()), pytest.raises(PermissionError):
            tool_step(call, invoke=execute, operation_id='read')
    assert execute.call_count == 1


def test_operation_scope_does_not_leak_after_failure(ledger):
    from agent_execution.temporal_backend.operations import operation_scope, run_operation
    with pytest.raises(ValueError):
        with operation_scope(ledger):
            run_operation('tool', None, {}, Mock())
    # Legacy calls need neither a logical operation ID nor Temporal serialization.
    execute = Mock(return_value=object())
    assert run_operation('tool', None, {}, execute) is execute.return_value
    execute.assert_called_once()


def test_cached_tool_rechecks_current_remote_role(ledger):
    from agent_execution.temporal_backend.operations import operation_scope
    from agent_execution.turn_control.steps import tool_step
    from openai.types.chat import ChatCompletionMessageToolCall
    call = ChatCompletionMessageToolCall(id='list', type='function', function={
        'name': 'list_files', 'arguments': '{}'})
    execute = Mock(return_value='listing')
    with execution_scope(identity='web:42', source='api'):
        remote = new_ledger()
        with operation_scope(remote), patch('permissions.get_user_role', return_value='admin'):
            assert tool_step(call, invoke=execute, operation_id='list') == 'listing'
        with operation_scope(remote), patch('permissions.get_user_role', return_value='viewer'):
            with pytest.raises(PermissionError):
                tool_step(call, invoke=execute, operation_id='list')
    assert execute.call_count == 1


def test_role_change_cannot_send_stale_model_capabilities(ledger):
    from agent_execution.temporal_backend.operations import operation_scope
    from agent_execution.turn_control.steps import model_step
    with execution_scope(identity='web:42', source='api'):
        remote = new_ledger()
        client = Mock()
        with operation_scope(remote), patch('permissions.get_user_role', return_value='viewer'):
            with pytest.raises(PermissionError, match='capabilities'):
                model_step(client, {'model': 'test', 'tools': [{'function': {'name': 'fetch_url'}}]},
                           [], iteration=1, operation_id='model')
        client.chat.completions.create.assert_not_called()


@pytest.mark.parametrize('stage', ['after_effect', 'after_commit'])
def test_process_crash_does_not_repeat_observed_effect(ledger, stage, tmp_path):
    script = '''
import os, sys
from pathlib import Path
from agent_execution import execution_scope
from agent_execution.temporal_backend.store import Store
from agent_execution.temporal_backend.ledger import Ledger
root=Path(os.environ['AGENT_WORKSPACE_ROOT'])
with execution_scope(source='local',identity='local',session='ledger',project='default',task_id='ledger-task',workspace=root):
    ledger=Ledger(Store(),sys.argv[1],sys.argv[2])
    def execute():
        with (root/'effect.txt').open('a',encoding='utf-8') as output:
            output.write('effect\\n')
        if sys.argv[3]=='after_effect':os._exit(23)
        return 'child result'
    ledger.run('crash','tool',{},execute)
    os._exit(24)
'''
    child = subprocess.run([sys.executable, '-c', script, ledger.key, ledger.fence, stage],
                           capture_output=True, text=True, timeout=30)
    assert child.returncode == (23 if stage == 'after_effect' else 24), child.stderr
    assert (tmp_path / 'effect.txt').read_text(encoding='utf-8') == 'effect\n'
    execute = Mock()
    if stage == 'after_effect':
        assert ledger.get('crash')['state'] == 'running'
        with pytest.raises(OperationUncertain):
            ledger.run('crash', 'tool', {}, execute)
    else:
        assert ledger.run('crash', 'tool', {}, execute) == 'child result'
    execute.assert_not_called()
    assert (tmp_path / 'effect.txt').read_text(encoding='utf-8') == 'effect\n'
