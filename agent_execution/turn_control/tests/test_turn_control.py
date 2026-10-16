import json
import subprocess
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pytest
from openai.types.chat import ChatCompletionMessage

from agent_execution import execution_scope
from agent_execution.results import ToolResult, normalize_result
from agent_execution.turn_control import TurnProgress, iteration_limit
from agent_execution.turn_control.web import fetch_target, readable_content
from agent_execution.turn_control.recovery import recovered_directory_failure


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_TASK_DB', str(tmp_path / 'tasks.db'))
    monkeypatch.setenv('AGENT_TENANT_ROOT', str(tmp_path / 'tenants'))
    monkeypatch.setenv('AGENT_TRACE_ENABLED', '0')
    with execution_scope(workspace=tmp_path, identity='local', source='local', task_id=None):
        yield


def call(name, args, key):
    return {'id': key, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}


def response(calls=None, content=None):
    return NS(choices=[NS(message=ChatCompletionMessage(role='assistant', content=content, tool_calls=calls))])


def client(responses):
    create = Mock(side_effect=responses)
    return NS(chat=NS(completions=NS(create=create))), create


def test_same_fetch_loop_is_bounded_and_final_has_no_tools():
    import server
    args = {'url': 'https://github.com/HarnessRouter/harnessrouter'}
    raw, create = client([response([call('fetch_url', args, str(i))]) for i in range(4)] + [response(content='已有 README 證據，說明原理。')])
    run = Mock(return_value='README evidence')
    with patch('server.get_client', return_value=raw), patch('server.run_tool_with_context', run):
        reply = server.run_agent([{'role': 'user', 'content': 'read repo'}])
    assert 'README' in reply
    assert run.call_count == 2
    assert 'tools' not in create.call_args.kwargs
    latest = next(m for m in reversed(create.call_args.kwargs['messages']) if m.get('role') == 'tool')
    assert 'README evidence' in latest['content'] and 'Reused earlier read' in latest['content']


def test_changing_mkdir_paths_stops_same_failure_family_then_allows_recovery():
    progress = TurnProgress()
    for path in ['a', 'b']:
        args = {'command': ['mkdir', path]}
        assert progress.before('run_command', args) is None
        progress.observe('run_command', args, ToolResult(False, error='shell builtin'))
    restored = TurnProgress(json.loads(json.dumps(progress.state)))
    assert restored.before('run_command', {'command': ['mkdir', 'c']})
    assert restored.before('create_directory', {'paths': ['a', 'b', 'c']}) is None


def test_read_after_edit_is_not_a_stale_duplicate():
    progress = TurnProgress()
    for _ in range(2):
        progress.observe('read_file', {'path': 'a'}, ToolResult(True, 'old'))
    assert progress.before('read_file', {'path':'a'}) is not None
    progress.observe('write_file', {'path': 'a', 'content': 'new'}, ToolResult(True, 'written'))
    assert progress.before('read_file', {'path': 'a'}) is None


def test_failure_can_change_arguments_to_repair():
    progress = TurnProgress()
    args = {'path': 'a', 'old_str': 'wrong', 'new_str': 'new'}
    for _ in range(2):
        progress.observe('edit_file', args, ToolResult(False, error='old_str not found'))
    assert progress.before('edit_file', dict(args, old_str='correct')) is None


def test_last_round_is_reserved_for_final_response():
    import server
    raw, create = client([response([call('list_files', {}, 'one')]), response(content='done')])
    with patch('server.get_client', return_value=raw), patch('server.run_tool_with_context', return_value='files'):
        assert server.run_agent([{'role':'user','content':'inspect'}], max_iterations=2) == 'done'
    assert 'tools' not in create.call_args.kwargs


def test_disobedient_final_tool_request_cannot_execute():
    import server
    raw, _ = client([response([call('write_file', {'path':'unwanted','content':'x'}, 'one')])])
    with patch('server.get_client', return_value=raw), patch('server.run_tool_with_context') as run:
        assert server.run_agent([{'role':'user','content':'inspect'}], max_iterations=1).startswith('Error:')
    run.assert_not_called()


