from contextlib import closing
import asyncio
import copy
import time
from unittest.mock import AsyncMock, Mock

import pytest

from agent_execution.temporal_backend.decisions import Decisions
from agent_execution.temporal_backend.contracts import HUMAN_SIGNAL, HUMAN_WORKFLOW_NAME
from agent_execution.temporal_backend.store import canonical
from agent_execution.temporal_backend.tests.test_step_store import task

pytestmark = pytest.mark.parametrize('task', ['human'], indirect=True)


def suspend(task, calls=('ask-one',)):
    store, steps, row, ledger, cursor = task
    fence, _ = steps.claim(cursor, ledger)
    messages = [{'role': 'user', 'content': 'private goal'}, {'role': 'assistant', 'tool_calls': [
        {'id': call, 'type': 'function', 'function': {'name': 'ask_user', 'arguments': '{"question":"private question"}'}}
        for call in calls]}]
    saved = {'messages': messages, 'iteration': 1, 'iteration_limit': 4,
             'suspended_tool_call_id': calls[0], 'eval_retries': 0, 'provider_retry_limit': 2}
    with closing(store.tasks.connect()) as conn, conn:
        conn.execute("UPDATE tasks SET state='suspended',phase='waiting_user',checkpoint=?,reply='private question' WHERE owner=? AND id=?",
                     (canonical(saved), row['owner'], row['task_id']))
    return steps.commit(cursor, fence, ledger, 'human')


def decision(task):
    store, steps, row, ledger, cursor = task
    with closing(store.tasks.connect()) as conn:
        return dict(conn.execute('SELECT * FROM temporal_human_answers WHERE submission_id=?', (row['id'],)).fetchone())


def test_v2_is_pinned_to_an_independent_queue_and_capability_set(task, monkeypatch):
    from agent_execution.temporal_backend.config import Settings
    from agent_execution.policy import current_context
    store, steps, row, ledger, cursor = task
    assert store.workflow_type(row) == HUMAN_WORKFLOW_NAME
    assert row['queue'] == store.queue_for('human') != store.queue_for('steps')
    assert 'ask_user' in current_context().allowed_tools
    monkeypatch.setenv('AGENT_TEMPORAL_ENGINE', 'bridge')
    retried = store.submit(row['owner'], row['task_id'], [{'role': 'user', 'content': 'private step goal'}], {}, 'viewer', Settings.load())
    assert retried['id'] == row['id'] and store.workflow_type(retried) == HUMAN_WORKFLOW_NAME
    with closing(store.tasks.connect()) as conn, conn:
        conn.execute('UPDATE temporal_protocols SET version=1 WHERE submission_id=?', (row['id'],))
    with pytest.raises(ValueError, match='protocol'):
        store.workflow_type(row)


def test_wait_is_atomic_reference_only_and_recovery_reuses_it(task):
    store, steps, row, ledger, cursor = task
    waiting = suspend(task)
    assert 'private' not in canonical(waiting)
    probe = steps.human(waiting, ledger)
    assert probe['cursor'] == waiting and time.time() < probe['expires'] <= time.time()+300
    assert steps.recover(cursor, ledger) == waiting
    assert decision(task)['state'] == 'waiting'
    assert store.tasks.get(row['owner'], row['task_id'])['state'] == 'suspended'


def test_recover_confirmed_suspension_before_cursor_commit(task):
    store, steps, row, ledger, cursor = task
    waiting = suspend(task)
    with closing(store.tasks.connect()) as conn, conn:
        conn.execute('DELETE FROM temporal_step_runs WHERE submission_id=? AND revision=?', (row['id'], waiting['revision']))
        conn.execute('DELETE FROM temporal_human_answers WHERE submission_id=?', (row['id'],))
        conn.execute("UPDATE temporal_step_runs SET status='running',output=NULL WHERE submission_id=? AND revision=0", (row['id'],))
    recovered = steps.recover(cursor, ledger)
    assert recovered['next_kind'] == 'human'
    assert steps.human(recovered, ledger)['cursor'] == recovered
    assert decision(task)['state'] == 'waiting'


def test_deleted_session_cannot_receive_or_consume_human_answer(task):
    store, steps, row, ledger, cursor = task
    waiting = suspend(task)
    with closing(store.tasks.connect()) as conn, conn:
        conn.execute("UPDATE wl_sessions SET state='deleting'")
    with pytest.raises(PermissionError, match='deleting or deleted'):
        Decisions(store).answer(row['id'], row['owner'], 'ask-one', 'late')
    with pytest.raises(PermissionError, match='deleting or deleted'):
        steps.human(waiting, ledger)


@pytest.mark.parametrize('calls,expected', [(('ask-one',), 'model'), (('ask-one', 'ask-two'), 'tool')])
def test_answer_consumed_once_with_limits_and_remaining_tools_preserved(task, calls, expected):
    store, steps, row, ledger, cursor = task
    waiting = suspend(task, calls)
    decisions = Decisions(store)
    decisions.answer(row['id'], row['owner'], 'ask-one', 'private answer')
    decisions.answer(row['id'], row['owner'], 'ask-one', 'private answer')
    following = steps.human(waiting, ledger)['cursor']
    assert following['next_kind'] == expected
    assert following['revision'] == waiting['revision']+1
    assert steps.human(waiting, ledger)['cursor'] == following
    decisions.answer(row['id'], row['owner'], 'ask-one', 'private answer')
    saved = store.tasks.get(row['owner'], row['task_id'])['checkpoint']
    assert saved['messages'][-1] == {'role': 'tool', 'tool_call_id': 'ask-one', 'content': 'private answer'}
    assert sum(m.get('role') == 'tool' for m in saved['messages']) == 1
    assert saved['iteration_limit'] == 4 and saved['provider_retry_limit'] == 2
    assert decision(task)['state'] == 'consumed'
    with pytest.raises(PermissionError):
        decisions.answer(row['id'], row['owner'], 'ask-one', 'changed answer')


