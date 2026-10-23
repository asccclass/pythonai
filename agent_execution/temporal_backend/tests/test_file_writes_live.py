"""Opt-in real provider write trial in an isolated workspace; no production settings."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
import time
from unittest.mock import Mock,patch

import pytest
pytest.importorskip('temporalio')
from temporalio.worker import Worker,Replayer
from agent_execution.approvals import ApprovalStore
from agent_execution.tasks import durable_invoke,task_owner
from agent_execution.policy import current_context
from agent_execution.budgets import TaskBudget
from agent_execution.temporal_backend.admission import owner_key
from agent_execution.temporal_backend.approval_gate import ApprovalGate
from agent_execution.temporal_backend.approval_outbox import pending
from agent_execution.temporal_backend.store import Store
from agent_execution.temporal_backend.step_activities import ACTIVITIES
from agent_execution.temporal_backend.workflows import AgentTaskWorkflowV3
from agent_execution.temporal_backend.tests.test_integration import configured,server
from exceptions import TaskSuspendedException

pytestmark = pytest.mark.skipif(os.environ.get('PYTHONAI_TEMPORAL_WRITE_LIVE')!='1',
                                reason='Opt in to isolated real-provider file writes')


def test_live_wiki_write_uses_exact_human_approval_and_preserves_results(server,configured,monkeypatch):
    for key,value in {'AGENT_TEMPORAL_ENGINE':'approval','AGENT_TEMPORAL_FILE_WRITES':'1',
        'AGENT_TASK_MAX_REQUESTS':'8','AGENT_TASK_MAX_TOKENS':'128000',
        'AGENT_MODEL_MAX_OUTPUT_TOKENS':'1024','AGENT_TASK_DEADLINE_SECONDS':'90'}.items():
        monkeypatch.setenv(key,value)
    monkeypatch.setenv('AGENT_TEMPORAL_TRIAL_OWNERS',json.dumps([owner_key(task_owner(current_context()))]))
    directories = {'llm-wiki/'+name for name in ['sources','notes','concepts','projects','people','daily','templates']}
    marker = 'TP17 verified isolated wiki'
    goal = ('請實際建立 llm-wiki 結構，目錄為 '+','.join(sorted(directories))+
            '。再寫入 llm-wiki/README.md，內容包含 '+marker+
            '，可有簡短 Markdown。使用 create_directory 批次建立目錄及 write_file；只需目錄與 Markdown 文件。')
    import server as runner
    raw = runner.create_ollama_client().with_options(timeout=15,max_retries=0)

    async def exercise():
        client = await server.client()
        store,approvals = Store(),ApprovalStore()
        gate = ApprovalGate(store)
        messages = [{'role':'user','content':goal}]
        fallback = Mock(side_effect=AssertionError('No legacy fallback'))
        with ThreadPoolExecutor(max_workers=2) as executor,patch('server.get_client',return_value=raw):
            async with Worker(client,task_queue=store.queue_for('approval'),workflows=[AgentTaskWorkflowV3],
                              activities=ACTIVITIES,activity_executor=executor):
                with pytest.raises(TaskSuspendedException):
                    await asyncio.wait_for(asyncio.to_thread(durable_invoke,fallback,messages,None),45)
                row = store.for_task(task_owner(current_context()),'integration-task')
                handle = client.get_workflow_handle(row['workflow_id'])
                result = asyncio.create_task(handle.result())
                decisions = 0
                deadline = time.monotonic()+100
                try:
                    while not result.done() and time.monotonic()<deadline:
                        for grant in approvals.pending('local',current_context().session):
                            data = json.loads(grant['arguments'])
                            name,args = data['tool'],data['arguments']
                            if name=='create_directory':
                                assert isinstance(args.get('paths'),list) and set(args['paths'])<=directories|{'llm-wiki'}
                            elif name=='write_file':
                                assert args['path']=='llm-wiki/README.md'
                                assert marker in args['content'] and len(args['content'])<=2048
                            elif name in {'read_file','list_files'}:
                                assert args.get('path','.') in directories|{'llm-wiki','llm-wiki/README.md'}
                            else:
                                pytest.fail('Unexpected live tool requires separate review: '+name)
                            assert decisions<12
                            # Test administrator reviews the exact destination/content before granting.
                            approvals.decide(grant['id'],'local',current_context().session,True)
                            decisions += 1
                        for event in pending(approvals):
                            await gate.dispatch(client,event)
                        await asyncio.sleep(.1)
                    assert result.done(),'Isolated live trial exceeded its deadline'
                    assert await result=={'submission_id':row['id']}
                finally:
                    if not result.done():
                        store.tasks.cancel(row['owner'],row['task_id'])
                        await handle.cancel()
                        result.cancel()
                        await asyncio.gather(result,return_exceptions=True)
            final = store.get(row['id'])
            before = TaskBudget(row['owner'],row['task_id']).snapshot()
            print('WRITE_TRIAL_RESULT '+json.dumps({'state':final['state'],'requests':before['requests'],'approvals':decisions}),flush=True)
            assert final['state']=='completed'
            assert all((configured/path).is_dir() for path in directories)
            assert marker in (configured/'llm-wiki/README.md').read_text(encoding='utf-8')
            assert 1<=before['requests']<=8
            history = await handle.fetch_history()
            assert marker not in history.to_json() and 'llm-wiki' not in history.to_json()
            await Replayer(workflows=[AgentTaskWorkflowV3]).replay_workflow(history)
            stamp = (configured/'llm-wiki/README.md').stat().st_mtime_ns
            with patch('server.get_client',side_effect=AssertionError('Completed task requested another model')):
                reply = await asyncio.to_thread(durable_invoke,fallback,messages,None,True)
            assert reply and (configured/'llm-wiki/README.md').stat().st_mtime_ns==stamp
            after = TaskBudget(row['owner'],row['task_id']).snapshot()
            assert (before['requests'],before['deadline'])==(after['requests'],after['deadline'])
            fallback.assert_not_called()
    try:
        asyncio.run(exercise())
    finally:
        raw.close()