def test_resume_restores_repeat_guard():
    import server
    progress = TurnProgress()
    args = {'url': 'https://example.test'}
    for _ in range(2):
        progress.observe('fetch_url', args, ToolResult(True, 'same'))
    raw, _ = client([response([call('fetch_url', args, 'three')]), response(content='done')])
    with patch('server.get_client', return_value=raw), patch('server.run_tool_with_context') as run:
        assert server.run_agent([{'role':'user','content':'inspect'}], resume_state={'iteration':2,'progress':progress.state}) == 'done'
    run.assert_not_called()


def test_limit_validated_and_checkpoint_limit_preserved(monkeypatch):
    monkeypatch.setenv('AGENT_MAX_ITERATIONS', '20')
    assert iteration_limit() == 20
    assert iteration_limit(saved=12) == 12
    assert iteration_limit(8, saved=12) == 8
    with pytest.raises(ValueError): iteration_limit(0)


def test_limit_reserves_model_request_headroom():
    budget = NS(max_requests=24, snapshot=lambda: {'requests': 1})
    with patch('agent_execution.budgets.active_budget', return_value=budget):
        assert iteration_limit() <= 21


def test_http_github_readme_and_raw_compatibility():
    import base
    url = 'https://github.com/HarnessRouter/harnessrouter'
    assert fetch_target(url) == 'https://api.github.com/repos/HarnessRouter/harnessrouter/readme'
    assert fetch_target(url + '/blob/main/a.py') == url + '/blob/main/a.py'
    result = subprocess.CompletedProcess([], 0, '# README\narchitecture', '')
    with patch('base.run_process', return_value=result) as run:
        text = base.fetch_url(url).stdout
        assert 'architecture' in text and 'Source:' in text
        assert 'Accept: application/vnd.github.raw+json' in run.call_args.args[0]
    with patch('base.run_process', return_value=result) as run:
        base.fetch_url(url, raw=True)
        assert run.call_args.args[0][-1] == url


def test_html_main_content_and_bound():
    html = '<!doctype html><html><body><nav>noise</nav><article><h1>Principle</h1><script>bad()</script><p>real content</p></article></body></html>'
    text = readable_content(html, 'https://example.test')
    assert 'real content' in text and 'noise' not in text and 'bad()' not in text
    assert 'truncated' in readable_content('a' * 25000, 'https://example.test')


def test_fetch_failure_and_non_http_not_success():
    import base
    with patch('base.run_process', return_value=subprocess.CompletedProcess([], 22, '', 'HTTP 404')):
        assert not normalize_result(base.fetch_url('https://example.test')).success
    with patch('base.run_process') as run:
        assert base.fetch_url('file:///private').returncode == 1
    run.assert_not_called()


def test_batch_directory_creation_and_permissions(tmp_path):
    from tools.create_directory import create_directory
    from permissions import set_current_role
    result = create_directory(['wiki/raw/pdfs', 'wiki/wiki/concepts', 'wiki/index'])
    assert result['success'] and len(result['directories']) == 3
    assert (tmp_path / 'wiki/raw/pdfs').is_dir()
    with pytest.raises(PermissionError): create_directory(['../outside'])
    with set_current_role('viewer'), pytest.raises(PermissionError): create_directory(['forbidden'])


def test_windows_mkdir_returns_actionable_preflight_error():
    import base
    with patch('base.platform.system', return_value='Windows'), patch('base.run_process') as run:
        process = base.run_command(['mkdir', 'a'])
        assert isinstance(process, subprocess.CompletedProcess) and process.returncode == 1
        result = normalize_result(process)
    assert not result.success and 'create_directory' in result.error
    assert result.side_effects['status'] == 'none'
    run.assert_not_called()


