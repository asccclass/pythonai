"""Regressions for the persisted ZAR lookup that spent its final judge request."""
import json
import subprocess
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pytest
from openai.types.chat import ChatCompletionMessage

from agent_execution import execution_scope
from agent_execution.budgets import budget_client, budget_scope
from agent_execution.results import ToolResult, normalize_result
from agent_execution.turn_control import TurnProgress
from agent_execution.turn_control.recovery import recovered_read_failure
from agent_execution.turn_control.web import readable_content
from task_evaluator import EvaluationResult


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_TASK_DB', str(tmp_path / 'tasks.db'))
    monkeypatch.setenv('AGENT_TRACE_ENABLED', '0')
    monkeypatch.setenv('AGENT_TASK_MAX_COST_USD', '0')
    monkeypatch.setenv('AGENT_TASK_MAX_TOKENS', '256000')
    monkeypatch.setenv('AGENT_MODEL_MAX_OUTPUT_TOKENS', '1024')
    monkeypatch.setenv('AGENT_MODEL_CAPABILITIES', '{}')
    with execution_scope(workspace=tmp_path, identity='local', source='local', task_id='lookup'):
        yield


@pytest.mark.parametrize('raw', [False, True])
def test_focused_json_returns_exact_rate_and_timestamp_even_in_raw_mode(raw):
    import base
    data = {'base_code': 'ZAR', 'time_last_update_utc': 'Tue, 03 Nov 2026 00:02:31 +0000',
            'rates': {'TWD': 1.958718, 'ZWL': 1.590584, 'USD': 0.060192}}
    completed = subprocess.CompletedProcess([], 0, json.dumps(data), '')
    with patch('base.run_process', return_value=completed):
        result = base.fetch_url('https://rates.test/ZAR', raw=raw, contains='"TWD"')
    assert '"TWD": 1.958718' in result.stdout
    assert '"ZWL"' not in result.stdout
    assert '"base_code": "ZAR"' in result.stdout
    assert data['time_last_update_utc'] in result.stdout
    assert normalize_result(result).side_effects == {'status': 'none'}


def test_json_without_focus_remains_complete_and_readable():
    data = {'rates': {'TWD': 1.958718, 'ZWL': 1.590584}}
    output = readable_content(json.dumps(data), 'https://rates.test')
    assert json.loads(output) == data
    assert '\n' in output


@pytest.mark.parametrize('name', ['fetch_url', 'browse_webpage'])
def test_failed_optional_source_does_not_invalidate_prior_process_evidence(name):
    source = {'name': 'fetch_url', 'state': 'completed', 'input': '{"url":"https://rates.test"}',
              'result': json.dumps(ToolResult(True, '1 ZAR = 1.958718 TWD', 0,
                                              evidence={'kind': 'process', 'returncode': 0}).to_dict())}
    failed = {'name': name, 'state': 'failed', 'input': '{"url":"https://other.test"}',
              'result': json.dumps(ToolResult(False, error='HTTP 404').to_dict())}
    assert recovered_read_failure(failed, [source, failed])
    assert not recovered_read_failure(failed, [failed])
    failed['result'] = json.dumps(ToolResult(False, error='PermissionError: denied').to_dict())
    assert not recovered_read_failure(failed, [source, failed])


def test_reused_reads_count_as_no_progress_across_checkpoint_and_changed_defaults():
    args = {'url': 'https://rates.test'}
    assert TurnProgress.key('fetch_url', args) == TurnProgress.key('fetch_url', {
        **args, 'raw': False, 'max_time': 20, 'follow_redirects': True, 'contains': ''})
    progress = TurnProgress()
    for _ in range(2):
        progress.observe('fetch_url', args, ToolResult(True, 'old evidence',
                         evidence={'kind': 'reuse_prior_read'}))
        progress = TurnProgress(json.loads(json.dumps(progress.state)))
    assert progress.state['read_reuses'] == 2
    progress.observe('fetch_url', {'url': 'https://other.test'}, ToolResult(False, error='HTTP 403'))
    assert progress.state['read_reuses'] == 0


def response(message):
    return NS(choices=[NS(message=message)], usage=NS(total_tokens=130, prompt_tokens=100, completion_tokens=30))


