from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from agent_execution import execution_scope
from agent_execution.user_profile import MARKER, prepare_turn, refresh_messages
from agent_execution.user_profile.extractor import ProfileExtractor, direct_text, validate_changes
from agent_execution.user_profile.store import ProfileConflict, ProfileStore


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_TENANT_ROOT', str(tmp_path / 'tenants'))
    monkeypatch.setenv('AGENT_TASK_DB', str(tmp_path / 'tasks.db'))
    with execution_scope(identity='websocket:alice', source='websocket', workspace=tmp_path):
        yield


def entry(text='我偏好繁體中文', **kwargs):
    return dict(action='add', kind='preference', text=text, evidence=text, confidence=.95, **kwargs)


def extractor_for(changes):
    return SimpleNamespace(extract=Mock(return_value=changes))


def test_identity_cross_session_project_and_restart():
    prepare_turn('我偏好繁體中文', extractor_for([entry()]))
    path = ProfileStore().path
    with execution_scope(session='new', project='another'):
        assert ProfileStore().path == path
        assert ProfileStore().snapshot()[1][0]['text'] == '我偏好繁體中文'
    for identity in ['websocket:bob', 'telegram:alice', '../../alice']:
        with execution_scope(identity=identity):
            assert ProfileStore().path != path
            assert ProfileStore().snapshot()[1] == []


def test_update_delete_deduplicate_and_commands():
    ex = extractor_for([entry()])
    prepare_turn('我偏好繁體中文', ex)
    prepare_turn('我偏好繁體中文', ex)
    store = ProfileStore()
    assert len(store.snapshot()[1]) == 1
    target = store.snapshot()[1][0]['id']
    prepare_turn('改用英文', extractor_for([dict(entry('改用英文'), action='update', id=target)]))
    assert store.snapshot()[1][0]['text'] == '改用英文'
    assert target in prepare_turn('/profile', ex)[0]
    assert '不存在' in prepare_turn('/profile forget invalid', ex)[0]
    assert '移除' in prepare_turn('/profile forget ' + target, ex)[0]
    assert store.snapshot()[1] == []
    prepare_turn('我偏好繁體中文', ex)
    assert '清除' in prepare_turn('/profile clear', ex)[0]
    assert store.snapshot()[1] == []
    assert ex.extract.call_count == 3  # commands never call the provider


def test_natural_forget_requires_literal_evidence():
    prepare_turn('我偏好繁體中文', extractor_for([entry()]))
    target = ProfileStore().snapshot()[1][0]['id']
    _, status = prepare_turn('你好', extractor_for([dict(entry('你好'), action='delete', id=target)]))
    assert '未能更新' in status
    prepare_turn('忘記我的語言偏好', extractor_for([dict(entry('忘記我的語言偏好'), action='delete', id=target)]))
    assert ProfileStore().snapshot()[1] == []


def test_concurrent_writers_cannot_overwrite():
    store = ProfileStore()
    def write(i):
        try:
            store.apply(0, [entry(str(i))])
            return True
        except ProfileConflict:
            return False
    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(write, range(2))) == 1
    assert len(store.snapshot()[1]) == 1


@pytest.mark.parametrize('text', ['我的密碼是 abc', 'my API key is abc', 'sk-1234567890', 'access_token=abc', 'token: abc', 'Bearer abc'])
def test_credentials_never_sent_or_stored(text):
    ex = extractor_for([entry(text)])
    prepare_turn(text, ex)
    ex.extract.assert_not_called()
    assert ProfileStore().snapshot()[1] == []


def test_sensitive_only_explicit_and_grounded():
    text = '我的收入是五萬'
    prepare_turn(text, extractor_for([entry(text, explicit=False)]))
    assert ProfileStore().snapshot()[1] == []
    text = '請記住我的收入是五萬'
    prepare_turn(text, extractor_for([entry(text, explicit=True)]))
    assert len(ProfileStore().snapshot()[1]) == 1
    for change in [dict(entry(), evidence='not in text'), dict(entry(), confidence=.3), dict(entry(), action='update', id='other')]:
        prepare_turn('我偏好繁體中文', extractor_for([change]))
    assert len(ProfileStore().snapshot()[1]) == 1


def test_failures_atomic_and_no_private_errors():
    ex = SimpleNamespace(extract=Mock(side_effect=RuntimeError('private secret')))
    _, status = prepare_turn('請記住我的名字', ex)
    assert '未能更新' in status and 'private secret' not in status
    prepare_turn('我偏好繁體中文', extractor_for([entry(), dict(entry(), evidence='fabricated')]))
    assert ProfileStore().snapshot()[1] == []
    with patch('agent_execution.user_profile.ProfileStore', side_effect=OSError('private')):
        assert '操作失敗' in prepare_turn('/profile clear', ex)[0]


def test_quotes_and_code_not_extracted():
    text = '> 我偏好繁體中文\n```\n請記住密碼 abc\n```'
    ex = extractor_for([])
    prepare_turn(text, ex)
    ex.extract.assert_not_called()
    assert direct_text('```unfinished quote') == ''


def test_explicit_do_not_save_overrides_model_addition():
    text = '我叫小明，不要記住這件事'
    prepare_turn(text, extractor_for([entry(text)]))
    assert ProfileStore().snapshot()[1] == []