def test_edit_miss_is_failure_and_empty_configuration_is_valid(tmp_path):
    import base
    from task_evaluator import TaskEvaluator
    (tmp_path / 'a.txt').write_text('old')
    result = normalize_result(base.edit_file('a.txt', 'absent', 'new'))
    assert not result.success and 'read_file' in result.error
    (tmp_path / 'requirements.txt').touch()
    (tmp_path / '__init__.py').touch()
    assert TaskEvaluator(tmp_path).verify_file_syntax_and_state(['requirements.txt','__init__.py'])[0]
    (tmp_path / 'main.py').touch()
    assert not TaskEvaluator(tmp_path).verify_file_syntax_and_state(['main.py'])[0]


def test_only_verified_directory_recovery_can_clear_mkdir_failure(tmp_path):
    target = tmp_path / 'wiki/raw'
    target.mkdir(parents=True)
    step = {'name':'run_command','state':'failed','input':json.dumps({'command':['mkdir','raw'],'cwd':'wiki'}),
            'result':json.dumps(ToolResult(False, side_effects={'status':'none'}, evidence={'kind':'unsupported_shell_builtin'}).to_dict())}
    repaired = {'name':'create_directory','state':'completed','result':json.dumps(ToolResult(True,json.dumps({'directories':[str(target)]})).to_dict())}
    assert recovered_directory_failure(step, [step, repaired], tmp_path)
    assert not recovered_directory_failure(step, [repaired, step], tmp_path)
    step['result'] = json.dumps(ToolResult(False, side_effects={'status':'none'}, evidence={'kind':'repeat_prevented'}).to_dict())
    assert recovered_directory_failure(step, [step, repaired], tmp_path)
    assert not recovered_directory_failure(step, [step], tmp_path)
    step['result'] = json.dumps(ToolResult(False,error='PermissionError').to_dict())
    assert not recovered_directory_failure(step, [step, repaired], tmp_path)


def test_failed_edit_recovers_only_with_matching_file_evidence(tmp_path):
    from agent_execution.turn_control.recovery import recovered_edit_failure
    target = tmp_path / 'a.md'
    target.write_text('correct final')
    step = {'name':'edit_file','state':'failed','input':json.dumps({'path':'a.md'}),
            'result':json.dumps(ToolResult(False, side_effects={'status':'none'}, evidence={'kind':'edit_no_match'}).to_dict())}
    repair = {'name':'write_file','state':'completed','input':json.dumps({'path':'a.md','content':'correct final'}),
              'result':json.dumps(ToolResult(True,'written').to_dict())}
    assert recovered_edit_failure(step, [step, repair], tmp_path)
    assert not recovered_edit_failure(step, [repair, step], tmp_path)
    target.write_text('not the claimed repair')
    assert not recovered_edit_failure(step, [step, repair], tmp_path)


def test_mkdir_failure_repaired_by_real_batch_tool_passes_evaluation(tmp_path):
    import server
    from agent_runtime import AgentRuntime, run_agent_turn
    from task_evaluator import TaskEvaluator
    raw, _ = client([response([call('run_command', {'command':['mkdir','wiki/raw']}, 'mkdir')]),
                     response([call('create_directory', {'paths':['wiki/raw']}, 'create')]),
                     *[response(content='Directory created and checked.') for _ in range(3)]])
    runtime = AgentRuntime(messages=[{'role':'system','content':'Follow tool schemas.'}],
                           guard=NS(assess=lambda text:NS(available=False,reason='offline')),
                           memory=None,memory_searcher=None,skill_matcher=NS(match=lambda text:[]),
                           memory_worker=None,memory_classifier_factory=None,semantic_extractor=None,
                           procedure_matcher=None,run_agent=server.run_agent,task_evaluator=TaskEvaluator(tmp_path))
    with execution_scope(task_id='repair-test'), patch('server.get_client', return_value=raw), patch('base.platform.system', return_value='Windows'):
        result = run_agent_turn('Create the wiki/raw directory.', runtime)
        from agent_execution.tasks import TaskStore, task_owner
        from agent_execution.policy import current_context
        steps = TaskStore().steps(task_owner(current_context()), 'repair-test')
    assert result.reply == 'Directory created and checked.', steps
    assert (tmp_path/'wiki/raw').is_dir()


