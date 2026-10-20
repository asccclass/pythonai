from contextlib import closing
from datetime import timedelta
import asyncio
import json
import time
from unittest.mock import AsyncMock, Mock, patch

import pytest

from agent_execution import execution_scope
from agent_execution.policy import current_context, execute_tool
from agent_execution.tasks import task_owner
from agent_execution.approvals import ApprovalStore, human_command
from agent_execution.temporal_backend.approval_gate import ApprovalGate
from agent_execution.temporal_backend.approval_outbox import pending, request_bound
from agent_execution.temporal_backend.config import Settings
from agent_execution.temporal_backend.context import execution_context
from agent_execution.temporal_backend.contracts import STEP_TOOLS, APPROVAL_WORKFLOW_NAME, APPROVAL_SIGNAL
from agent_execution.temporal_backend.ledger import OperationUncertain
from agent_execution.temporal_backend.operations import run_operation
from agent_execution.temporal_backend.step_store import StepStore
from agent_execution.temporal_backend.store import Store, canonical


@pytest.fixture
def task(tmp_path, monkeypatch):
    roles = tmp_path/'roles.json'
    roles.write_text(json.dumps({'websocket:alice': 'admin'}), encoding='utf-8')
    for name, value in {'AGENT_TASK_DB': str(tmp_path/'tasks.db'), 'AGENT_APPROVAL_DB': str(tmp_path/'approvals.db'),
        'AGENT_TENANT_ROOT': str(tmp_path/'tenants'), 'AGENT_WORKSPACE_ROOT': str(tmp_path),
        'AGENT_TEMPORAL_ENGINE': 'approval', 'AGENT_EXECUTION_BACKEND': 'temporal', 'ROLES_CONFIG': str(roles),
        'AGENT_TASK_WORKTREE': '0', 'AGENT_TRACE_ENABLED': '0'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv('AGENT_PROJECT_WORKSPACES', raising=False)
    (tmp_path/'note.txt').write_text('private evidence', encoding='utf-8')
    with execution_scope(source='websocket', identity='websocket:alice', session='device', project='default',
                         task_id='approval-task', workspace=tmp_path):
        store, approvals = Store(), ApprovalStore()
        context = current_context()
        owner = task_owner(context)
        store.bind(owner, context.task_id, 'temporal')
        scope = {k: getattr(context, k) for k in ('source', 'identity', 'session', 'project', 'conversation')}
        scope.update(workspace=str(tmp_path), allowed_tools=sorted(STEP_TOOLS), approval_db=str(approvals.path.resolve()))
        row = store.submit(owner, context.task_id, [{'role': 'user', 'content': 'private goal'}], scope, 'admin', Settings.load())
        steps = StepStore(store)
        row = steps.bootstrap_claim(row['id'], Settings.load())
        with execution_context(store, row) as ledger:
            cursor = steps.bootstrap(row, ledger)
            yield store, steps, row, ledger, cursor, approvals, roles


def tool_cursor(task, name='read_file', arguments=None):
    store, steps, row, ledger, cursor, approvals, roles = task
    arguments = {'path': 'note.txt'} if arguments is None else arguments
    fence, _ = steps.claim(cursor, ledger)
    saved = {'messages': [{'role': 'user', 'content': 'private goal'},
        {'role': 'assistant', 'tool_calls': [{'id': 'read-one', 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments)}}]}],
        'iteration': 1, 'iteration_limit': 4, 'provider_retry_limit': 2, 'eval_retries': 0, 'next_kind': 'tool'}
    with closing(store.tasks.connect()) as conn, conn:
        conn.execute("UPDATE tasks SET state='pending',phase='step_ready',checkpoint=? WHERE owner=? AND id=?",
                     (canonical(saved), row['owner'], row['task_id']))
    return steps.commit(cursor, fence, ledger, 'tool')


def wait(task, name='read_file', arguments=None):
    store, steps, row, ledger, cursor, approvals, roles = task
    cursor = tool_cursor(task, name, arguments)
    gate = ApprovalGate(store)
    result = gate.probe(cursor, ledger, steps)
    assert not result['ready'] and result['cursor'] == cursor
    with closing(store.tasks.connect()) as conn:
        intent = dict(conn.execute('SELECT * FROM temporal_approval_waits WHERE submission_id=?', (row['id'],)).fetchone())
    return gate, cursor, intent


