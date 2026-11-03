from contextlib import closing
from copy import deepcopy
import json
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pytest
from openai.types.chat import ChatCompletionMessage

from agent_execution.policy import execution_scope, current_context
from agent_execution.tasks import TaskStore, task_owner, dispatch_task_input
from agent_execution.budgets import TaskBudget


PLAN = '# Go 音軌轉錄專案\n\n- yt-dlp 音軌下載\n- STT\n- TXT/JSON\n'
GOAL = '請將此規劃先寫入專案目錄中的 TODO.md中'


@pytest.fixture
def setup(tmp_path,monkeypatch):
    for name,value in {'AGENT_TASK_DB':str(tmp_path/'tasks.db'),
        'AGENT_TENANT_ROOT':str(tmp_path/'tenants'),'AGENT_TRACE_ENABLED':'0',
        'AGENT_TASK_MAX_TOKENS':'1000000','AGENT_TASK_DEADLINE_SECONDS':'600',
        'AGENT_MODEL_MAX_OUTPUT_TOKENS':'1024','AGENT_MODEL_CAPABILITIES':'{}',
        'AGENT_MODEL_RATES':'{}'}.items():monkeypatch.setenv(name,value)
    with execution_scope(source='websocket',identity='websocket:alice',session='default:one',
                         conversation='one',workspace=tmp_path,task_id='new-request'):
        yield TaskStore(),task_owner(current_context()),tmp_path


def wait(store,owner,expired=True):
    messages = [{'role':'system','content':'outdated system policy'},
        {'role':'user','content':'請規劃 Go 音軌 STT 專案'}, {'role':'assistant','content':PLAN},
        {'role':'user','content':GOAL},
        {'role':'assistant','tool_calls':[{'id':'ask','type':'function','function':{
            'name':'ask_user','arguments':'{"question":"哪個目錄？"}'}}]}]
    token = store.claim(owner,'old',GOAL,messages,episode=7)
    store.checkpoint(owner,'old',token,'waiting_user',{'messages':messages,'suspended_tool_call_id':'ask'})
    store.finish(owner,'old',token,'suspended','哪個目錄？')
    budget = TaskBudget(owner,'old');budget.reserve('m',100,10)
    if expired:
        with closing(store.connect()) as conn,conn:
            conn.execute('UPDATE task_budgets SET deadline=? WHERE owner=? AND id=?',(time.time()-30,owner,'old'))
    return budget.snapshot()


def test_expired_wait_does_not_consume_new_message_or_reset_old_budget(setup):
    store,owner,_ = setup
    before = wait(store,owner)
    runtime = NS(messages=[{'role':'system','content':'current policy'}])
    with patch('agent_runtime.resume_agent_turn') as resume:
        assert dispatch_task_input(GOAL,runtime) is None
    resume.assert_not_called()
    assert store.get(owner,'old')['state']=='cancelled'
    assert TaskBudget(owner,'old').snapshot()==before
    assert {'role':'assistant','content':PLAN} in runtime.messages
    assert runtime.messages[0]['content']=='current policy'
    assert not any(message.get('tool_calls') for message in runtime.messages)
    assert not any(message.get('role')=='tool' for message in runtime.messages)
    assert not any(message.get('content')=='outdated system policy' for message in runtime.messages)
    with pytest.raises(KeyError):store.get(owner,'new-request')


def test_valid_wait_still_consumes_the_answer_under_the_old_task(setup):
    store,owner,_ = setup
    wait(store,owner,expired=False)
    with patch('agent_runtime.resume_agent_turn',return_value='resumed') as resume:
        assert dispatch_task_input('channel_audio_stt/',NS(messages=[]))=='resumed'
    assert resume.call_args.args[0][-1]=={'role':'tool','tool_call_id':'ask','content':'channel_audio_stt/'}
    assert store.get(owner,'old')['state']=='suspended'  # mocked runner did not mutate it


@pytest.mark.parametrize('incoming',[None,'old'])
def test_same_task_or_missing_request_id_cannot_mint_a_fresh_budget(setup,incoming):
    store,owner,_ = setup;before=wait(store,owner)
    with execution_scope(task_id=incoming),patch('agent_runtime.resume_agent_turn') as resume:
        result=dispatch_task_input(GOAL,NS(messages=[]))
    assert '新訊息' in result.reply
    resume.assert_not_called()
    assert store.get(owner,'old')['state']=='suspended'
    assert TaskBudget(owner,'old').snapshot()==before


def test_explicit_resume_reports_expired_deadline_without_consuming_answer(setup):
    store,owner,_ = setup;before=wait(store,owner)
    with patch('agent_runtime.resume_agent_turn') as resume:
        result=dispatch_task_input('/resume old',NS(messages=[]))
    assert '等待回答的期限' in result.reply
    resume.assert_not_called()
    assert store.get(owner,'old')['checkpoint']['suspended_tool_call_id']=='ask'
    assert TaskBudget(owner,'old').snapshot()==before


def test_replayed_new_id_cannot_reopen_or_replace_an_expired_wait(setup):
    store,owner,_=setup;before=wait(store,owner)
    token=store.claim(owner,'new-request','already accepted',[])
    store.finish(owner,'new-request',token,'failed','already handled')
    result=dispatch_task_input(GOAL,NS(messages=[]))
    assert '新訊息' in result.reply
    assert store.get(owner,'old')['state']=='suspended'
    assert TaskBudget(owner,'old').snapshot()==before