def test_permission_rejection_loop_is_bounded_without_invoking_tools():
    import server
    from permissions import set_current_role
    raw, create = client([response([call('write_file', {'path':'a','content':'x'}, str(i))]) for i in range(4)] + [response(content='Permission prevents this operation.')])
    with set_current_role('viewer'), patch('server.get_client', return_value=raw), patch('server.run_tool_with_context') as run:
        reply = server.run_agent([{'role':'user','content':'write a'}])
    assert reply == 'Permission prevents this operation.'
    assert create.call_count == 5
    run.assert_not_called()


def test_large_latest_fetch_is_retained_with_full_tool_pair_and_marker():
    from agent_execution.context import govern_messages, message_tokens, count_tokens
    import server
    original = 'README principle start\n' + ('large evidence ' * 3000) + '\nsource ending'
    messages = [{'role':'system','content':'Rules.'}, {'role':'user','content':'Explain this repo.'},
                {'role':'assistant','tool_calls':[call('fetch_url', {'url':'https://example.test'}, 'fetch')]},
                {'role':'tool','tool_call_id':'fetch','content':original}]
    fitted = govern_messages(messages, 30000, tools=server.TOOLS_SCHEMAS)
    assert any(m.get('tool_calls') for m in fitted)
    content = next(m['content'] for m in fitted if m.get('role') == 'tool')
    assert 'README principle start' in content and 'truncated' in content and 'source ending' in content
    assert message_tokens(fitted) + count_tokens(server.TOOLS_SCHEMAS) <= 30000
    assert messages[-1]['content'] == original


def test_approval_payload_and_latest_request_are_never_truncated():
    from agent_execution.context import govern_messages, ContextTooLarge
    messages = [{'role':'user','content':'Do the original task.'},
                {'role':'assistant','tool_calls':[call('write_file', {}, 'a')]},
                {'role':'tool','tool_call_id':'a','content':'approval_required: '+('x'*10000)}]
    with pytest.raises(ContextTooLarge): govern_messages(messages, 2000)


def test_progress_feedback_does_not_interrupt_parallel_tool_results():
    import server
    args = {'url':'https://example.test'}
    raw, _ = client([response([call('fetch_url', args, str(i)) for i in range(3)]), response(content='done')])
    messages = [{'role':'user','content':'Explain this repo.'}]
    with patch('server.get_client', return_value=raw), patch('server.run_tool_with_context', return_value='real evidence') as run:
        assert server.run_agent(messages) == 'done'
    pending = set()
    for message in messages:
        if message.get('tool_calls'):
            assert not pending
            pending = {tool['id'] for tool in message['tool_calls']}
        elif message.get('role') == 'tool':
            pending.remove(message['tool_call_id'])
        elif message.get('role') == 'user':
            assert not pending
    assert not pending and run.call_count == 2


@pytest.mark.parametrize('budgeted', [False, True])
def test_empty_tools_are_omitted_at_provider_boundary(budgeted):
    from contextlib import nullcontext
    from agent_execution.budgets import BudgetEndpoint, budget_scope
    raw, create = client([response(content='done')])
    with execution_scope(task_id='empty-tools'), (budget_scope() if budgeted else nullcontext()):
        BudgetEndpoint(raw, 'chat').create(model='test', messages=[{'role':'user','content':'finish'}], tools=[])
    assert 'tools' not in create.call_args.kwargs


def test_streaming_final_round_omits_empty_tools():
    from agent_execution.streaming import streamed_completion
    chunk = NS(choices=[NS(index=0, delta=NS(content='done', tool_calls=None), finish_reason='stop')])
    class Stream:
        def __iter__(self): return iter([chunk])
        def close(self): pass
    create = Mock(return_value=Stream())
    raw = NS(chat=NS(completions=NS(create=create)))
    events = []
    result = streamed_completion(raw, 'test', [{'role':'user','content':'finish'}], [], lambda kind,payload: events.append(kind))
    assert result.content == 'done' and 'model.delta' in events
    assert 'tools' not in create.call_args.kwargs