def approve(task, intent):
    context = current_context()
    task[5].decide(intent['approval_id'], context.identity, context.session, True)


def execute(task, cursor, name='read_file', arguments=None, function=None):
    store, steps, row, ledger, _, approvals, roles = task
    arguments = {'path': 'note.txt'} if arguments is None else arguments
    steps.claim(cursor, ledger)
    with closing(store.tasks.connect()) as conn, conn:
        conn.execute("UPDATE tasks SET phase='tool_started' WHERE owner=? AND id=?", (row['owner'], row['task_id']))
    return run_operation('tool', 'model:1:tool:read-one', {'name': name, 'arguments': arguments},
                         lambda: execute_tool(name, arguments, function))


def test_v3_queue_and_database_authority_are_pinned(task, monkeypatch):
    store, steps, row, ledger, cursor, approvals, roles = task
    assert store.workflow_type(row) == APPROVAL_WORKFLOW_NAME
    assert row['queue'] == store.queue_for('approval') != store.queue_for('human')
    monkeypatch.setenv('AGENT_APPROVAL_DB', str(roles.parent/'other.db'))
    with pytest.raises(PermissionError, match='database configuration'):
        with execution_context(store, row):
            pytest.fail('Changed authority admitted')


def test_pending_wait_is_bounded_and_cannot_be_answered_as_text(task):
    store, steps, row, ledger, _, approvals, roles = task
    gate, cursor, intent = wait(task)
    assert store.tasks.get(row['owner'], row['task_id'])['phase'] == 'waiting_approval'
    assert intent['expires'] <= store.verify_budget_contract(row)['deadline']
    from agent_execution.temporal_backend.backend import invoke
    from exceptions import TaskSuspendedException
    saved = store.tasks.get(row['owner'], row['task_id'])['checkpoint']
    with pytest.raises(TaskSuspendedException):
        invoke(store, saved['messages']+[{'role':'tool','tool_call_id':'read-one','content':'approved'}], resume=True)
    assert approvals.decision_state(intent['approval_id'], 'websocket:alice', 'device') == 'pending'


def test_human_decision_and_outbox_commit_or_rollback_together(task):
    gate, cursor, intent = wait(task)
    approvals = task[5]
    with patch('agent_execution.temporal_backend.approval_outbox.record_decision', side_effect=RuntimeError('disk fault')):
        with pytest.raises(RuntimeError):
            approve(task, intent)
    assert approvals.decision_state(intent['approval_id'], 'websocket:alice', 'device') == 'pending'
    assert pending(approvals) == []
    assert 'original Temporal task' in human_command('/approve '+intent['approval_id'], 'websocket:alice', 'device')
    approve(task, intent)
    assert len(pending(approvals)) == 1
    with pytest.raises(PermissionError):
        approvals.decide(intent['approval_id'], 'websocket:alice', 'device', False)


