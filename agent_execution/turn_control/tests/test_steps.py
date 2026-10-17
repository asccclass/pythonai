"""Behavior contracts shared by SQLite execution and Temporal Activities."""
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pytest

from agent_execution import execution_scope
from agent_execution.policy import current_context
from agent_execution.turn_control.steps import model_step, tool_step, evaluation_step


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_TASK_DB', str(tmp_path / 'tasks.db'))
    monkeypatch.setenv('AGENT_TENANT_ROOT', str(tmp_path / 'tenants'))
    monkeypatch.setenv('AGENT_TRACE_ENABLED', '0')
    with execution_scope(workspace=tmp_path, source='local', identity='local', task_id=None):
        yield


def test_single_attempt_does_not_retry_unknown_provider_outcome():
    create = Mock(side_effect=TimeoutError('unknown outcome'))
    client = NS(chat=NS(completions=NS(create=create)))
    with pytest.raises(TimeoutError):
        model_step(client, {'model': 'test', 'tools': []}, [], iteration=1)
    create.assert_called_once_with(model='test', messages=[])


def test_shared_model_failure_remains_charged_after_budget_reload(monkeypatch):
    from agent_execution.budgets import budget_scope, TaskBudget, BudgetExhausted
    from agent_execution.tasks import task_owner
    monkeypatch.setenv('AGENT_TASK_MAX_REQUESTS', '1')
    monkeypatch.setenv('AGENT_TASK_MAX_COST_USD', '0')
    create = Mock(side_effect=TimeoutError('unknown provider outcome'))
    client = NS(chat=NS(completions=NS(create=create)))
    with execution_scope(task_id='shared-model-budget'):
        owner = task_owner(current_context())
        with budget_scope():
            with pytest.raises(TimeoutError):
                model_step(client, {'model': 'test', 'tools': []}, [], iteration=1)
        restored = TaskBudget(owner, 'shared-model-budget')
        assert restored.snapshot()['requests'] == 1
        with budget_scope():
            with pytest.raises(BudgetExhausted):
                model_step(client, {'model': 'test', 'tools': []}, [], iteration=1)
    assert create.call_count == 1


def test_model_cancelled_before_request_is_not_sent():
    client = Mock()
    with pytest.raises(InterruptedError):
        model_step(client, {'model': 'test', 'tools': []}, [], iteration=1,
                   check_cancelled=lambda: True)
    client.chat.completions.create.assert_not_called()


def test_streaming_uses_same_callback_and_cancellation():
    client, callback, cancelled = Mock(), Mock(), Mock(return_value=False)
    messages, tools = [{'role': 'user', 'content': 'hello'}], [{'type': 'function'}]
    with patch('agent_execution.streaming.streamed_completion', return_value='response') as stream:
        assert model_step(client, {'model': 'test', 'tools': tools}, messages,
                          iteration=3, event_callback=callback, check_cancelled=cancelled) == 'response'
    callback.assert_called_once_with('model.start', {'iteration': 3, 'model': 'test'})
    stream.assert_called_once_with(client, 'test', messages, tools, callback, cancelled)


def test_tool_scope_preserves_identity_and_restores_on_failure():
    call = NS(function=NS(name='read_file'))
    cancelled = lambda: False
    before = current_context()

    def invoke(*args, **kwargs):
        context = current_context()
        assert context.identity == before.identity and context.workspace == before.workspace
        assert context.check_cancelled is cancelled and context.tool_hook_managed == 'read_file'
        raise RuntimeError('injected dispatcher failure')

    with pytest.raises(RuntimeError):
        tool_step(call, invoke=invoke, check_cancelled=cancelled)
    assert current_context() == before


def test_shared_tool_step_does_not_bypass_policy(tmp_path):
    from base import run_tool_with_context
    from openai.types.chat import ChatCompletionMessageToolCall
    (tmp_path / 'note.txt').write_text('private evidence', encoding='utf-8')
    call = ChatCompletionMessageToolCall(id='read', type='function', function={
        'name': 'read_file', 'arguments': '{"path":"note.txt"}'})
    with execution_scope(allowed_tools=frozenset()):
        result = tool_step(call, invoke=run_tool_with_context)
    assert 'outside this agent role' in str(result)
    assert 'private evidence' not in str(result)


def test_legacy_invalid_tool_arguments_remain_a_reported_tool_error():
    from base import run_tool_with_context
    call = NS(function=NS(name='read_file', arguments='{invalid json'))
    result = tool_step(call, invoke=run_tool_with_context)
    assert str(result).startswith('Error:')


def test_evaluation_uses_current_turn_and_captured_post_state():
    evaluator = Mock()
    evaluator.capture_state.return_value = {'files': ['actual']}
    expected = evaluator.evaluate.return_value
    messages = [{'role': 'user', 'content': 'old coding task'},
                {'role': 'user', 'content': 'current read task'}]
    assert evaluation_step(evaluator, user_goal='persisted original goal', messages=messages,
                           evaluation_start=1, final_reply=None, pre_state={'files': []}) is expected
    evaluator.evaluate.assert_called_once_with(user_goal='persisted original goal',
        messages=messages[1:], final_reply='', pre_state={'files': []}, post_state={'files': ['actual']})


def test_evaluation_capture_failure_does_not_invent_evidence():
    evaluator = Mock()
    evaluator.capture_state.side_effect = OSError('workspace unavailable')
    with pytest.raises(OSError):
        evaluation_step(evaluator, user_goal='read', messages=[], evaluation_start=0,
                        final_reply='claimed done', pre_state={})
    evaluator.evaluate.assert_not_called()


def test_legacy_runner_uses_all_shared_steps_with_real_read(tmp_path):
    import server
    from openai.types.chat import ChatCompletionMessage
    from task_evaluator import EvaluationResult
    from agent_execution.turn_control import steps

    (tmp_path / 'note.txt').write_text('read-through-shared-steps', encoding='utf-8')
    replies = [ChatCompletionMessage(role='assistant', tool_calls=[{
        'id': 'read-one', 'type': 'function', 'function': {
            'name': 'read_file', 'arguments': '{"path":"note.txt"}'}}]),
        ChatCompletionMessage(role='assistant', content='read-through-shared-steps')]
    create = Mock(side_effect=[NS(choices=[NS(message=message)]) for message in replies])
    client = NS(chat=NS(completions=NS(create=create)))
    evaluator = NS(capture_state=Mock(return_value={}),
                   evaluate=Mock(return_value=EvaluationResult(True, 1.0, 'ok')))
    messages = [{'role': 'user', 'content': 'Read note.txt and report its content.'}]
    with (patch('server.get_client', return_value=client),
          patch.object(steps, 'model_step', wraps=steps.model_step) as model,
          patch.object(steps, 'tool_step', wraps=steps.tool_step) as tool,
          patch.object(steps, 'evaluation_step', wraps=steps.evaluation_step) as evaluation):
        assert server.run_agent(messages, task_evaluator=evaluator) == 'read-through-shared-steps'
    assert model.call_count == 2 and tool.call_count == 1 and evaluation.call_count == 1
    assert any(m.get('role') == 'tool' and 'read-through-shared-steps' in m['content'] for m in messages)
    assert evaluator.evaluate.call_args.kwargs['user_goal'] == 'Read note.txt and report its content.'