def test_clarifying_project_path_retains_the_goal_between_plan_and_new_message():
    from agent_execution.context import govern_messages
    messages=[{'role':'system','content':'rules'},
        {'role':'user','content':'obsolete '*1000}, {'role':'assistant','content':'obsolete '*1000},
        {'role':'user','content':'Plan a Go project'}, {'role':'assistant','content':PLAN},
        {'role':'user','content':GOAL}, {'role':'user','content':'channel_audio_stt/'}]
    fitted=govern_messages(messages,2000,goal='channel_audio_stt/')
    assert {'role':'assistant','content':PLAN} in fitted
    assert {'role':'user','content':GOAL} in fitted
    assert messages[1] not in fitted


def test_deadline_error_does_not_replace_the_plan_as_the_next_turn_reference():
    from agent_execution.context import govern_messages,fit_tool_schemas,message_tokens,count_tokens
    messages=[{'role':'system','content':'rules'}, {'role':'user','content':'Plan this project'},
        {'role':'assistant','content':PLAN}, {'role':'user','content':GOAL},
        {'role':'assistant','content':'Error: Task deadline exhausted'},
        {'role':'user','content':GOAL}]
    tools=[{'function':{'name':'write_file','description':'tool schema '*100}}]
    limit=message_tokens(messages[:4]+messages[-1:])+count_tokens([])+600
    selected=fit_tool_schemas(messages,limit,tools,goal=GOAL)
    result=govern_messages(messages,limit,selected,goal=GOAL)
    assert {'role':'assistant','content':PLAN} in result


def completed(store,owner,key='plan'):
    history=[{'role':'system','content':'old policy'}, {'role':'user','content':'Plan'},
             {'role':'assistant','content':PLAN}]
    token=store.claim(owner,key,'Plan',history)
    store.checkpoint(owner,key,token,'model_received',{'messages':history})
    store.finish(owner,key,token,'completed',PLAN)


def test_restart_restores_only_current_owner_completed_dialogue_and_clear_is_durable(setup):
    store,owner,_ = setup
    completed(store,owner)
    runtime=NS(messages=[])
    assert dispatch_task_input(GOAL,runtime) is None
    assert {'role':'assistant','content':PLAN} in runtime.messages
    with execution_scope(identity='websocket:bob'):
        other=NS(messages=[])
        assert dispatch_task_input(GOAL,other) is None
        assert other.messages==[]
    dispatch_task_input('/clear',runtime)
    restarted=NS(messages=[])
    assert dispatch_task_input(GOAL,restarted) is None
    assert restarted.messages==[]


def test_live_lease_or_native_workflow_cannot_be_replaced_automatically(setup):
    store,owner,_=setup;wait(store,owner)
    with closing(store.connect()) as conn,conn:
        conn.execute('UPDATE tasks SET lease_until=? WHERE owner=? AND id=?',(time.time()+60,owner,'old'))
    result=dispatch_task_input(GOAL,NS(messages=[]))
    assert '執行程序' in result.reply
    assert store.get(owner,'old')['state']=='suspended'
    with closing(store.connect()) as conn,conn:
        conn.execute('UPDATE tasks SET lease_until=NULL WHERE owner=? AND id=?',(owner,'old'))
        conn.execute('''CREATE TABLE IF NOT EXISTS execution_bindings (
            owner TEXT,task_id TEXT,backend TEXT,PRIMARY KEY(owner,task_id))''')
        conn.execute("INSERT INTO execution_bindings(owner,task_id,backend) VALUES(?,?,'temporal')",(owner,'old'))
    result=dispatch_task_input(GOAL,NS(messages=[]))
    assert '原生工作流程' in result.reply
    assert store.get(owner,'old')['state']=='suspended'


def test_expired_wait_restart_then_real_file_write_uses_new_budget_and_original_plan(setup):
    import server
    from agent_runtime import AgentRuntime,run_agent_turn
    from permissions import set_current_role
    store,owner,root=setup;before=wait(store,owner)
    seen=[]
    def provider(**kwargs):
        seen.append(deepcopy(kwargs['messages']))
        assert {'role':'assistant','content':PLAN} in kwargs['messages']
        if len(seen)==1:
            message=ChatCompletionMessage(role='assistant',content=None,tool_calls=[{
                'id':'save','type':'function','function':{'name':'write_file','arguments':json.dumps({
                    'path':'channel_audio_stt/TODO.md','content':PLAN},ensure_ascii=False)}}])
        else:message=ChatCompletionMessage(role='assistant',content='已寫入 TODO.md')
        return NS(choices=[NS(message=message)],usage=None)
    runtime=AgentRuntime(messages=[],guard=NS(assess=lambda text:NS(available=False,reason='offline')),
        memory=None,memory_searcher=None,skill_matcher=NS(match=lambda text:[]),memory_worker=None,
        memory_classifier_factory=None,semantic_extractor=None,procedure_matcher=None,run_agent=server.run_agent)
    client=NS(chat=NS(completions=NS(create=Mock(side_effect=provider))))
    assert dispatch_task_input(GOAL,runtime) is None
    with set_current_role('admin'),patch('server.get_client',return_value=client):
        assert run_agent_turn(GOAL,runtime).reply=='已寫入 TODO.md'
    assert (root/'channel_audio_stt/TODO.md').read_text(encoding='utf-8')==PLAN
    assert TaskBudget(owner,'old').snapshot()==before
    assert store.get(owner,'old')['state']=='cancelled'
    assert store.get(owner,'new-request')['state']=='completed'
    assert TaskBudget(owner,'new-request').snapshot()['deadline']>time.time()
