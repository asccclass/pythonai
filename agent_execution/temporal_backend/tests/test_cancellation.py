"""Cancellation fences commit with a retryable delivery intent, even offline."""
import asyncio
from contextlib import closing
import sqlite3
from unittest.mock import AsyncMock, Mock, patch
from agent_execution.delivery import CapacityError

import pytest

from agent_execution.tasks import TaskStore
from agent_execution.temporal_backend.cancellation import pending, dispatch
from agent_execution.temporal_backend.store import Store
from agent_execution.temporal_backend.tests.test_step_store import task


@pytest.mark.parametrize('entry', ['submission', 'task', 'session'])
def test_all_cancel_entries_commit_fence_and_outbox(task, entry):
    store, steps, row, ledger, cursor = task
    operation = ledger.prepare('read-one', 'tool', {'path': 'x'})
    fence = ledger.claim(operation['step_id'])
    if entry == 'submission':
        store.cancel(row['id'], row['owner'])
    elif entry == 'task':
        assert store.tasks.cancel(row['owner'], row['task_id'])
    else:
        store.tasks.cancel_session(row['owner'])
    assert store.get(row['id'])['state'] == 'cancelled'
    assert store.tasks.get(row['owner'], row['task_id'])['state'] == 'cancelled'
    with closing(store.tasks.connect()) as conn:
        assert conn.execute('SELECT state FROM operation_ledger WHERE step_id=?', (operation['step_id'],)).fetchone()[0] == 'uncertain'
    # A second cancellation must not create a new delivery or undo evidence.
    store.cancel(row['id'], row['owner'])
    assert len(pending(store, store.settings(row), {row['queue']})) == 1
    assert not pending(store, store.settings(row), {'another-host'})
    from agent_execution.temporal_backend.config import Settings
    assert not pending(store, Settings(row['address'], 'other-namespace'), {row['queue']})
    with closing(store.tasks.connect()) as conn:
        assert conn.execute('SELECT count(*) FROM temporal_cancellations').fetchone()[0] == 1
    with pytest.raises((PermissionError, InterruptedError)):
        ledger.complete(operation['step_id'], fence, {}, {})


def test_cancel_transaction_rolls_back_with_outbox_failure(task):
    store, steps, row, ledger, cursor = task
    with closing(store.tasks.connect()) as conn, conn:
        conn.execute("""CREATE TRIGGER reject_cancel BEFORE INSERT ON temporal_cancellations
            BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        store.tasks.cancel(row['owner'], row['task_id'])
    assert store.get(row['id'])['state'] == 'running'
    assert store.tasks.get(row['owner'], row['task_id'])['state'] != 'cancelled'


def test_cancel_rpc_unknown_is_retried_after_store_restart(task):
    pytest.importorskip('temporalio')
    store, steps, row, ledger, cursor = task
    store.cancel(row['id'], row['owner'])
    handle = Mock(cancel=AsyncMock(side_effect=TimeoutError('RPC outcome unknown')))
    client = Mock(get_workflow_handle=Mock(return_value=handle))
    with pytest.raises(TimeoutError):
        asyncio.run(dispatch(store, row, client))
    restarted = Store(store.path)
    event, = pending(restarted, restarted.settings(row), {row['queue']})
    handle.cancel.side_effect = None
    asyncio.run(dispatch(restarted, event, client))
    assert not pending(restarted, restarted.settings(row), {row['queue']})
    assert handle.cancel.await_count == 2
    assert handle.cancel.call_args.kwargs['rpc_timeout'].total_seconds() == 10
    client.get_workflow_handle.assert_called_with(row['workflow_id'])


@pytest.mark.parametrize('status', ['NOT_FOUND', 'UNAVAILABLE'])
def test_cancel_rpc_only_acknowledges_definite_absence(task, status):
    pytest.importorskip('temporalio')
    from temporalio.service import RPCError, RPCStatusCode
    store, steps, row, ledger, cursor = task
    store.cancel(row['id'], row['owner'])
    handle = Mock(cancel=AsyncMock(side_effect=RPCError('fixture', getattr(RPCStatusCode, status), b'')))
    client = Mock(get_workflow_handle=Mock(return_value=handle))
    if status == 'NOT_FOUND':
        asyncio.run(dispatch(store, row, client))
    else:
        with pytest.raises(RPCError):
            asyncio.run(dispatch(store, row, client))
    assert bool(pending(store, store.settings(row), {row['queue']})) == (status != 'NOT_FOUND')


def test_uncancelled_target_cannot_be_dispatched(task):
    store, steps, row, ledger, cursor = task
    client = Mock()
    with pytest.raises(PermissionError):
        asyncio.run(dispatch(store, row, client))
    client.get_workflow_handle.assert_not_called()


def test_terminal_success_is_not_relabelled_as_cancelled(task):
    store, steps, row, ledger, cursor = task
    with closing(store.tasks.connect()) as conn, conn:
        conn.execute("UPDATE tasks SET state='completed' WHERE owner=? AND id=?", (row['owner'], row['task_id']))
        conn.execute("UPDATE temporal_submissions SET state='completed' WHERE id=?", (row['id'],))
    store.cancel(row['id'], row['owner'])
    assert store.get(row['id'])['state'] == 'completed'
    assert not pending(store, store.settings(row), {row['queue']})


def test_legacy_task_cancel_does_not_install_temporal_schema(tmp_path):
    tasks = TaskStore(tmp_path / 'legacy.db')
    tasks.cancel('missing-owner', 'missing-task')
    tasks.cancel_session('missing-owner')
    with closing(tasks.connect()) as conn:
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'temporal_%'").fetchall()


def test_existing_cancelled_submission_gets_delivery_on_upgrade(task):
    store, steps, row, ledger, cursor = task
    store.tasks.cancel(row['owner'], row['task_id'])
    with closing(store.tasks.connect()) as conn, conn:
        conn.execute('DROP TABLE temporal_cancellations')
    upgraded = Store(store.path)
    event, = pending(upgraded, upgraded.settings(row), {row['queue']})
    assert event['id'] == row['id'] and event['state'] == 'cancelled'


@pytest.mark.parametrize('error', [OSError('offline'), ValueError('invalid approval config'), CapacityError('full')])
def test_grant_cleanup_failure_does_not_block_remote_cancellation(task, error):
    pytest.importorskip('temporalio')
    store, steps, row, ledger, cursor = task
    handle = Mock(cancel=AsyncMock())
    client = Mock(get_workflow_handle=Mock(return_value=handle))
    with patch('agent_execution.temporal_backend.approval_gate.cancel_waits', side_effect=error):
        store.cancel(row['id'], row['owner'])
        assert pending(store, store.settings(row), {row['queue']})
        asyncio.run(dispatch(store, row, client))
    handle.cancel.assert_awaited_once()
    assert store.get(row['id'])['state'] == 'cancelled'
    assert not pending(store, store.settings(row), {row['queue']})
