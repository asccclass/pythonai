"""Strict chat templates must receive one leading system message on every path."""
import copy
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from openai.types.chat import ChatCompletionMessage

from agent_execution.context import provider_messages
from agent_execution.policy import execution_scope
from agent_execution.turn_control.steps import model_step


def conversation():
    return [
        {'role': 'system', 'content': 'Main instructions'},
        {'role': 'system', 'content': 'Current profile'},
        {'role': 'user', 'content': 'Read note'},
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'read', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'read', 'content': 'note contents'},
        {'role': 'system', 'content': 'Memory summary'},
        {'role': 'user', 'content': 'Continue'},
    ]


def test_preserves_system_content_and_tool_history_without_mutating_checkpoint():
    messages = conversation()
    before = copy.deepcopy(messages)
    result = provider_messages(messages)
    assert result[0] == {'role': 'system', 'content': 'Main instructions\n\nCurrent profile\n\nMemory summary'}
    assert result[1:] == [m for m in messages if m['role'] != 'system']
    assert messages == before
    assert provider_messages(result) == result


@pytest.mark.parametrize('messages', [[], [{'role': 'user', 'content': 'Hi'}],
    [{'role': 'system', 'content': 'Instructions'}, {'role': 'user', 'content': 'Hi'}]])
def test_already_compatible_messages_are_unchanged(messages):
    assert provider_messages(messages) is messages


def test_moves_a_late_system_and_preserves_text_parts():
    messages = [{'role': 'user', 'content': 'Hi'},
                {'role': 'system', 'content': [{'type': 'text', 'text': 'Profile'}]},
                {'role': 'system', 'content': 'Memory'}]
    before = copy.deepcopy(messages)
    result = provider_messages(messages)
    assert result[0]['content'] == [{'type': 'text', 'text': 'Profile'},
                                   {'type': 'text', 'text': '\n\n'},
                                   {'type': 'text', 'text': 'Memory'}]
    assert result[1:] == [messages[0]]
    result[0]['content'][0]['text'] = 'Changed'
    assert messages == before


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('budgeted', [False, True])
def test_model_request_is_compatible_with_and_without_budget(tmp_path, monkeypatch, streaming, budgeted):
    from contextlib import nullcontext
    from agent_execution.budgets import budget_scope
    from agent_execution import context
    monkeypatch.setenv('AGENT_TASK_DB', str(tmp_path / 'tasks.db'))
    monkeypatch.setenv('AGENT_TRACE_ENABLED', '0')
    monkeypatch.setenv('AGENT_TASK_MAX_COST_USD', '0')
    monkeypatch.setenv('AGENT_MODEL_CONTEXT_TOKENS', '32768')
    monkeypatch.setenv('AGENT_MODEL_MAX_OUTPUT_TOKENS', '100')
    if budgeted:
        govern = context.govern_messages
        def add_summary(*args, **kwargs):
            return [*govern(*args, **kwargs), {'role': 'system', 'content': 'Budget summary'}]
        monkeypatch.setattr(context, 'govern_messages', add_summary)

    class Stream:
        def __iter__(self):
            yield NS(choices=[NS(index=0, delta=NS(content='Hello', tool_calls=None),
                                 finish_reason='stop')], usage=None)

        def close(self):
            pass

    def create(**kwargs):
        sent = kwargs['messages']
        assert [i for i, m in enumerate(sent) if m['role'] == 'system'] == [0]
        assert 'Current profile' in sent[0]['content']
        assert sent[1:] == [m for m in messages if m['role'] != 'system']
        if budgeted:
            assert 'Budget summary' in sent[0]['content']
        return Stream() if streaming else NS(choices=[NS(message=ChatCompletionMessage(role='assistant', content='Hello'))], usage=None)

    endpoint = Mock(side_effect=create)
    client = NS(chat=NS(completions=NS(create=endpoint)))
    messages = conversation()
    before = copy.deepcopy(messages)
    with execution_scope(workspace=tmp_path, source='local', identity='local', task_id='strict-template'):
        with budget_scope() if budgeted else nullcontext():
            result = model_step(client, {'model': 'fixture', 'tools': []}, messages,
                                iteration=1, event_callback=Mock() if streaming else None)
    assert result.content == 'Hello'
    endpoint.assert_called_once()
    assert messages == before
