"""Real local Service/Worker trial: bounded original goals, optional live provider."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pytest
pytest.importorskip('temporalio')
from temporalio.worker import Worker, Replayer
from openai.types.chat import ChatCompletionMessage

from agent_execution.tasks import durable_invoke, task_owner
from agent_execution.policy import current_context
from agent_execution.budgets import TaskBudget
from agent_execution.temporal_backend.admission import owner_key
from agent_execution.temporal_backend.store import Store
from agent_execution.temporal_backend.step_activities import ACTIVITIES
from agent_execution.temporal_backend.workflows import AgentTaskWorkflowV1
from agent_execution.temporal_backend.tests.test_integration import configured, server

URL = 'https://github.com/HarnessRouter/harnessrouter'
EVIDENCE = 'HarnessRouter uses UHP (Unified Harness Protocol) to route requests between harnesses.'
GOALS = {
    'wiki': '建立 llm-wiki 結構，包含 sources、notes、concepts、projects、people、daily、templates 目錄，以及 README.md。請實際建立，不只列計畫。',
    'router': '閱讀 ' + URL + ' 並說明原理，根據 README 說明 UHP 與使用情境。不要寫入檔案。',
}


def controlled_provider(case):
    turns = []
    def response(**kwargs):
        if kwargs.get('tools'):
            turns.append(1)
            name,args = ('create_directory',{'paths':['llm-wiki/notes']}) if case=='wiki' else ('fetch_url',{'url':URL})
            # A deliberately repetitive model exercises persisted loop detection.
            if len(turns) <= 4:
                call = {'id':'trial-'+str(len(turns)),'type':'function','function':{
                    'name':name,'arguments':json.dumps(args)}}
                message = ChatCompletionMessage(role='assistant',content=None,tool_calls=[call])
            else:
                message = ChatCompletionMessage(role='assistant',content='UHP routes harness requests.' if case=='router' else '此環境為唯讀，無法建立 llm-wiki。')
        elif kwargs.get('temperature')==0.0:
            message = ChatCompletionMessage(role='assistant',content=json.dumps({
                'success':case=='router','score':1.0 if case=='router' else 0.0,'feedback':'UHP explained' if case=='router' else 'Read-only write denied'}))
        else:
            message = ChatCompletionMessage(role='assistant',content='UHP routes harness requests.' if case=='router' else '此環境為唯讀，無法建立 llm-wiki。')
        return NS(choices=[NS(message=message)])
    create = Mock(side_effect=response)
    return NS(chat=NS(completions=NS(create=create))),create


@pytest.mark.parametrize('live',[False,pytest.param(True,marks=pytest.mark.skipif(os.environ.get('PYTHONAI_TEMPORAL_LIVE')!='1',reason='Set PYTHONAI_TEMPORAL_LIVE=1 for real-provider trial'))],ids=['controlled','live'])
@pytest.mark.parametrize('case',['wiki','router'])
def test_closed_trial_original_goals_are_bounded_and_recover_without_io(server,configured,monkeypatch,case,live):
    if live and os.environ.get('PYTHONAI_TEMPORAL_LIVE')!='1':
        pytest.skip('Set PYTHONAI_TEMPORAL_LIVE=1 for a budgeted real-provider trial')
    monkeypatch.setenv('AGENT_TEMPORAL_ENGINE','steps')
    monkeypatch.setenv('AGENT_TEMPORAL_TRIAL_OWNERS',json.dumps([owner_key(task_owner(current_context()))]))
    for key,value in {'AGENT_TASK_MAX_REQUESTS':'8','AGENT_TASK_MAX_TOKENS':'128000',
        'AGENT_MODEL_MAX_OUTPUT_TOKENS':'512','AGENT_TASK_DEADLINE_SECONDS':'75','AGENT_MAX_ITERATIONS':'6'}.items():
        monkeypatch.setenv(key,value)
    import server as runner
    raw,create = (runner.create_ollama_client().with_options(timeout=15,max_retries=0),None) if live else controlled_provider(case)
    actual_tool = runner.run_tool_with_context
    fetches = []
    def tool(call,**kwargs):
        name,arguments = call.function.name,json.loads(call.function.arguments)
        if name=='fetch_url':
            fetches.append(1)
            if not live:
                assert arguments=={'url':URL}
                return EVIDENCE
        return actual_tool(call,**kwargs)

    async def exercise():
        client = await server.client()
        store = Store()
        owner = task_owner(current_context())
        messages = [{'role':'user','content':GOALS[case]}]
        fallback = Mock(side_effect=AssertionError('No legacy fallback after Temporal admission'))
        with ThreadPoolExecutor(max_workers=2) as executor,patch('server.get_client',return_value=raw),patch('server.run_tool_with_context',side_effect=tool):
            async with Worker(client,task_queue=store.queue_for('steps'),workflows=[AgentTaskWorkflowV1],
                              activities=ACTIVITIES,activity_executor=executor):
                reply = await asyncio.wait_for(asyncio.to_thread(durable_invoke,fallback,messages,None),90)
            row = store.for_task(owner,'integration-task')
            task = store.tasks.get(owner,'integration-task')
            budget = TaskBudget(owner,'integration-task').snapshot()
            report = {'case':case,'provider':'live' if live else 'controlled','state':row['state'],
                      'requests':budget['requests'],'fetches':len(fetches),'wiki_created':(configured/'llm-wiki').exists()}
            print('TRIAL_RESULT '+json.dumps(report),flush=True)
            assert row['state'] in {'completed','failed','cancelled','uncertain'},report
            assert 1 <= budget['requests'] <= 8,report
            assert not (configured/'llm-wiki').exists(),report
            fallback.assert_not_called()
            if case=='router':
                assert row['state']=='completed' and 'UHP' in reply,report
                assert len(fetches)==1,report
                assert any(item.get('role')=='tool' and 'UHP' in str(item.get('content')) for item in messages)
            else:
                assert row['state']!='completed',report
                assert task['state']!='completed',report
                if not live:
                    with closing(store.tasks.connect()) as conn:
                        assert conn.execute("SELECT count(*) FROM operation_ledger WHERE kind='tool'").fetchone()[0]==0
                    assert budget['requests']==1,report
            if not live:
                assert create.call_count==budget['requests']
            history = await client.get_workflow_handle(row['workflow_id']).fetch_history()
            assert GOALS[case] not in history.to_json()
            assert EVIDENCE not in history.to_json()
            await Replayer(workflows=[AgentTaskWorkflowV1]).replay_workflow(history)
            # A fresh Worker and stopped admission cannot rerun completed work.
            monkeypatch.setenv('AGENT_TEMPORAL_TRIAL_OWNERS','[]')
            with patch('server.get_client',side_effect=AssertionError('Recovered result made provider I/O')):
                async with Worker(client,task_queue=store.queue_for('steps'),workflows=[AgentTaskWorkflowV1],
                                  activities=ACTIVITIES,activity_executor=executor):
                    if row['state'] in {'uncertain','cancelled'}:
                        # Existing policy failures stop conservatively; manual recovery is forbidden.
                        with pytest.raises(ValueError,match='manual resume'):
                            await asyncio.to_thread(durable_invoke,fallback,messages,None,True)
                    else:
                        again = await asyncio.wait_for(asyncio.to_thread(durable_invoke,fallback,messages,None,True),10)
                        assert again==reply
            after = TaskBudget(owner,'integration-task').snapshot()
            assert (after['requests'],after['deadline'])==(budget['requests'],budget['deadline'])
            assert len(fetches)==report['fetches']
            assert store.for_task(owner,'integration-task')['id']==row['id']
    try:
        asyncio.run(exercise())
    finally:
        if live:
            raw.close()
