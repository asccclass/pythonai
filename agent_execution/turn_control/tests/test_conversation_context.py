"""Follow-up references survive tool admission and later model/tool rounds."""
from copy import deepcopy
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pytest
from openai.types.chat import ChatCompletionMessage

from agent_execution.context import govern_messages, fit_tool_schemas, message_tokens, count_tokens, ContextTooLarge
from agent_execution.policy import execution_scope


def test_tool_schemas_yield_to_the_previous_plan_instead_of_erasing_it():
    plan = '## Go 音軌轉錄專案\n' + '- downloader: yt-dlp；STT；TXT/JSON 輸出\n'*30
    goal = '請將此規劃先寫入專案目錄中的 TODO.md中'
    messages = [
        {'role':'system','content':'trusted instructions'},
        {'role':'user','content':'unrelated obsolete history '*1000},
        {'role':'assistant','content':'unrelated obsolete answer '*1000},
        {'role':'user','content':'請規劃 Go 音軌轉錄專案'},
        {'role':'assistant','content':plan},
        {'role':'user','content':goal},
    ]
    original = deepcopy(messages)
    tools = [{'type':'function','function':{'name':name,'description':'tool description '*100}}
             for name in ('write_file','read_file','ask_user','fetch_url','search_web')]
    limit = message_tokens(messages[3:])+count_tokens(tools[:1])+1000
    selected = fit_tool_schemas(messages,limit,tools,goal=goal)
    result = govern_messages(messages,limit,selected,goal=goal)
    assert {'role':'assistant','content':plan} in result
    assert messages[3] in result and messages[5] in result
    assert messages[1] not in result and messages[2] not in result
    assert message_tokens(result)+count_tokens(selected)<=limit
    assert len(selected)<len(tools)
    assert messages==original


def test_reference_stays_protected_after_tool_results_and_critic_feedback():
    plan = 'Create channel_audio_stt/TODO.md with the Go downloader and STT plan.'
    goal = '請將此規劃寫入 TODO.md'
    call = {'id':'list','type':'function','function':{'name':'list_files','arguments':'{}'}}
    messages = [
        {'role':'system','content':'rules'},
        {'role':'user','content':'請規劃新專案'}, {'role':'assistant','content':plan},
        {'role':'user','content':goal},
        {'role':'assistant','tool_calls':[call]},
        {'role':'tool','tool_call_id':'list','content':'test_project (unrelated)'},
        {'role':'assistant','content':'可省略的中間回覆 '*1000},
        {'role':'user','content':'【任務驗收未通過】請完成寫檔。'},
    ]
    # The current task boundary is the actual goal, not the critic's user role.
    limit = 2000
    result = govern_messages(messages,limit,goal=goal)
    assert messages[2] in result and messages[3] in result
    assert messages[5] in result
    assert messages[6] not in result
    with pytest.raises(ContextTooLarge,match='previous conversation exchange'):
        govern_messages(messages,300,goal=goal)


def test_oversize_prior_answer_is_not_silently_replaced_with_no_context():
    messages = [{'role':'system','content':'rules'}, {'role':'user','content':'Plan'},
                {'role':'assistant','content':'complete plan '*1000},
                {'role':'user','content':'Save this plan.'}]
    with pytest.raises(ContextTooLarge,match='previous conversation exchange'):
        fit_tool_schemas(messages,2000,[{'function':{'name':'write_file'}}],goal='Save this plan.')


