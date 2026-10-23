"""Real V3 Service/Worker, filesystem approvals, projection loss and unknown effects."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch

import pytest
pytest.importorskip('temporalio')
from temporalio import activity
from temporalio.worker import Worker,Replayer
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from openai.types.chat import ChatCompletionMessage

from agent_execution.tasks import durable_invoke,task_owner
from agent_execution.policy import current_context
from agent_execution.approvals import ApprovalStore
from agent_execution.budgets import TaskBudget
from agent_execution.temporal_backend.admission import owner_key
from agent_execution.temporal_backend.approval_gate import ApprovalGate
from agent_execution.temporal_backend.approval_outbox import pending
from agent_execution.temporal_backend.store import Store
from agent_execution.temporal_backend.ledger import Ledger
from agent_execution.temporal_backend.step_activities import ACTIVITIES,tool,advance
from agent_execution.temporal_backend.contracts import STEP_ACTIVITIES
from agent_execution.temporal_backend.workflows import AgentTaskWorkflowV3
from agent_execution.temporal_backend.tests.test_integration import configured,server
from agent_execution.temporal_backend import file_writes as writes
from agent_execution.temporal_backend.write_resolution import record
from exceptions import TaskSuspendedException


@pytest.mark.parametrize('fault',['none','consume','effect','projection','publication'])
def test_real_approval_bound_write_never_repeats_after_lost_result(server,configured,monkeypatch,fault):
    for key,value in {'AGENT_TEMPORAL_ENGINE':'approval','AGENT_TEMPORAL_FILE_WRITES':'1',
        'AGENT_TASK_MAX_REQUESTS':'8','AGENT_TASK_DEADLINE_SECONDS':'90'}.items():
        monkeypatch.setenv(key,value)
    monkeypatch.setenv('AGENT_TEMPORAL_TRIAL_OWNERS',json.dumps([owner_key(task_owner(current_context()))]))
    body = 'TP17 private durable file evidence 繁體中文'
    args = {'path':'manual/note.md','content':body}
    def reply(content=None,calls=None):
        return NS(choices=[NS(message=ChatCompletionMessage(role='assistant',content=content,tool_calls=calls))])
    responses = [reply(calls=[{'id':'write-one','type':'function','function':{
        'name':'write_file','arguments':json.dumps(args)}}]),reply('文件已完成，內容已寫入。'),
        reply('{"success":true,"score":1.0,"feedback":"document exists"}')]
    def completion(**kwargs):
        response = responses.pop(0)
        if not kwargs.get("stream"):
            return response
        message = response.choices[0].message
        delta = NS(content=message.content,tool_calls=[NS(index=i,id=c.id,function=c.function) for i,c in enumerate(message.tool_calls or [])])
        return (chunk for chunk in [NS(choices=[NS(index=0,delta=delta,finish_reason="tool_calls" if message.tool_calls else "stop")])])
    create = Mock(side_effect=completion)
    raw = NS(chat=NS(completions=NS(create=create)))
    replaced = []
    original_replace = writes.os.replace
    def replace(source,target):
        if str(target)==str(configured/'manual/note.md'):
            replaced.append(1)
        return original_replace(source,target)
    original_complete = Ledger.complete
    def complete(self,step,*rest):
        if fault=='effect' and step=='model:1:tool:write-one':
            raise RuntimeError('lost after actual file replacement')
        return original_complete(self,step,*rest)
    original_execute = writes.execute_bound
    def execute(name,arguments,function):
        if fault=='consume' and name=='write_file':
            raise RuntimeError('lost after consumption before write')
        return original_execute(name,arguments,function)
    lost = []
    @activity.defn(name=STEP_ACTIVITIES['tool'])
    def lost_projection(payload:dict)->dict:
        value = advance(payload,'tool')
        if fault=='projection' and not lost:
            lost.append(1)
            raise ApplicationError('projection reply lost',type='StepExecutionUnknown',non_retryable=False)
        return value

    async def exercise():
        client = await server.client()
        store,approvals = Store(),ApprovalStore()
        gate = ApprovalGate(store)
        messages = [{'role':'user','content':'建立文件 manual/note.md，內容為 '+body}]
        fallback = Mock(side_effect=AssertionError('No legacy execution'))
        activities = [fn for fn in ACTIVITIES if fn is not tool]+[lost_projection]
        from agent_execution.temporal_backend.publication import runtime_scope
        publication = runtime_scope(NS(memory=None,memory_worker=None),None) if fault=='publication' else nullcontext()
        with publication,ThreadPoolExecutor(max_workers=2) as executor,patch('server.get_client',return_value=raw),patch.object(Ledger,'complete',complete),patch.object(writes,'execute_bound',execute),patch.object(writes.os,'replace',replace):
            async with Worker(client,task_queue=store.queue_for('approval'),workflows=[AgentTaskWorkflowV3],
                              activities=activities,activity_executor=executor):
                with pytest.raises(TaskSuspendedException):
                    await asyncio.wait_for(asyncio.to_thread(durable_invoke,fallback,messages,None),45)
                row = store.for_task(task_owner(current_context()),'integration-task')
                task = store.tasks.get(row['owner'],row['task_id'])
                key = task['checkpoint']['approval_wait']['id']
                grant = approvals.pending('local',current_context().session)[0]
                assert 'temporal_file_write' in grant['action']
                assert 'file_precondition' in json.loads(grant['arguments'])
                assert not (configured/'manual').exists() and replaced==[]
            # Decision survives Worker restart; no actor or callback enters History.
            approvals.decide(key,'local',current_context().session,True)
            handle = client.get_workflow_handle(row['workflow_id'])
            async with Worker(client,task_queue=store.queue_for('approval'),workflows=[AgentTaskWorkflowV3],
                              activities=activities,activity_executor=executor):
                await gate.dispatch(client,pending(approvals)[0])
                if fault in {'consume','effect'}:
                    with pytest.raises(WorkflowFailureError):
                        await asyncio.wait_for(handle.result(),45)
                else:
                    assert await asyncio.wait_for(handle.result(),45)=={'submission_id':row['id']}
                result = await asyncio.wait_for(asyncio.to_thread(durable_invoke,fallback,messages,None,True),15)
        final = store.for_task(row['owner'],row['task_id'])
        before = TaskBudget(row['owner'],row['task_id']).snapshot()
        if fault in {'none','projection','publication'}:
            assert final['state']=='completed'
            assert '文件' in result and (configured/'manual/note.md').read_text(encoding='utf-8')==body
            assert len(replaced)==1 and create.call_count==3
        else:
            assert final['state']=='uncertain' and result.startswith('Error: Temporal')
            assert len(replaced)==int(fault=='effect') and create.call_count==1
            assert record(store.tasks,row['owner'],row['task_id'],'write-one',json.dumps({
                'status':'matches_expected' if fault=='effect' else 'matches_before','note':'Human verified isolated output'}))
            assert store.get(row['id'])['state']=='uncertain'
        assert before['requests']==create.call_count
        assert approvals.decision_state(key,'local',current_context().session)=='consumed'
        with closing(store.tasks.connect()) as conn:
            op = conn.execute("SELECT state FROM operation_ledger WHERE kind='tool'").fetchone()
            assert op[0]==('completed' if fault in {'none','projection','publication'} else 'uncertain')
        if fault=='publication':
            with store.tasks.connect() as conn:
                assert conn.execute("SELECT count(*) FROM temporal_events WHERE event_key='final:1'").fetchone()[0]==1
                assert conn.execute("SELECT count(*) FROM temporal_events WHERE kind='tool.result'").fetchone()[0]==1
        assert not list(configured.rglob('.pythonai-tp17-*.tmp'))
        history = await handle.fetch_history()
        assert body not in history.to_json() and 'manual/note.md' not in history.to_json()
        await Replayer(workflows=[AgentTaskWorkflowV3]).replay_workflow(history)
        after = TaskBudget(row['owner'],row['task_id']).snapshot()
        assert (before['requests'],before['deadline'])==(after['requests'],after['deadline'])
        fallback.assert_not_called()
    asyncio.run(exercise())
