"""Real Service/Worker tests for the opt-in step protocol; provider responses controlled."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pytest
import httpx
pytest.importorskip('temporalio')
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker, Replayer
from openai.types.chat import ChatCompletionMessage
from openai import APIStatusError

from agent_execution.tasks import durable_invoke, task_owner
from agent_execution.policy import current_context
from agent_execution.temporal_backend.contracts import STEP_ACTIVITIES, STEP_WORKFLOW_NAME
from agent_execution.temporal_backend.step_activities import ACTIVITIES, advance, model, tool, authority
from agent_execution.temporal_backend.context import execution_context
from agent_execution.temporal_backend.store import Store, canonical
from agent_execution.temporal_backend.workflows import AgentTaskWorkflowV1, ReadOnlyBridge
from agent_execution.temporal_backend.tests.test_integration import configured, server


def provider():
    def reply(content=None,calls=None):
        return NS(choices=[NS(message=ChatCompletionMessage(role='assistant',content=content,tool_calls=calls))])
    create=Mock(side_effect=[reply(calls=[{'id':'read-one','type':'function','function':{
        'name':'read_file','arguments':'{"path":"note.txt"}'}}]),reply('step-private-evidence'),
        reply('{"success":true,"score":1.0,"feedback":"ok"}')])
    return NS(chat=NS(completions=NS(create=create))),create


async def invoke():
    messages=[{'role':'user','content':'Read note.txt and report its content.'}]
    fallback=Mock(side_effect=AssertionError('Legacy fallback must not execute'))
    reply=await asyncio.wait_for(asyncio.to_thread(durable_invoke,fallback,messages,None),60)
    fallback.assert_not_called()
    return reply,messages


@pytest.mark.parametrize('lose_reply',[False,True])
def test_step_workflow_real_io_cached_projection_and_replay(server,configured,monkeypatch,lose_reply):
    monkeypatch.setenv('AGENT_TEMPORAL_ENGINE','steps')
    (configured/'note.txt').write_text('step-private-evidence',encoding='utf-8')
    raw,create=provider()
    lost=[]
    @activity.defn(name=STEP_ACTIVITIES['model'])
    def model_reply_lost(payload:dict)->dict:
        result=advance(payload,'model')
        if lose_reply and not lost:
            lost.append(1)
            raise ApplicationError('reply_lost',type='ProjectionDeliveryUnknown',non_retryable=False)
        return result
    async def exercise():
        client=await server.client()
        store=Store()
        activities=[fn for fn in ACTIVITIES if fn is not model]+[model_reply_lost]
        with ThreadPoolExecutor(max_workers=2) as executor,patch('server.get_client',return_value=raw):
            async with Worker(client,task_queue=store.queue_for('steps'),workflows=[AgentTaskWorkflowV1],
                              activities=activities,activity_executor=executor):
                reply,messages=await invoke()
        assert reply=='step-private-evidence'
        assert create.call_count==3
        assert any(m.get('role')=='tool' and 'step-private-evidence' in m['content'] for m in messages)
        row=store.for_task(task_owner(current_context()),'integration-task')
        assert row['state']=='completed' and store.workflow_type(row)==STEP_WORKFLOW_NAME
        with closing(store.tasks.connect()) as conn:
            assert [r[0] for r in conn.execute('SELECT state FROM operation_ledger')]==['completed']*4
            requests=conn.execute('SELECT requests FROM task_budgets').fetchone()[0]
        assert requests==3
        history=await client.get_workflow_handle(row['workflow_id']).fetch_history()
        assert 'step-private-evidence' not in history.to_json()
        if not lose_reply:
            from agent_execution.temporal_backend.tests.replay_fixtures import save
            save('v1',history,row['id'])
        names=[e.activity_task_scheduled_event_attributes.activity_type.name for e in history.events
               if e.HasField('activity_task_scheduled_event_attributes')]
        assert names.count(STEP_ACTIVITIES['recover'])==int(lose_reply)
        assert names.count(STEP_ACTIVITIES['tool'])==1
        scheduled=[e.activity_task_scheduled_event_attributes for e in history.events
                   if e.HasField('activity_task_scheduled_event_attributes')]
        for item in scheduled:
            expected=3 if item.activity_type.name in {STEP_ACTIVITIES['bootstrap'],
                STEP_ACTIVITIES['recover'],STEP_ACTIVITIES['finalize']} else 1
            assert item.retry_policy.maximum_attempts==expected
        await Replayer(workflows=[ReadOnlyBridge,AgentTaskWorkflowV1]).replay_workflow(history)
    asyncio.run(exercise())


def test_unknown_tool_effect_stops_without_another_tool_attempt(server,configured,monkeypatch):
    monkeypatch.setenv('AGENT_TEMPORAL_ENGINE','steps')
    raw,create=provider()
    effects=[]
    @activity.defn(name=STEP_ACTIVITIES['tool'])
    def interrupted_tool(payload:dict)->dict:
        store,row,steps=authority(payload)
        with execution_context(store,row) as ledger:
            steps.claim(payload,ledger)
            task=store.tasks.get(row['owner'],row['task_id'])
            with closing(store.tasks.connect()) as conn,conn:
                conn.execute("UPDATE tasks SET state='running',phase='tool_started' WHERE owner=? AND id=?",
                             (row['owner'],row['task_id']))
            ledger.prepare('model:1:tool:read-one','tool',{})
            ledger.claim('model:1:tool:read-one')
            effects.append(1)
        raise ApplicationError('worker_lost',type='StepExecutionUnknown',non_retryable=False)
    async def exercise():
        client=await server.client();store=Store()
        activities=[fn for fn in ACTIVITIES if fn is not tool]+[interrupted_tool]
        with ThreadPoolExecutor(max_workers=2) as executor,patch('server.get_client',return_value=raw):
            async with Worker(client,task_queue=store.queue_for('steps'),workflows=[AgentTaskWorkflowV1],
                              activities=activities,activity_executor=executor):
                reply,_=await invoke()
        assert reply.startswith('Error: Temporal')
        row=store.for_task(task_owner(current_context()),'integration-task')
        assert row['state']=='uncertain'
        assert store.tasks.get(row['owner'],row['task_id'])['state']=='uncertain'
        assert effects==[1] and create.call_count==1
    asyncio.run(exercise())


def test_worker_restart_before_tool_preserves_progress_and_budget(server,configured,monkeypatch):
    monkeypatch.setenv('AGENT_TEMPORAL_ENGINE','steps')
    (configured/'note.txt').write_text('step-private-evidence',encoding='utf-8')
    raw,create=provider();waiting=threading.Event()
    @activity.defn(name=STEP_ACTIVITIES['tool'])
    def waiting_for_restart(payload:dict)->dict:
        waiting.set()
        while not activity.is_worker_shutdown():
            threading.Event().wait(.02)
        raise ApplicationError('worker_restart',type='StepExecutionUnknown',non_retryable=False)
    async def exercise():
        client=await server.client();store=Store()
        first_activities=[fn for fn in ACTIVITIES if fn is not tool]+[waiting_for_restart]
        with ThreadPoolExecutor(max_workers=2) as executor,patch('server.get_client',return_value=raw):
            async with Worker(client,task_queue=store.queue_for('steps'),workflows=[AgentTaskWorkflowV1],
                              activities=first_activities,activity_executor=executor):
                submitted=asyncio.create_task(invoke())
                assert await asyncio.to_thread(waiting.wait,15)
            async with Worker(client,task_queue=store.queue_for('steps'),workflows=[AgentTaskWorkflowV1],
                              activities=ACTIVITIES,activity_executor=executor):
                reply,_=await submitted
        assert reply=='step-private-evidence' and create.call_count==3
        row=store.for_task(task_owner(current_context()),'integration-task')
        assert row['state']=='completed'
        history=await client.get_workflow_handle(row['workflow_id']).fetch_history()
        await Replayer(workflows=[AgentTaskWorkflowV1]).replay_workflow(history)
    asyncio.run(exercise())


def test_deployment_worker_process_upgrade_drains_existing_v1_run(server, configured, monkeypatch, tmp_path):
    """A run started by the old process resumes on the replacement V1 process."""
    from dataclasses import replace
    from agent_execution import execution_scope
    from agent_execution.temporal_backend.client import dispatch
    from agent_execution.temporal_backend.config import Settings
    from agent_execution.temporal_backend.tests.test_integration import prepare

    monkeypatch.setenv('AGENT_TEMPORAL_ENGINE', 'steps')
    monkeypatch.setenv('AGENT_TRACE_ENABLED', '1')
    monkeypatch.setenv('AGENT_TRACE_DB', str(tmp_path / 'traces.db'))
    (configured / 'note.txt').write_text('process-drain-evidence', encoding='utf-8')
    marker = tmp_path / 'worker.marker'

    async def exercise():
        client = await server.client()
        with execution_scope(task_id='deployment-drain'):
            store, row = prepare(replace(Settings.load(), engine='steps'))
        base = [sys.executable, '-m', 'agent_execution.temporal_backend.tests.rollout_worker']

        def start(mode):
            output = (tmp_path / f'{mode}.log').open('ab')
            process = subprocess.Popen(base + [mode, str(marker)], cwd=Path.cwd(),
                stdout=output, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            return process, output

        old, old_output = start('old')
        replacement = replacement_output = None
        try:
            deadline = time.monotonic() + 15
            while not marker.exists() and time.monotonic() < deadline:
                if old.poll() is not None:
                    raise RuntimeError((tmp_path / 'old.log').read_text(errors='replace'))
                await asyncio.sleep(.05)
            assert marker.exists(), 'old Worker process did not become ready'
            handle = await dispatch(store, row, client)
            deadline = time.monotonic() + 15
            while marker.read_text(encoding='utf-8') != 'started' and time.monotonic() < deadline:
                if old.poll() is not None:
                    raise RuntimeError((tmp_path / 'old.log').read_text(errors='replace'))
                await asyncio.sleep(.05)
            assert marker.read_text(encoding='utf-8') == 'started', 'old Worker did not claim the run'
            old.terminate()
            old.wait(timeout=10)
            old_output.close()
            marker.unlink()
            replacement, replacement_output = start('new')
            deadline = time.monotonic() + 20
            while not marker.exists() and time.monotonic() < deadline:
                if replacement.poll() is not None:
                    raise RuntimeError((tmp_path / 'new.log').read_text(errors='replace'))
                await asyncio.sleep(.05)
            assert marker.exists(), 'replacement Worker did not become ready'
            result = await asyncio.wait_for(handle.result(), 110)
            assert result == {'submission_id': row['id']}
            assert store.get(row['id'])['state'] == 'completed'
            history = await handle.fetch_history()
            attempts = [event.activity_task_started_event_attributes.attempt
                        for event in history.events
                        if event.HasField('activity_task_started_event_attributes')]
            assert len(attempts) >= 2
            replay = await Replayer(workflows=[AgentTaskWorkflowV1]).replay_workflow(history)
            assert replay.replay_failure is None
            from agent_execution.operations.tracing import TraceStore
            with execution_scope(task_id='deployment-drain'):
                traces = TraceStore().list()
            activities = [item for item in traces if item['kind'] == 'workflow_activity']
            task_spans = {item['id']: item for item in traces if item['kind'] == 'task'}
            model_steps = [item for item in traces if item['kind'] == 'attempt']
            assert activities and model_steps
            assert all(step['trace_id'] == activities[0]['trace_id'] for step in model_steps)
            for step in model_steps:
                task_span = task_spans[step['parent_span']]
                assert task_span['parent_span'] in {item['id'] for item in activities}
            assert 'process-drain-evidence' not in json.dumps(traces)
        finally:
            for process, output in ((old, old_output), (replacement, replacement_output)):
                if process is not None:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                    if output is not None:
                        output.close()

    asyncio.run(exercise())


def test_provider_retry_stays_inside_one_activity_and_each_request_is_charged(server,configured,monkeypatch):
    monkeypatch.setenv('AGENT_TEMPORAL_ENGINE','steps')
    (configured/'note.txt').write_text('step-private-evidence',encoding='utf-8')
    raw,create=provider()
    replies=list(create.side_effect)
    response=httpx.Response(429,headers={'retry-after':'0'},request=httpx.Request('POST','http://provider/v1'))
    create.side_effect=[APIStatusError('capacity',response=response,body={}),*replies]
    async def exercise():
        client=await server.client();store=Store()
        with ThreadPoolExecutor(max_workers=2) as executor,patch('server.get_client',return_value=raw),\
                patch('server.random.uniform',return_value=0):
            async with Worker(client,task_queue=store.queue_for('steps'),workflows=[AgentTaskWorkflowV1],
                              activities=ACTIVITIES,activity_executor=executor):
                reply,_=await invoke()
        assert reply=='step-private-evidence' and create.call_count==4
        row=store.for_task(task_owner(current_context()),'integration-task')
        with closing(store.tasks.connect()) as conn:
            assert conn.execute('SELECT requests FROM task_budgets').fetchone()[0]==4
            assert conn.execute('SELECT COUNT(*) FROM budget_calls').fetchone()[0]==4
        history=await client.get_workflow_handle(row['workflow_id']).fetch_history()
        scheduled=[e.activity_task_scheduled_event_attributes for e in history.events
                   if e.HasField('activity_task_scheduled_event_attributes')]
        models=[item for item in scheduled if item.activity_type.name==STEP_ACTIVITIES['model']]
        assert len(models)==2 and all(item.retry_policy.maximum_attempts==1 for item in models)
        await Replayer(workflows=[AgentTaskWorkflowV1]).replay_workflow(history)
    asyncio.run(exercise())


def test_worker_entrypoint_serves_old_and_new_protocol_queues(server,configured,monkeypatch):
    from dataclasses import replace
    from datetime import timedelta
    from agent_execution import execution_scope
    from agent_execution.temporal_backend.config import Settings
    from agent_execution.temporal_backend.client import dispatch
    from agent_execution.temporal_backend.worker import serve
    from agent_execution.temporal_backend.tests.test_integration import prepare
    from task_evaluator import TaskEvaluator, EvaluationResult
    class Evaluator(TaskEvaluator):
        def capture_state(self): return {}
        def evaluate(self,**kwargs): return EvaluationResult(True,1,'ok')
    (configured/'note.txt').write_text('dual-queue',encoding='utf-8')
    def completion(**kwargs):
        calls=None if any(m.get('role')=='tool' for m in kwargs['messages']) else [
            {'id':'read','type':'function','function':{'name':'read_file','arguments':'{"path":"note.txt"}'}}]
        return NS(choices=[NS(message=ChatCompletionMessage(role='assistant',content='dual-queue' if calls is None else None,tool_calls=calls))])
    raw=NS(chat=NS(completions=NS(create=Mock(side_effect=completion))))
    async def exercise():
        client=await server.client()
        rows=[]
        for engine in ('bridge','steps'):
            with execution_scope(task_id='dual-'+engine):
                store,row=prepare(replace(Settings.load(),engine=engine))
                rows.append(row)
        with patch('server.get_client',return_value=raw),patch('server.TaskEvaluator',Evaluator):
            running=asyncio.create_task(serve())
            try:
                handles=[await dispatch(store,row,client) for row in rows]
                results=await asyncio.wait_for(asyncio.gather(*(h.result() for h in handles)),60)
                assert results==[{'submission_id':row['id']} for row in rows]
                assert all(store.get(row['id'])['state']=='completed' for row in rows)
            finally:
                running.cancel()
                await asyncio.gather(running,return_exceptions=True)
        assert raw.chat.completions.create.call_count==4
    asyncio.run(exercise())


def test_queued_workflow_cancellation_projects_without_model_execution(server,configured,monkeypatch):
    from dataclasses import replace
    from agent_execution import execution_scope
    from agent_execution.temporal_backend.client import dispatch
    from agent_execution.temporal_backend.config import Settings
    from agent_execution.temporal_backend.tests.test_integration import prepare
    from agent_execution.temporal_backend.worker import serve
    monkeypatch.setenv('AGENT_TEMPORAL_ENGINE','steps')
    raw=NS(chat=NS(completions=NS(create=Mock(side_effect=AssertionError('cancelled task called provider')))))
    async def exercise():
        client=await server.client()
        with execution_scope(task_id='queued-cancel'):
            store,row=prepare(replace(Settings.load(),engine='steps'))
        handle=await dispatch(store,row,client)
        await handle.cancel()
        with patch('server.get_client',return_value=raw):
            running=asyncio.create_task(serve())
            try:
                deadline=asyncio.get_running_loop().time()+12
                while store.get(row['id'])['state']!='cancelled':
                    if asyncio.get_running_loop().time()>=deadline:
                        raise TimeoutError('cancelled Workflow was not projected')
                    await asyncio.sleep(.1)
            finally:
                running.cancel()
                await asyncio.gather(running,return_exceptions=True)
        assert store.tasks.get(row['owner'],row['task_id'])['state']=='cancelled'
        raw.chat.completions.create.assert_not_called()
    asyncio.run(exercise())