@pytest.mark.parametrize('answer', ['', ' ', 'x'*(1024*1024+1), None], ids=['empty', 'blank', 'oversize', 'wrong-type'])
def test_answer_size_and_type_are_bounded(task, answer):
    store, steps, row, ledger, cursor = task
    suspend(task)
    with pytest.raises(ValueError):
        Decisions(store).answer(row['id'], row['owner'], 'ask-one', answer)
    assert decision(task)['state'] == 'waiting'


def test_cross_owner_call_and_early_answer_rejected(task):
    store, steps, row, ledger, cursor = task
    decisions = Decisions(store)
    with pytest.raises(PermissionError):
        decisions.answer(row['id'], row['owner'], 'ask-one', 'too early')
    suspend(task)
    with pytest.raises(KeyError):
        decisions.answer(row['id'], 'other-owner', 'ask-one', 'answer')
    with pytest.raises(PermissionError):
        decisions.answer(row['id'], row['owner'], 'other-call', 'answer')
    assert decision(task)['state'] == 'waiting'


@pytest.mark.parametrize('answered', [False, True])
def test_ttl_does_not_extend_budget_or_accept_late_answer(task, answered):
    store, steps, row, ledger, cursor = task
    contract = store.verify_budget_contract(row)
    waiting = suspend(task)
    decisions = Decisions(store)
    if answered:
        decisions.answer(row['id'], row['owner'], 'ask-one', 'answer before TTL')
    with closing(store.tasks.connect()) as conn, conn:
        conn.execute('UPDATE temporal_human_answers SET expires=?', (time.time()-1,))
    with pytest.raises(PermissionError):
        decisions.answer(row['id'], row['owner'], 'ask-one', 'late answer')
    following = steps.human(waiting, ledger)['cursor']
    assert following['next_kind'] == 'finalize'
    assert store.tasks.get(row['owner'], row['task_id'])['state'] == 'failed'
    assert decision(task)['state'] == 'expired'
    assert store.verify_budget_contract(row) == contract


def test_cancellation_fences_answers_and_old_cursor(task):
    store, steps, row, ledger, cursor = task
    waiting = suspend(task)
    store.cancel(row['id'], row['owner'])
    with pytest.raises(PermissionError):
        Decisions(store).answer(row['id'], row['owner'], 'ask-one', 'late')
    with pytest.raises((PermissionError, InterruptedError)):
        steps.human(waiting, ledger)


def test_outbox_retries_unknown_signal_without_answer_in_payload(task):
    store, steps, row, ledger, cursor = task
    waiting = suspend(task)
    decisions = Decisions(store)
    decisions.answer(row['id'], row['owner'], 'ask-one', 'private answer')
    event = decisions.pending()[0]
    signal = AsyncMock(side_effect=RuntimeError('unknown RPC outcome'))
    client = Mock()
    client.get_workflow_handle.return_value.signal = signal
    with pytest.raises(RuntimeError):
        asyncio.run(decisions.dispatch(client, event))
    assert len(decisions.pending()) == 1
    signal.side_effect = None
    asyncio.run(decisions.dispatch(client, event))
    assert decisions.pending() == []
    signal.assert_called_with(HUMAN_SIGNAL, {'schema_version': 1, 'submission_id': row['id']})
    assert 'private' not in canonical(signal.call_args.args)
    assert steps.human(waiting, ledger)['cursor']['next_kind'] == 'model'


def test_wait_missing_artifact_and_forged_cursor_fail_closed(task):
    store, steps, row, ledger, cursor = task
    waiting = suspend(task)
    forged = copy.deepcopy(waiting)
    forged['state_ref']['sha256'] = 'f'*64
    with pytest.raises((PermissionError, ValueError)):
        steps.human(forged, ledger)
    with closing(store.tasks.connect()) as conn, conn:
        conn.execute('DELETE FROM temporal_artifacts WHERE id=?', (waiting['state_ref']['id'],))
    with pytest.raises(KeyError):
        steps.human(waiting, ledger)


def test_resume_without_new_answer_never_reuses_an_old_tool_result(task, monkeypatch):
    from exceptions import TaskSuspendedException
    from agent_execution.temporal_backend.backend import invoke
    store, steps, row, ledger, cursor = task
    suspend(task)
    saved = store.tasks.get(row['owner'], row['task_id'])['checkpoint']
    # Tool-call IDs can recur across model turns. Only a newly appended answer
    # matching the entire saved conversation may decide the current wait.
    saved['messages'].insert(1, {'role': 'tool', 'tool_call_id': 'ask-one', 'content': 'previous answer'})
    with closing(store.tasks.connect()) as conn, conn:
        conn.execute('UPDATE tasks SET checkpoint=? WHERE owner=? AND id=?',
                     (canonical(saved), row['owner'], row['task_id']))
    with pytest.raises(TaskSuspendedException):
        invoke(store, saved['messages'], resume=True)
    assert decision(task)['state'] == 'waiting'


def test_answer_storage_limit_uses_shared_artifact_capacity(task, monkeypatch):
    from agent_execution.delivery import CapacityError
    store, steps, row, ledger, cursor = task
    suspend(task)
    monkeypatch.setenv('AGENT_TEMPORAL_LEDGER_STORAGE_BYTES', '1')
    with pytest.raises(CapacityError):
        Decisions(store).answer(row['id'], row['owner'], 'ask-one', 'answer')
    assert decision(task)['state'] == 'waiting'