def test_early_decision_survives_task_projection_failure_without_new_grant(task):
    store, steps, row, ledger, _, approvals, roles = task
    cursor = tool_cursor(task)
    gate = ApprovalGate(store)
    original = approvals.request
    keys = []
    def early(*args, **kwargs):
        key = original(*args, **kwargs)
        keys.append(key)
        approvals.decide(key, 'websocket:alice', 'device', True)
        if len(keys) == 1:
            raise RuntimeError('worker lost before task wait projection')
        return key
    with patch.object(ApprovalStore, 'request', side_effect=early):
        with pytest.raises(RuntimeError):
            gate.probe(cursor, ledger, steps)
        assert gate.probe(cursor, ledger, steps)['ready']
    assert keys[0] == keys[1] and len(pending(approvals)) == 1
    with closing(approvals.connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM approvals').fetchone()[0] == 1


def test_exact_approval_consumes_once_and_completed_io_is_cached(task):
    gate, cursor, intent = wait(task)
    approve(task, intent)
    assert gate.probe(cursor, task[3], task[1])['ready']
    effect = Mock(return_value='private evidence')
    assert execute(task, cursor, function=effect) == 'private evidence'
    assert task[5].decision_state(intent['approval_id'], 'websocket:alice', 'device') == 'consumed'
    effect.assert_called_once()
    assert run_operation('tool', 'model:1:tool:read-one', {'name':'read_file','arguments':{'path':'note.txt'}},
                         Mock(side_effect=AssertionError('Repeated I/O'))) == 'private evidence'


@pytest.mark.parametrize('state', ['denied', 'expired', 'cancelled'])
def test_terminal_decision_prevents_tool_and_preserves_root_contract(task, state):
    gate, cursor, intent = wait(task)
    store, steps, row, ledger, _, approvals, roles = task
    contract = store.verify_budget_contract(row)
    if state == 'denied':
        approvals.decide(intent['approval_id'], 'websocket:alice', 'device', False)
    elif state == 'cancelled':
        approvals.cancel_wait(intent['approval_id'], 'websocket:alice', 'device')
    else:
        with closing(approvals.connect()) as conn, conn:
            conn.execute('UPDATE approvals SET expires=? WHERE id=?', (time.time()-1, intent['approval_id']))
    result = gate.probe(cursor, ledger, steps)
    assert result['ready'] and result['cursor']['next_kind'] == 'finalize'
    assert store.tasks.get(row['owner'], row['task_id'])['state'] == 'failed'
    assert store.verify_budget_contract(row) == contract
    with closing(store.tasks.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM operation_ledger WHERE kind='tool'").fetchone()[0] == 0


@pytest.mark.parametrize('field,value', [('identity','other'), ('session','other'), ('action','other'), ('arguments',{'tool':'read_file','arguments':{'path':'other.txt'}})])
def test_grant_cannot_be_consumed_by_another_scope_or_operation(task, field, value):
    gate, cursor, intent = wait(task)
    approve(task, intent)
    values = dict(approval_id=intent['approval_id'], identity='websocket:alice', session='device',
                  action=intent['action'], arguments=json.loads(intent['payload']), binding=json.loads(intent['binding']))
    values[field] = value
    with pytest.raises(PermissionError):
        task[5].consume(**values)
    assert task[5].decision_state(intent['approval_id'], 'websocket:alice', 'device') == 'approved'


def test_unbound_legacy_consume_cannot_steal_durable_grant(task):
    gate, cursor, intent = wait(task)
    approve(task, intent)
    with pytest.raises(PermissionError, match='binding'):
        task[5].consume(intent['approval_id'], 'websocket:alice', 'device', intent['action'], json.loads(intent['payload']))


@pytest.mark.parametrize('change', ['arguments', 'role'])
def test_changed_arguments_or_revoked_role_never_reach_function(task, change):
    name = 'list_files' if change == 'role' else 'read_file'
    arguments = {'path': '.'} if change == 'role' else {'path': 'note.txt'}
    gate, cursor, intent = wait(task, name, arguments)
    approve(task, intent)
    assert gate.probe(cursor, task[3], task[1])['ready']
    effect = Mock()
    if change == 'arguments':
        arguments = {'path': 'other.txt'}
        with pytest.raises(PermissionError):
            execute(task, cursor, name, arguments, effect)
    else:
        # Revoke after the initial operation admission but before consumption.
        with patch('permissions.get_user_role', side_effect=['admin', 'viewer']):
            with pytest.raises(PermissionError):
                execute(task, cursor, name, arguments, effect)
    effect.assert_not_called()
    assert task[5].decision_state(intent['approval_id'], 'websocket:alice', 'device') == 'approved'


def test_crash_after_consumption_is_uncertain_and_never_mints_replacement(task):
    gate, cursor, intent = wait(task)
    approve(task, intent)
    assert gate.probe(cursor, task[3], task[1])['ready']
    effect = Mock()
    consume = ApprovalStore.consume
    class WorkerLost(BaseException):
        pass
    def lost(self, *args, **kwargs):
        consume(self, *args, **kwargs)
        raise WorkerLost()
    with patch.object(ApprovalStore, 'consume', lost):
        with pytest.raises(WorkerLost):
            execute(task, cursor, function=effect)
    effect.assert_not_called()
    assert task[5].decision_state(intent['approval_id'], 'websocket:alice', 'device') == 'consumed'
    with pytest.raises(OperationUncertain):
        task[1].recover(cursor, task[3])
    with pytest.raises(OperationUncertain):
        gate.probe(cursor, task[3], task[1])
    with closing(task[5].connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM approvals').fetchone()[0] == 1


def test_cancelled_or_deleted_task_fences_approved_grant(task):
    gate, cursor, intent = wait(task)
    approve(task, intent)
    store, steps, row, ledger, _, approvals, roles = task
    store.cancel(row['id'], row['owner'])
    with pytest.raises((PermissionError, InterruptedError)):
        gate.probe(cursor, ledger, steps)
    assert approvals.decision_state(intent['approval_id'], 'websocket:alice', 'device') == 'cancelled'
    with pytest.raises(PermissionError):
        approve(task, intent)


def test_notification_rpc_unknown_is_retried_without_content_in_payload(task):
    gate, cursor, intent = wait(task)
    approve(task, intent)
    event = pending(task[5])[0]
    client = Mock()
    client.get_workflow_handle.return_value.signal = AsyncMock(side_effect=RuntimeError('RPC unknown'))
    with pytest.raises(RuntimeError):
        asyncio.run(gate.dispatch(client, event))
    assert len(pending(task[5])) == 1
    client.get_workflow_handle.return_value.signal.side_effect = None
    asyncio.run(gate.dispatch(client, event))
    asyncio.run(gate.dispatch(client, event))
    assert pending(task[5]) == []
    client.get_workflow_handle.return_value.signal.assert_called_with(APPROVAL_SIGNAL,
        {'schema_version':1, 'submission_id':task[2]['id'], 'event_id':event['id']}, rpc_timeout=timedelta(seconds=10))
    assert 'private' not in canonical(client.get_workflow_handle.return_value.signal.call_args.args)


def test_deleted_session_and_wrong_binding_cannot_dispatch_or_resume(task):
    gate, cursor, intent = wait(task)
    approve(task, intent)
    with closing(task[0].tasks.connect()) as conn, conn:
        conn.execute("UPDATE wl_sessions SET state='deleting'")
    with pytest.raises(PermissionError):
        gate.probe(cursor, task[3], task[1])


def test_approval_storage_capacity_rejects_before_request(task, monkeypatch):
    cursor = tool_cursor(task)
    gate = ApprovalGate(task[0])
    monkeypatch.setenv('AGENT_TEMPORAL_LEDGER_STORAGE_BYTES', '1')
    from agent_execution.delivery import CapacityError
    with pytest.raises(CapacityError):
        gate.probe(cursor, task[3], task[1])
    with closing(task[5].connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM approvals').fetchone()[0] == 0

def test_outbox_capacity_cannot_commit_a_decision_without_notification(task, monkeypatch):
    gate, cursor, intent = wait(task)
    monkeypatch.setenv('AGENT_TEMPORAL_LEDGER_STORAGE_BYTES', '1')
    from agent_execution.delivery import CapacityError
    with pytest.raises(CapacityError):
        approve(task, intent)
    assert task[5].decision_state(intent['approval_id'], 'websocket:alice', 'device') == 'pending'
    assert pending(task[5]) == []


def test_other_host_events_do_not_starve_this_queue(task):
    gate, cursor, intent = wait(task)
    approve(task, intent)
    assert pending(task[5], queues={'different-host'}) == []
    assert len(pending(task[5], queues={task[2]['queue']})) == 1


@pytest.mark.parametrize('mutation', ['owner', 'queue', 'operation', 'event'])
def test_approval_protocol_rejects_foreign_targets_and_malformed_events(task, mutation):
    from agent_execution.temporal_backend.contracts import validate_approval_event
    gate, cursor, intent = wait(task)
    approve(task, intent)
    event = pending(task[5])[0]
    if mutation == 'event':
        with pytest.raises(ValueError):
            validate_approval_event({'schema_version':1,'submission_id':task[2]['id'],'event_id':'not-an-id','approved':True})
        return
    target = json.loads(event['binding'])
    target[{'owner':'owner','queue':'queue','operation':'operation_id'}[mutation]] = 'other'
    event['binding'] = canonical(target)
    client = Mock()
    client.get_workflow_handle.return_value.signal = AsyncMock()
    with pytest.raises((PermissionError, KeyError)):
        asyncio.run(gate.dispatch(client, event))
    client.get_workflow_handle.assert_not_called()


@pytest.mark.parametrize('value', [float('nan'), -1, True, 'tomorrow'])
def test_malformed_approval_wait_deadline_fails_closed(task, value):
    from agent_execution.temporal_backend.contracts import validate_approval_probe
    cursor = tool_cursor(task)
    with pytest.raises(ValueError):
        validate_approval_probe({'cursor':cursor,'ready':False,'expires':value})

def test_v3_does_not_open_write_tools_before_tp17(task):
    cursor = tool_cursor(task, 'write_file', {'path':'new.txt','content':'unapproved write'})
    with pytest.raises(PermissionError):
        ApprovalGate(task[0]).probe(cursor, task[3], task[1])
    with closing(task[5].connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM approvals').fetchone()[0] == 0
    assert not (task[6].parent/'new.txt').exists()

def test_missing_persisted_approval_binding_does_not_mint_a_new_request(task):
    gate, cursor, intent = wait(task)
    with closing(task[5].connect()) as conn, conn:
        conn.execute('DELETE FROM temporal_approval_bindings WHERE approval_id=?', (intent['approval_id'],))
    with pytest.raises(PermissionError, match='do not reissue'):
        gate.probe(cursor, task[3], task[1])
    with closing(task[5].connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM approvals').fetchone()[0] == 1

def test_pending_approval_uses_existing_event_sink_and_owner_scope(task):
    from agent_execution.temporal_backend.approval_gate import notify_wait
    gate, cursor, intent = wait(task)
    callback = Mock()
    notify_wait(callback)
    callback.assert_called_once_with('approval.required', {'approval_id':intent['approval_id'],
                                     'name':'read_file','arguments':{'path':'note.txt'}})
    callback.reset_mock()
    with execution_scope(session='other-device'):
        notify_wait(callback)
    callback.assert_not_called()


def test_resume_runtime_emits_approval_card_without_approving_text(task):
    from agent_execution.tasks import dispatch_task_input
    from agent_runtime import AgentRuntime
    gate, cursor, intent = wait(task)
    callback = Mock()
    runtime = AgentRuntime(messages=[], guard=None, memory=None, memory_searcher=None,
        skill_matcher=None, memory_worker=None, memory_classifier_factory=None, semantic_extractor=None,
        procedure_matcher=None, run_agent=Mock(side_effect=AssertionError('Legacy executor')), event_callback=callback)
    from agent_execution.temporal_backend.router import activity_scope
    with activity_scope(None, None):
        result = dispatch_task_input('/resume '+task[2]['task_id'], runtime)
    assert result.suspended and intent['approval_id'] in result.reply
    callback.assert_called_once_with('approval.required', {'approval_id':intent['approval_id'],
                                     'name':'read_file','arguments':{'path':'note.txt'}})
    assert task[5].decision_state(intent['approval_id'], 'websocket:alice', 'device') == 'pending'

def test_legacy_human_wait_notification_does_not_migrate_temporal(tmp_path, monkeypatch):
    from agent_execution.tasks import TaskStore
    from agent_execution.temporal_backend.approval_gate import notify_wait
    monkeypatch.setenv('AGENT_TASK_DB', str(tmp_path/'legacy.db'))
    monkeypatch.setenv('AGENT_EXECUTION_BACKEND', 'sqlite')
    callback = Mock()
    with execution_scope(source='local', identity='local', session='legacy', task_id='legacy', workspace=tmp_path):
        tasks = TaskStore()
        owner = task_owner(current_context())
        token = tasks.claim(owner, 'legacy', 'goal', [{'role':'user','content':'goal'}])
        tasks.checkpoint(owner, 'legacy', token, 'waiting_user', {'messages':[]})
        tasks.finish(owner, 'legacy', token, 'suspended', 'question')
        notify_wait(callback)
        with closing(tasks.connect()) as conn:
            assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_schema'").fetchone()
    callback.assert_not_called()