@pytest.mark.parametrize('limit', [6, 24])
def test_critic_retries_preserve_final_judge_headroom(monkeypatch, limit):
    import server
    monkeypatch.setenv('AGENT_TASK_MAX_REQUESTS', str(limit))
    judge_count = 0
    model_requests = []

    def create(**kwargs):
        if kwargs['model'] in {'judge', 'profile'}:
            return response(ChatCompletionMessage(role='assistant', content='judge'))
        model_requests.append(kwargs)
        if not kwargs.get('tools'):
            return response(ChatCompletionMessage(role='assistant', content='correct final rate'))
        # First two final answers fail validation; later model calls keep
        # choosing new sources until the live request reserve forces conclusion.
        if len(model_requests) <= 2:
            return response(ChatCompletionMessage(role='assistant', content='wrong rate'))
        return response(ChatCompletionMessage(role='assistant', tool_calls=[{
            'id': str(len(model_requests)), 'type': 'function', 'function': {
                'name': 'fetch_url', 'arguments': json.dumps({'url': f'https://rates.test/{len(model_requests)}'})}}]))

    raw = NS(chat=NS(completions=NS(create=Mock(side_effect=create))))
    def evaluate(**kwargs):
        nonlocal judge_count
        budget_client(raw).chat.completions.create(model='judge', messages=[])
        judge_count += 1
        return EvaluationResult(judge_count == 3, 1 if judge_count == 3 else 0,
                                'check the rate', verified=judge_count == 3)

    evaluator = Mock()
    evaluator.capture_state.return_value = {}
    evaluator.evaluate.side_effect = evaluate
    with budget_scope() as budget, patch('server.get_client', return_value=raw), \
         patch('server.run_tool_with_context', return_value=ToolResult(True, 'source data')):
        if limit == 24:
            # The incident had already spent one profile request and saved an
            # immutable iteration limit of 21 before its two critic retries.
            budget_client(raw).chat.completions.create(model='profile', messages=[])
        reply = server.run_agent([{'role': 'user', 'content': 'current ZAR to TWD rate'}],
                                 task_evaluator=evaluator,
                                 resume_state={'iteration_limit': 21} if limit == 24 else None)
        assert budget.snapshot()['requests'] <= limit
    assert judge_count == 3, (reply, len(model_requests), budget.snapshot())
    assert reply == 'correct final rate'
    assert not model_requests[-1].get('tools')


def test_failed_judge_does_not_start_correction_without_two_requests(monkeypatch):
    import server
    monkeypatch.setenv('AGENT_TASK_MAX_REQUESTS', '3')
    raw = NS(chat=NS(completions=NS(create=Mock(return_value=response(
        ChatCompletionMessage(role='assistant', content='candidate'))))))
    evaluator = Mock()
    evaluator.capture_state.return_value = {}
    def evaluate(**kwargs):
        budget_client(raw).chat.completions.create(model='judge', messages=[])
        return EvaluationResult(False, 0, 'wrong rate')
    evaluator.evaluate.side_effect = evaluate
    with budget_scope(), patch('server.get_client', return_value=raw):
        reply = server.run_agent([{'role': 'user', 'content': 'rate'}], task_evaluator=evaluator)
    assert 'HITL' in reply
    assert 'Task budget exhausted' not in reply
    evaluator.evaluate.assert_called_once()
    assert raw.chat.completions.create.call_count == 2


def test_provider_retries_cannot_spend_reserved_judge_request(monkeypatch):
    import httpx
    from openai import InternalServerError
    import server
    monkeypatch.setenv('AGENT_TASK_MAX_REQUESTS', '3')
    request = httpx.Request('POST', 'https://provider.test/chat/completions')
    raw = NS(chat=NS(completions=NS(create=Mock(side_effect=InternalServerError(
        'provider failed', response=httpx.Response(500, request=request), body={})))) )
    evaluator = Mock()
    evaluator.capture_state.return_value = {}
    with budget_scope() as budget, patch('server.get_client', return_value=raw):
        reply = server.run_agent([{'role': 'user', 'content': 'rate'}],
                                 task_evaluator=evaluator, sleep=lambda seconds: None)
        assert budget.snapshot()['requests'] == 2
    assert '不足以繼續重試並完成驗收' in reply
    assert '已達重試上限' not in reply
    assert 'Task budget exhausted' not in reply
    evaluator.evaluate.assert_not_called()


@pytest.mark.parametrize('passed', [False, True])
def test_recovered_lookup_still_requires_independent_answer_judge(tmp_path, passed):
    from agent_execution.tasks import _active
    from task_evaluator import TaskEvaluator
    source = {'name': 'fetch_url', 'id': 'source', 'state': 'completed',
              'input': '{"url":"https://rates.test"}',
              'result': json.dumps(ToolResult(True, '1 ZAR = 1.958718 TWD', 0,
                                              evidence={'kind': 'process', 'returncode': 0}).to_dict())}
    failed = {'name': 'fetch_url', 'id': 'optional', 'state': 'failed',
              'input': '{"url":"https://optional.test"}',
              'result': json.dumps(ToolResult(False, error='HTTP 404').to_dict())}
    store = Mock()
    store.get.return_value = {'criteria': {'version': 1, 'checks': []}}
    store.steps.return_value = [source, failed]
    evaluator = TaskEvaluator(tmp_path, git_status_fn=lambda: '', git_diff_fn=lambda **kwargs: '')
    token = _active.set((store, 'owner', 'lookup'))
    try:
        with patch.object(evaluator, 'evaluate_llm_judge', return_value=(passed, float(passed), 'wrong rate')):
            result = evaluator.evaluate('current ZAR rate', [], 'candidate')
    finally:
        _active.reset(token)
    assert result.success is passed
    assert result.verified is passed
    assert 'Tool step not successful' not in result.feedback