@pytest.mark.parametrize('streaming',[False,True])
def test_two_turn_plan_then_todo_write_uses_the_complete_prior_reply(tmp_path,monkeypatch,streaming):
    import server
    from agent_runtime import AgentRuntime, run_agent_turn
    from permissions import set_current_role

    for name,value in {
        'AGENT_TASK_DB':str(tmp_path/'tasks.db'), 'AGENT_TENANT_ROOT':str(tmp_path/'tenants'),
        'AGENT_TRACE_ENABLED':'0', 'AGENT_MODEL_CONTEXT_TOKENS':'24000',
        'AGENT_MODEL_MAX_OUTPUT_TOKENS':'1024', 'AGENT_TASK_MAX_TOKENS':'1000000',
        'AGENT_MODEL_CAPABILITIES':'{}', 'AGENT_MODEL_RATES':'{}',
    }.items():monkeypatch.setenv(name,value)
    plan = '# Go 頻道音軌轉錄專案\n\n專案目錄：channel_audio_stt/\n\n' + (
        '- [ ] downloader：使用 yt-dlp 下載公開影片音軌。\n'
        '- [ ] STT：呼叫轉錄服務，保存 TXT 與 JSON。\n'
        '- [ ] 加入錯誤重試、重複下載檢查與測試。\n')*25
    first = '使用 Go 規劃頻道音軌下載與 STT 專案'
    second = '請將此規劃先寫入專案目錄中的 TODO.md中'
    seen = []
    def provider(**kwargs):
        seen.append(deepcopy(kwargs['messages']))
        if len(seen)==1:
            message = ChatCompletionMessage(role='assistant',content=plan)
        elif len(seen)==2:
            assert {'role':'assistant','content':plan} in kwargs['messages']
            assert {'role':'user','content':first} in kwargs['messages']
            assert 'write_file' in [tool['function']['name'] for tool in kwargs['tools']]
            message = ChatCompletionMessage(role='assistant',content=None,tool_calls=[{
                'id':'save-plan','type':'function','function':{'name':'write_file','arguments':json.dumps({
                    'path':'channel_audio_stt/TODO.md','content':plan},ensure_ascii=False)}}])
        else:
            assert {'role':'assistant','content':plan} in kwargs['messages']
            assert any(m.get('role')=='tool' and m.get('tool_call_id')=='save-plan' for m in kwargs['messages'])
            message = ChatCompletionMessage(role='assistant',content='已寫入 channel_audio_stt/TODO.md')
        if not kwargs.get('stream'):
            return NS(choices=[NS(message=message)],usage=None)
        calls = [NS(index=index,id=call.id,function=call.function)
                 for index,call in enumerate(message.tool_calls or [])]
        chunk = NS(choices=[NS(index=0,delta=NS(content=message.content,tool_calls=calls),
                               finish_reason='tool_calls' if calls else 'stop')],usage=None)
        return LocalStream([chunk])

    class LocalStream:
        def __init__(self,chunks):self.chunks=chunks
        def __iter__(self):return iter(self.chunks)
        def close(self):pass

    raw = NS(chat=NS(completions=NS(create=Mock(side_effect=provider))))
    runtime = AgentRuntime(messages=[],guard=NS(assess=lambda text:NS(available=False,reason='offline')),
        memory=None,memory_searcher=None,skill_matcher=NS(match=lambda text:[]),memory_worker=None,
        memory_classifier_factory=None,semantic_extractor=None,procedure_matcher=None,
        run_agent=server.run_agent,event_callback=(lambda *args:None) if streaming else None)
    (tmp_path/'test_project').mkdir()
    (tmp_path/'test_project/keep.txt').write_text('unrelated')
    with execution_scope(source='websocket',identity='websocket:alice',session='default:one',
                         conversation='one',workspace=tmp_path), \
         set_current_role('admin'), patch('server.get_client',return_value=raw):
        with execution_scope(task_id='plan'):assert run_agent_turn(first,runtime).reply==plan
        with execution_scope(task_id='save'):assert '已寫入' in run_agent_turn(second,runtime).reply
    assert (tmp_path/'channel_audio_stt/TODO.md').read_text(encoding='utf-8')==plan
    assert not (tmp_path/'test_project/TODO.md').exists()
    assert (tmp_path/'test_project/keep.txt').read_text()=='unrelated'
    assert len(seen)==3
