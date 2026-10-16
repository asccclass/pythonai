import json
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pytest
from openai.types.chat import ChatCompletionMessage

from agent_execution import execution_scope
from agent_execution.turn_control import environment as env


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_EXECUTION_MODE', 'host')
    monkeypatch.setenv('AGENT_TASK_DB', str(tmp_path / 'tasks.db'))
    monkeypatch.setenv('AGENT_TENANT_ROOT', str(tmp_path / 'tenants'))
    monkeypatch.setenv('AGENT_TRACE_ENABLED', '0')
    env.host_environment.cache_clear()
    with execution_scope(workspace=tmp_path, identity='local', source='local', task_id=None):
        yield
    env.host_environment.cache_clear()


def facts(context):
    return json.loads(context.splitlines()[1])


@pytest.mark.parametrize('system', ['Windows', 'Linux', 'Darwin', 'Unknown'])
def test_detected_os_is_supplied_without_executing_commands(system):
    with patch.object(env.platform, 'system', return_value=system), patch.object(env.shutil, 'which', return_value=None):
        context = env.environment_context()
    assert facts(context)['host']['os'] == system
    assert facts(context)['run_command']['os'] == system
    assert facts(context)['host']['available_host_commands'] == []
    assert 'never implicitly starts a shell' in context


def test_host_detection_is_cached_and_reports_only_command_names(monkeypatch):
    monkeypatch.setenv('ENV_TEST_SECRET', 'never-report-this')
    with patch.object(env.platform, 'system', return_value='Windows') as system, patch.object(env.shutil, 'which', side_effect=lambda name: 'C:/private/python.exe' if name == 'python' else None):
        first = env.host_environment()
        assert env.host_environment() == first
    system.assert_called_once()
    assert first['available_host_commands'] == ['python']
    assert 'private' not in json.dumps(first)
    assert 'never-report-this' not in json.dumps(first)


def test_container_separates_host_tools_from_linux_target(monkeypatch):
    from sandbox import SandboxExecution
    monkeypatch.setenv('AGENT_EXECUTION_MODE', 'container')
    with patch.object(env.platform, 'system', return_value='Windows'):
        context = env.environment_context()
    data = facts(context)
    assert data['host']['os'] == 'Windows'
    assert data['run_command']['os'] == 'Linux'
    assert data['run_command']['image'] == SandboxExecution.IMAGE
    assert 'File/directory tools, fetch_url and run_test_suite execute on the backend host' in context
    assert 'host fallback' in context


def test_refresh_replaces_checkpoint_environment_preserving_profile_and_user(tmp_path):
    stale = env.START + '\nold-host\n' + env.END
    user = {'role': 'user', 'content': 'My computer is Linux. ' + stale}
    messages = [{'role': 'system', 'content': 'Follow profile principles. ' + stale}, user]
    with patch.object(env.platform, 'system', return_value='Windows'):
        assert env.refresh_environment(messages) is False
        original = messages[0]['content']
        assert env.refresh_environment(messages) is False
    assert messages[0]['content'] == original
    assert 'Follow profile principles.' in original
    assert original.count(env.START) == 1
    assert facts(original[original.index(env.START):])['host']['os'] == 'Windows'
    assert messages[1] == user
    nested = tmp_path / 'worktree'
    nested.mkdir()
    with execution_scope(workspace=nested):
        env.refresh_environment(messages)
    context = messages[0]['content'][messages[0]['content'].index(env.START):]
    assert facts(context)['host_workspace'] == str(nested)


@pytest.mark.parametrize('system,mode,command,installed,blocked', [
    ('Windows', 'host', 'mkdir', False, True),
    ('Windows', 'host', 'ls', False, True),
    ('Windows', 'host', 'ls', True, False),
    ('Linux', 'host', 'mkdir', True, False),
    ('Darwin', 'host', 'cmd.exe', False, True),
    ('Linux', 'host', 'pwsh', True, False),
    ('Windows', 'container', 'mkdir', False, False),
    ('Windows', 'container', 'powershell.exe', True, True),
])
def test_command_target_controls_compatibility(system, mode, command, installed, blocked, monkeypatch):
    monkeypatch.setenv('AGENT_EXECUTION_MODE', mode)
    with patch.object(env.platform, 'system', return_value=system), patch.object(env.shutil, 'which', return_value='/installed' if installed else None):
        assert bool(env.command_environment_error([command])) == blocked


def test_incompatible_command_never_reaches_process_runner():
    import base
    with patch.object(env.platform, 'system', return_value='Windows'), patch.object(env.shutil, 'which', return_value=None), patch('base.run_process') as process:
        result = base.run_command(['ls'])
    assert result.returncode == 1
    assert result.side_effects == {'status': 'none'}
    assert result.evidence == {'kind': 'environment_mismatch'}
    assert 'Windows' in result.stderr
    process.assert_not_called()


@pytest.mark.parametrize('with_system', [False, True])
def test_agent_model_receives_current_environment_and_preserves_goal_checkpoint(with_system):
    import server
    messages = ([{'role': 'system', 'content': 'policy'}] if with_system else []) + [{'role': 'user', 'content': 'original task'}]
    response = NS(choices=[NS(message=ChatCompletionMessage(role='assistant', content='done'))])
    create = Mock(return_value=response)
    raw = NS(chat=NS(completions=NS(create=create)))
    with patch.object(env.platform, 'system', return_value='Windows'), patch('server.get_client', return_value=raw), patch('agent_execution.tasks.save_checkpoint') as save:
        assert server.run_agent(messages, max_iterations=2, resume_state={'evaluation_start': int(with_system)}) == 'done'
    sent = create.call_args.kwargs['messages']
    assert sent[0]['role'] == 'system'
    assert 'Windows' in sent[0]['content']
    assert sum(str(m.get('content', '')).count(env.START) for m in sent) == 1
    assert save.call_args.kwargs['evaluation_start'] == 1
    assert messages[1]['content'] == 'original task'


def test_system_prompt_contains_environment_alongside_agents(tmp_path):
    import server
    path = tmp_path / 'AGENTS.md'
    path.write_text('Project instructions', encoding='utf-8')
    prompt = server.build_system_prompt('base policy', agents_path=path)
    assert 'base policy' in prompt and 'Project instructions' in prompt
    assert env.START in prompt


def test_streaming_agent_receives_environment():
    import server
    chunk = NS(choices=[NS(index=0, delta=NS(content='done', tool_calls=None), finish_reason='stop')])

    class Stream:
        def __iter__(self):
            return iter([chunk])

        def close(self):
            pass

    create = Mock(return_value=Stream())
    raw = NS(chat=NS(completions=NS(create=create)))
    with patch('server.get_client', return_value=raw):
        assert server.run_agent([{'role': 'user', 'content': 'task'}], max_iterations=2,
                                event_callback=lambda kind, payload: None) == 'done'
    assert env.START in create.call_args.kwargs['messages'][0]['content']
    assert create.call_args.kwargs['stream'] is True
