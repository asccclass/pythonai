"""Real Temporal approval waits and failure injection with a controlled provider."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pytest

pytest.importorskip('temporalio')
from temporalio import activity
from temporalio.worker import Worker, Replayer
from temporalio.client import WorkflowFailureError
from openai.types.chat import ChatCompletionMessage

from agent_execution.tasks import durable_invoke, task_owner
from agent_execution.policy import current_context
from agent_execution.approvals import ApprovalStore, human_command
from agent_execution.temporal_backend.approval_gate import ApprovalGate
from agent_execution.temporal_backend.approval_outbox import pending
from agent_execution.temporal_backend.contracts import (STEP_ACTIVITIES, STEP_TOOLS,
    APPROVAL_SIGNAL, APPROVAL_WORKFLOW_NAME)
from agent_execution.temporal_backend.ledger import Ledger
from agent_execution.temporal_backend.step_activities import ACTIVITIES, approval
from agent_execution.temporal_backend.store import Store
from agent_execution.temporal_backend.workflows import AgentTaskWorkflowV1,AgentTaskWorkflowV3
from agent_execution.temporal_backend.tests.test_integration import configured, server
from exceptions import TaskSuspendedException


def test_v3_task_is_not_claimed_by_rolled_back_v1_worker(server,configured,monkeypatch):
    from agent_execution.temporal_backend.client import dispatch
    from agent_execution.temporal_backend.config import Settings

    monkeypatch.setenv('AGENT_TEMPORAL_ENGINE','approval')
    async def exercise():
        client=await server.client()
        store=Store()
        context=current_context()
        owner=task_owner(context)
        store.bind(owner,context.task_id,'temporal')
        approvals=ApprovalStore()
        scope={'source':context.source,'identity':context.identity,'session':context.session,
            'conversation':context.conversation,'project':context.project,'workspace':str(configured),
            'allowed_tools':sorted(STEP_TOOLS),'approval_db':str(approvals.path.resolve())}
        row=store.submit(owner,context.task_id,[{'role':'user','content':'Read note.txt'}],scope,'admin',Settings.load())
        assert store.workflow_type(row)==APPROVAL_WORKFLOW_NAME
        assert row['queue']==store.queue_for('approval')
        with ThreadPoolExecutor(max_workers=2) as executor:
            async with Worker(client,task_queue=store.queue_for('steps'),workflows=[AgentTaskWorkflowV1],
                              activities=ACTIVITIES,activity_executor=executor):
                handle=await dispatch(store,row,client)
                await asyncio.sleep(.5)
                assert (await handle.describe()).status.name=='RUNNING'
                history=await handle.fetch_history()
                started=next(event.workflow_execution_started_event_attributes for event in history.events
                    if event.HasField('workflow_execution_started_event_attributes'))
                assert started.workflow_type.name==APPROVAL_WORKFLOW_NAME
                assert started.task_queue.name==store.queue_for('approval')
                assert not any(event.HasField('activity_task_scheduled_event_attributes') for event in history.events)
                await handle.terminate('isolated Worker rollback drill')
    asyncio.run(exercise())


def provider():
    def reply(content=None, calls=None):
        return NS(choices=[NS(message=ChatCompletionMessage(role='assistant', content=content, tool_calls=calls))])
    create = Mock(side_effect=[reply(calls=[{'id':'read-one','type':'function',
        'function':{'name':'read_file','arguments':'{"path":"note.txt"}'}}]),
        reply('private approval evidence'), reply('{"success":true,"score":1.0,"feedback":"ok"}')])
    return NS(chat=NS(completions=NS(create=create))), create


async def start_wait(store):
    messages = [{'role':'user','content':'Read note.txt and report its content.'}]
    fallback = Mock(side_effect=AssertionError('Legacy executor must not run'))
    with pytest.raises(TaskSuspendedException) as caught:
        await asyncio.wait_for(asyncio.to_thread(durable_invoke, fallback, messages, None), 45)
    row = store.for_task(task_owner(current_context()), 'integration-task')
    task = store.tasks.get(row['owner'], row['task_id'])
    assert task['phase'] == 'waiting_approval'
    return row, task['checkpoint']['approval_wait']['id'], messages, fallback


@pytest.mark.parametrize('decision', ['approve', 'deny'])
def test_restart_duplicate_notification_and_human_decision(server, configured, monkeypatch, decision):
    monkeypatch.setenv('AGENT_TEMPORAL_ENGINE', 'approval')
    (configured/'note.txt').write_text('private approval evidence', encoding='utf-8')
    raw, create = provider()
    async def exercise():
        client = await server.client()
        store = Store()
        gate, approvals = ApprovalGate(store), ApprovalStore()
        with ThreadPoolExecutor(max_workers=2) as executor, patch('server.get_client', return_value=raw):
            async with Worker(client, task_queue=store.queue_for('approval'), workflows=[AgentTaskWorkflowV3],
                              activities=ACTIVITIES, activity_executor=executor):
                row, key, messages, fallback = await start_wait(store)
                assert create.call_count == 1
                handle = client.get_workflow_handle(row['workflow_id'])
                contract = store.verify_budget_contract(row)
                # An unauthorised Signal carries no decision and cannot release I/O.
                await handle.signal(APPROVAL_SIGNAL,
                    {'schema_version':1,'submission_id':row['id'],'event_id':'f'*32})
            text = human_command('/'+decision+' '+key, 'local', current_context().session)
            assert 'original Temporal task' in text
            event = pending(approvals)[0]
            assert json.loads(event['binding'])['submission_id'] == row['id']
            async with Worker(client, task_queue=store.queue_for('approval'), workflows=[AgentTaskWorkflowV3],
                              activities=ACTIVITIES, activity_executor=executor):
                await gate.dispatch(client, event)
                await gate.dispatch(client, event)
                assert await asyncio.wait_for(handle.result(), 45) == {'submission_id':row['id']}
        task = store.tasks.get(row['owner'], row['task_id'])
        assert task['state'] == ('completed' if decision == 'approve' else 'failed')
        assert create.call_count == (3 if decision == 'approve' else 1)
        assert store.verify_budget_contract(row) == contract
        with closing(store.tasks.connect()) as conn:
            assert conn.execute('SELECT requests FROM task_budgets').fetchone()[0] == create.call_count
            assert conn.execute("SELECT COUNT(*) FROM operation_ledger WHERE kind='tool'").fetchone()[0] == (1 if decision == 'approve' else 0)
        assert approvals.decision_state(key, 'local', current_context().session) == ('consumed' if decision == 'approve' else 'denied')
        history = await handle.fetch_history()
        assert 'private' not in history.to_json() and 'note.txt' not in history.to_json()
        if decision == 'approve':
            from agent_execution.temporal_backend.tests.replay_fixtures import save
            save('v3',history,row['id'])
        schedules = [e.activity_task_scheduled_event_attributes for e in history.events if e.HasField('activity_task_scheduled_event_attributes')]
        assert all(e.retry_policy.maximum_attempts == 1 for e in schedules if e.activity_type.name in
                   {STEP_ACTIVITIES['model'],STEP_ACTIVITIES['tool'],STEP_ACTIVITIES['evaluation']})
        await Replayer(workflows=[AgentTaskWorkflowV3]).replay_workflow(history)
        fallback.assert_not_called()
    asyncio.run(exercise())


def test_consume_and_effect_before_lost_result_never_reexecutes(server, configured, monkeypatch):
    monkeypatch.setenv('AGENT_TEMPORAL_ENGINE', 'approval')
    (configured/'note.txt').write_text('private approval evidence', encoding='utf-8')
    raw, create = provider()
    complete = Ledger.complete
    effects = []
    def lose_result(self, step, fence, result, evidence):
        if step == 'model:1:tool:read-one':
            effects.append(result)
            raise RuntimeError('worker lost after consumed operation')
        return complete(self, step, fence, result, evidence)
    async def exercise():
        client = await server.client()
        store = Store()
        gate, approvals = ApprovalGate(store), ApprovalStore()
        with ThreadPoolExecutor(max_workers=2) as executor, patch('server.get_client', return_value=raw), patch.object(Ledger, 'complete', lose_result):
            async with Worker(client, task_queue=store.queue_for('approval'), workflows=[AgentTaskWorkflowV3],
                              activities=ACTIVITIES, activity_executor=executor):
                row, key, messages, fallback = await start_wait(store)
                approvals.decide(key, 'local', current_context().session, True)
                await gate.dispatch(client, pending(approvals)[0])
                with pytest.raises(WorkflowFailureError):
                    await asyncio.wait_for(client.get_workflow_handle(row['workflow_id']).result(), 45)
                reply = await asyncio.wait_for(asyncio.to_thread(durable_invoke, fallback, messages, None, resume=True), 15)
                assert reply.startswith('Error: Temporal')
        assert store.get(row['id'])['state'] == 'uncertain'
        assert approvals.decision_state(key, 'local', current_context().session) == 'consumed'
        assert len(effects) == 1 and create.call_count == 1
        with closing(store.tasks.connect()) as conn:
            assert conn.execute("SELECT state FROM operation_ledger WHERE kind='tool'").fetchone()[0] == 'uncertain'
        with closing(approvals.connect()) as conn:
            assert conn.execute('SELECT COUNT(*) FROM approvals').fetchone()[0] == 1
    asyncio.run(exercise())


def test_approval_timer_expires_without_new_model_or_tool(server, configured, monkeypatch):
    monkeypatch.setenv('AGENT_TEMPORAL_ENGINE', 'approval')
    raw, create = provider()
    @activity.defn(name=STEP_ACTIVITIES['approval'])
    def short_wait(payload:dict)->dict:
        result = approval(payload)
        if not result['ready']:
            with closing(Store().tasks.connect()) as conn, conn:
                conn.execute('UPDATE temporal_approval_waits SET expires=?', (time.time()+.5,))
            result['expires'] = time.time()+.5
        return result
    async def exercise():
        client = await server.client()
        store = Store()
        activities = [fn for fn in ACTIVITIES if fn is not approval]+[short_wait]
        with ThreadPoolExecutor(max_workers=1) as executor, patch('server.get_client', return_value=raw):
            async with Worker(client, task_queue=store.queue_for('approval'), workflows=[AgentTaskWorkflowV3],
                              activities=activities, activity_executor=executor):
                row, key, messages, fallback = await start_wait(store)
                handle = client.get_workflow_handle(row['workflow_id'])
                await asyncio.wait_for(handle.result(), 15)
        assert store.tasks.get(row['owner'], row['task_id'])['state'] == 'failed'
        assert create.call_count == 1
        await Replayer(workflows=[AgentTaskWorkflowV3]).replay_workflow(await handle.fetch_history())
    asyncio.run(exercise())

def test_cancelled_wait_rejects_late_approval_and_never_calls_tool(server, configured, monkeypatch):
    monkeypatch.setenv('AGENT_TEMPORAL_ENGINE', 'approval')
    raw, create = provider()
    async def exercise():
        client = await server.client()
        store = Store()
        with ThreadPoolExecutor(max_workers=1) as executor, patch('server.get_client', return_value=raw):
            async with Worker(client, task_queue=store.queue_for('approval'), workflows=[AgentTaskWorkflowV3],
                              activities=ACTIVITIES, activity_executor=executor):
                row, key, messages, fallback = await start_wait(store)
                store.cancel(row['id'], row['owner'])
                with pytest.raises(PermissionError):
                    ApprovalStore().decide(key, 'local', current_context().session, True)
                handle = client.get_workflow_handle(row['workflow_id'])
                await handle.cancel()
                with pytest.raises(WorkflowFailureError):
                    await asyncio.wait_for(handle.result(), 15)
        assert store.tasks.get(row['owner'], row['task_id'])['state'] == 'cancelled'
        assert ApprovalStore().decision_state(key, 'local', current_context().session) == 'cancelled'
        assert create.call_count == 1
    asyncio.run(exercise())


def test_v3_human_answer_then_approval_uses_existing_resume_entry(server, configured, monkeypatch):
    from agent_execution.temporal_backend.tests.test_human_integration import provider as human_provider
    monkeypatch.setenv('AGENT_TEMPORAL_ENGINE', 'approval')
    (configured/'note.txt').write_text('private evidence', encoding='utf-8')
    raw, create = human_provider()
    async def exercise():
        client = await server.client()
        store = Store()
        fallback = Mock(side_effect=AssertionError('Legacy fallback'))
        messages = [{'role':'user','content':'Ask for my label, read note.txt and report its content.'}]
        with ThreadPoolExecutor(max_workers=2) as executor, patch('server.get_client', return_value=raw):
            async with Worker(client, task_queue=store.queue_for('approval'), workflows=[AgentTaskWorkflowV3],
                              activities=ACTIVITIES, activity_executor=executor):
                with pytest.raises(TaskSuspendedException) as question:
                    await asyncio.wait_for(asyncio.to_thread(durable_invoke, fallback, messages, None), 45)
                assert question.value.tool_call_id == 'ask-one'
                answered = question.value.messages+[{'role':'tool','tool_call_id':'ask-one','content':'private human answer'}]
                with pytest.raises(TaskSuspendedException):
                    await asyncio.wait_for(asyncio.to_thread(durable_invoke, fallback, answered, None, resume=True), 45)
                row = store.for_task(task_owner(current_context()), 'integration-task')
                task = store.tasks.get(row['owner'], row['task_id'])
                key = task['checkpoint']['approval_wait']['id']
                assert task['phase'] == 'waiting_approval' and create.call_count == 1
                ApprovalStore().decide(key, 'local', current_context().session, True)
                reply = await asyncio.wait_for(asyncio.to_thread(durable_invoke, fallback, task['checkpoint']['messages'], None, resume=True), 45)
                assert reply == 'private evidence'
        assert create.call_count == 3
        history = await client.get_workflow_handle(row['workflow_id']).fetch_history()
        assert 'private' not in history.to_json()
        await Replayer(workflows=[AgentTaskWorkflowV3]).replay_workflow(history)
    asyncio.run(exercise())
