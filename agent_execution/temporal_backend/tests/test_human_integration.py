"""Real Service/Worker human waiting, outbox delivery, restart and replay."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pytest

pytest.importorskip('temporalio')
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker, Replayer
from openai.types.chat import ChatCompletionMessage

from exceptions import TaskSuspendedException
from agent_execution.tasks import durable_invoke, task_owner, dispatch_task_input
from agent_execution.policy import current_context
from agent_execution.temporal_backend.contracts import STEP_ACTIVITIES, HUMAN_SIGNAL
from agent_execution.temporal_backend.decisions import Decisions
from agent_execution.temporal_backend.step_activities import ACTIVITIES, tool, advance
from agent_execution.temporal_backend.store import Store
from agent_execution.temporal_backend.workflows import AgentTaskWorkflowV2
from agent_execution.temporal_backend.tests.test_integration import configured, server


def provider():
    def reply(content=None, calls=None):
        return NS(choices=[NS(message=ChatCompletionMessage(role='assistant', content=content, tool_calls=calls))])
    create = Mock(side_effect=[reply(calls=[
        {'id': 'ask-one', 'type': 'function', 'function': {'name': 'ask_user', 'arguments': '{"question":"private label?"}'}},
        {'id': 'read-one', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{"path":"note.txt"}'}}]),
        reply('private evidence'), reply('{"success":true,"score":1.0,"feedback":"ok"}')])
    return NS(chat=NS(completions=NS(create=create))), create


@pytest.mark.parametrize('lose_reply', [False, True])
def test_answer_survives_worker_restart_and_duplicate_signal(server, configured, monkeypatch, lose_reply):
    monkeypatch.setenv('AGENT_TEMPORAL_ENGINE', 'human')
    (configured/'note.txt').write_text('private evidence', encoding='utf-8')
    raw, create = provider()
    messages = [{'role': 'user', 'content': 'Ask for my label, read note.txt and report its content.'}]
    fallback = Mock(side_effect=AssertionError('Legacy fallback'))
    lost = []

    @activity.defn(name=STEP_ACTIVITIES['tool'])
    def lose_human_reply(payload: dict) -> dict:
        result = advance(payload, 'tool')
        if lose_reply and result['next_kind'] == 'human' and not lost:
            lost.append(True)
            raise ApplicationError('lost', non_retryable=False)
        return result

    async def exercise():
        client = await server.client()
        store = Store()
        activities = [fn for fn in ACTIVITIES if fn is not tool]+[lose_human_reply]
        with ThreadPoolExecutor(max_workers=2) as executor, patch('server.get_client', return_value=raw):
            async with Worker(client, task_queue=store.queue_for('human'), workflows=[AgentTaskWorkflowV2],
                              activities=activities, activity_executor=executor):
                with pytest.raises(TaskSuspendedException) as suspended:
                    await asyncio.wait_for(asyncio.to_thread(durable_invoke, fallback, messages, None), 45)
                assert str(suspended.value.question) == 'private label?'
                assert create.call_count == 1
                assert not any(m.get('role') == 'tool' and m.get('tool_call_id') == 'read-one' for m in suspended.value.messages)
                row = store.for_task(task_owner(current_context()), 'integration-task')
                handle = client.get_workflow_handle(row['workflow_id'])
                # A forged/early notification cannot provide a human answer.
                await handle.signal(HUMAN_SIGNAL, {'schema_version': 1, 'submission_id': row['id']})
            decisions = Decisions(store)
            if lose_reply:
                decisions.answer(row['id'], row['owner'], 'ask-one', 'private human answer')
                decisions.answer(row['id'], row['owner'], 'ask-one', 'private human answer')
                assert len(decisions.pending()) == 1
            async with Worker(client, task_queue=store.queue_for('human'), workflows=[AgentTaskWorkflowV2],
                              activities=activities, activity_executor=executor):
                if lose_reply:
                    event = decisions.pending()[0]
                    await decisions.dispatch(client, event)
                    await decisions.dispatch(client, event)  # Crash after RPC before ack.
                else:
                    # Existing human resume route joins the bound Workflow even
                    # after the default for new tasks changes back to bridge.
                    monkeypatch.setenv('AGENT_TEMPORAL_ENGINE', 'bridge')
                    from agent_runtime import AgentRuntime
                    runtime = AgentRuntime(messages=[], guard=None, memory=None, memory_searcher=None,
                        skill_matcher=None, memory_worker=None, memory_classifier_factory=None,
                        semantic_extractor=None, procedure_matcher=None, run_agent=fallback)
                    reply = await asyncio.wait_for(asyncio.to_thread(dispatch_task_input, 'private human answer', runtime), 45)
                    assert reply.reply == 'private evidence' and not reply.suspended
                assert await asyncio.wait_for(handle.result(), 45) == {'submission_id': row['id']}
        task = store.tasks.get(row['owner'], row['task_id'])
        assert task['state'] == 'completed' and task['reply'] == 'private evidence'
        assert sum(m.get('tool_call_id') == 'ask-one' for m in task['checkpoint']['messages']) == 1
        assert create.call_count == 3
        with closing(store.tasks.connect()) as conn:
            assert conn.execute('SELECT requests FROM task_budgets').fetchone()[0] == 3
            assert conn.execute('SELECT state FROM temporal_human_answers').fetchone()[0] == 'consumed'
            assert conn.execute("SELECT COUNT(*) FROM operation_ledger WHERE kind='tool'").fetchone()[0] == 2
        history = await handle.fetch_history()
        assert 'private' not in history.to_json()
        assert 'note.txt' not in history.to_json()
        await Replayer(workflows=[AgentTaskWorkflowV2]).replay_workflow(history)
        fallback.assert_not_called()
    asyncio.run(exercise())


def test_human_timer_expires_without_holding_activity_thread(server, configured, monkeypatch):
    monkeypatch.setenv('AGENT_TEMPORAL_ENGINE', 'human')
    raw, create = provider()
    messages = [{'role': 'user', 'content': 'Ask for my label, read note.txt and report its content.'}]

    @activity.defn(name=STEP_ACTIVITIES['tool'])
    def short_wait(payload: dict) -> dict:
        result = advance(payload, 'tool')
        if result['next_kind'] == 'human':
            with closing(Store().tasks.connect()) as conn, conn:
                conn.execute('UPDATE temporal_human_answers SET expires=?', (time.time()+1,))
        return result

    async def exercise():
        client = await server.client()
        store = Store()
        activities = [fn for fn in ACTIVITIES if fn is not tool]+[short_wait]
        with ThreadPoolExecutor(max_workers=1) as executor, patch('server.get_client', return_value=raw):
            async with Worker(client, task_queue=store.queue_for('human'), workflows=[AgentTaskWorkflowV2],
                              activities=activities, activity_executor=executor):
                with pytest.raises(TaskSuspendedException):
                    await asyncio.wait_for(asyncio.to_thread(durable_invoke, Mock(), messages, None), 45)
                row = store.for_task(task_owner(current_context()), 'integration-task')
                handle = client.get_workflow_handle(row['workflow_id'])
                await asyncio.wait_for(handle.result(), 15)
        assert store.get(row['id'])['state'] == 'failed'
        assert 'expired' in store.tasks.get(row['owner'], row['task_id'])['reply']
        assert create.call_count == 1
        await Replayer(workflows=[AgentTaskWorkflowV2]).replay_workflow(await handle.fetch_history())
    asyncio.run(exercise())