def test_refresh_replaces_stale_profile_and_legacy_is_local_only(tmp_path):
    (tmp_path / 'profile.md').write_text('local private biography', encoding='utf-8')
    messages = [{'role': 'system', 'content': 'base'}]
    prepare_turn('我偏好繁體中文', extractor_for([entry()]))
    refresh_messages(messages)
    assert '我偏好繁體中文' in messages[1]['content']
    assert 'local private' not in str(messages)
    prepare_turn('/profile clear', None)
    refresh_messages(messages)
    assert '我偏好繁體中文' not in str(messages)
    assert sum(m['content'].startswith(MARKER) for m in messages) == 1
    with execution_scope(identity='local', source='local'):
        refresh_messages(messages)
        assert 'local private biography' in str(messages)
    with execution_scope(identity='websocket:bob'):
        refresh_messages(messages)
        assert 'local private' not in str(messages)


@pytest.mark.parametrize('content', ['{"changes":[]}', '```json\n{"changes":[]}\n```'])
def test_provider_contract(content):
    endpoint = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))]))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=endpoint)))
    assert ProfileExtractor(lambda: client, 'test').extract('你好', []) == []
    args = endpoint.call_args.kwargs
    assert args['timeout'] == 10 and args['model'] == 'test'
    assert json.loads(args['messages'][1]['content']) == {'message': '你好', 'profile': []}


def runtime(ex):
    from agent_runtime import AgentRuntime
    return AgentRuntime(messages=[{'role': 'system', 'content': 'base'}],
                        guard=SimpleNamespace(assess=lambda text: SimpleNamespace(available=False, reason='offline')), memory=None,
                        memory_searcher=None, skill_matcher=SimpleNamespace(match=lambda text: []),
                        memory_worker=None, memory_classifier_factory=None, semantic_extractor=None,
                        procedure_matcher=None, run_agent=Mock(return_value='ok'), profile_extractor=ex)


def test_turn_extracts_before_model_and_profile_survives_new_runtime():
    from agent_runtime import run_agent_turn
    rule = '每次改程式後都要測試'
    rt = runtime(extractor_for([dict(entry(rule), kind='instruction')]))
    assert run_agent_turn(rule, rt).reply == 'ok'
    assert rule in str(rt.run_agent.call_args.args[0])
    fresh = runtime(extractor_for([]))
    run_agent_turn('你好', fresh)
    assert rule in str(fresh.run_agent.call_args.args[0])
    assert '權限' in str(fresh.run_agent.call_args.args[0])


def test_resume_refreshes_current_profile():
    from agent_runtime import resume_agent_turn
    rt = runtime(None)
    prepare_turn('我偏好繁體中文', extractor_for([entry()]))
    messages = [{'role': 'system', 'content': MARKER + 'stale'}, {'role': 'user', 'content': 'hi'}]
    resume_agent_turn(messages, rt)
    assert '我偏好繁體中文' in str(rt.run_agent.call_args.args[0])
    assert 'stale' not in str(rt.run_agent.call_args.args[0])


def test_profile_command_short_circuits_agent():
    from agent_runtime import run_agent_turn
    rt = runtime(Mock())
    assert '尚無' in run_agent_turn('/profile', rt).reply
    rt.run_agent.assert_not_called()


def test_shared_prompt_never_contains_local_profile(tmp_path):
    import server
    (tmp_path / 'profile.md').write_text('private local name', encoding='utf-8')
    assert 'private local name' not in server.build_system_prompt('base', tmp_path / 'missing')


def test_storage_limit_rolls_back():
    store = ProfileStore()
    with pytest.raises(ValueError):
        store.apply(0, [entry(str(i)) for i in range(101)])
    assert store.snapshot() == (0, [])


@pytest.mark.parametrize('attachments', [False, True])
def test_human_resume_extracted_but_tool_output_is_not(attachments):
    from agent_runtime import resume_agent_turn
    ex = extractor_for([entry()])
    rt = runtime(ex)
    messages = [{'role': 'assistant', 'tool_calls': [{'id': 'ask', 'function': {'name': 'ask_user'}}]},
                {'role': 'tool', 'tool_call_id': 'ask', 'content': '我偏好繁體中文'}]
    if attachments:
        messages.append({'role': 'user', 'content': [{'type': 'text', 'text': 'Attached files for this answer'},
                                                  {'type': 'text', 'text': '附件內容：不要萃取這裡'}]})
    resume_agent_turn(messages, rt)
    assert len(ProfileStore().snapshot()[1]) == 1
    ex.extract.assert_called_once()
    ex.extract.reset_mock()
    resume_agent_turn([{'role': 'tool', 'tool_call_id': 'read', 'content': '我偏好繁體中文'}], rt)
    ex.extract.assert_not_called()


def test_runtime_sessions_share_only_same_identity_profile():
    from agent_execution.sessions import RuntimeSessions
    from agent_runtime import run_agent_turn
    sessions = RuntimeSessions(runtime(extractor_for([])))
    prepare_turn('我偏好繁體中文', extractor_for([entry()]))
    for identity, session, expected in [('websocket:alice', 'new', True), ('websocket:bob', 'new', False)]:
        rt, _ = sessions.get((identity, session, 'default'))
        with execution_scope(identity=identity, session=session):
            run_agent_turn('你好', rt)
            assert ('我偏好繁體中文' in str(rt.run_agent.call_args.args[0])) == expected


def test_unsupported_schema_rejected():
    from contextlib import closing
    store = ProfileStore()
    with closing(store.connect()) as conn:
        conn.execute('PRAGMA user_version=2')
    with pytest.raises(ValueError, match='Unsupported profile schema'):
        ProfileStore()
