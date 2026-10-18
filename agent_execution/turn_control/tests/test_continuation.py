import json
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pytest
from openai.types.chat import ChatCompletionMessage

from agent_execution import execution_scope
from agent_execution.policy import current_context
from agent_execution.tasks import TaskStore, durable_invoke, task_owner
from agent_execution.turn_control.continuation import StepController, TaskYield
from task_evaluator import EvaluationResult


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_TASK_DB', str(tmp_path/'tasks.db'))
    monkeypatch.setenv('AGENT_TENANT_ROOT', str(tmp_path/'tenants'))
    monkeypatch.setenv('AGENT_WORKSPACE_ROOT', str(tmp_path))
    monkeypatch.setenv('AGENT_TRACE_ENABLED', '0')
    monkeypatch.setenv('AGENT_TASK_WORKTREE', '0')
    monkeypatch.setenv('AGENT_EXECUTION_BACKEND', 'sqlite')
    with execution_scope(task_id='continuation', workspace=tmp_path, source='local', identity='local'):
        yield


def response(content=None, calls=None):
    return NS(choices=[NS(message=ChatCompletionMessage(role='assistant',content=content,tool_calls=calls))])


def call(key):
    return {'id':key,'type':'function','function':{'name':'read_file','arguments':json.dumps({'path':key})}}


def test_each_invocation_advances_one_step_and_preserves_pairs_and_limits():
    import server
    create=Mock(side_effect=[response(calls=[call('one'),call('two')]),response(content='done')])
    raw=NS(chat=NS(completions=NS(create=create)))
    evaluator=NS(capture_state=Mock(return_value={}),evaluate=Mock(return_value=EvaluationResult(True,1,'ok')))
    messages=[{'role':'user','content':'inspect files'}]
    kinds=[]
    expected='model'
    tool=Mock(return_value='evidence')
    with patch('server.get_client',return_value=raw),patch('server.run_tool_with_context',tool):
        for _ in range(10):
            controller=StepController(expected)
            kinds.append(expected)
            try:
                reply=durable_invoke(lambda saved:server.run_agent(messages,resume_state=saved,
                    task_evaluator=evaluator,step_controller=controller,max_iterations=4),messages,None,resume=True)
            except TaskYield as boundary:
                expected=boundary.next_kind
                row=TaskStore().get(task_owner(current_context()),'continuation')
                assert row['state']=='pending' and row['phase']=='step_ready'
                assert row['checkpoint']['iteration_limit']==4
            else:
                break
    assert reply=='done'
    assert kinds==['model','tool','tool','model','evaluation']
    assert create.call_count==2 and tool.call_count==2 and evaluator.evaluate.call_count==1
    row=TaskStore().get(task_owner(current_context()),'continuation')
    assert row['state']=='completed' and row['recoveries']==0
    assert [m['tool_call_id'] for m in messages if m['role']=='tool']==['one','two']


def test_recovered_pending_model_keeps_attempt_iteration_and_prepared_request():
    import server
    create=Mock(return_value=response('done'))
    raw=NS(chat=NS(completions=NS(create=create)))
    messages=[{'role':'user','content':'already prepared final request'}]
    saved={'phase':'model_pending','model_request':{'model':'prepared-model','tools':[]},
           'model_attempt':2,'iteration':2,'iteration_limit':2,'pre_state':{}}
    with patch('server.get_client',return_value=raw):
        assert server.run_agent(messages,resume_state=saved,step_controller=StepController('model'))=='done'
    assert create.call_args.kwargs['model']=='prepared-model'
    assert len([m for m in messages if m['role']=='user'])==1


def test_recovered_last_round_can_evaluate_without_another_model_request():
    import server
    messages=[{'role':'user','content':'inspect'}, {'role':'assistant','content':'done'}]
    evaluator=NS(capture_state=Mock(return_value={}),evaluate=Mock(return_value=EvaluationResult(True,1,'ok')))
    saved={'phase':'step_ready','next_kind':'evaluation','iteration':1,'iteration_limit':1,'pre_state':{}}
    with patch('server.get_client') as model:
        assert server.run_agent(messages,resume_state=saved,task_evaluator=evaluator,
                                step_controller=StepController('evaluation'))=='done'
    model.assert_not_called()


def test_wrong_action_yields_before_executing_any_model():
    import server
    messages=[{'role':'user','content':'inspect'}]
    with patch('server.get_client') as model,pytest.raises(TaskYield) as result:
        durable_invoke(lambda saved:server.run_agent(messages,resume_state=saved,
            step_controller=StepController('tool')),messages,None)
    assert result.value.next_kind=='model'
    model.assert_not_called()


def test_unpersisted_yield_cannot_hide_an_uncertain_operation():
    from agent_execution.tasks import RecoveryRequired
    messages=[{'role':'user','content':'inspect'}]
    def invalid(saved): raise TaskYield('model')
    with pytest.raises(RecoveryRequired):
        durable_invoke(invalid,messages,None)
    assert TaskStore().get(task_owner(current_context()),'continuation')['state']=='uncertain'


def test_model_feedback_yields_to_model_without_executing_discarded_tools():
    import server
    from hooks import HookManager,FunctionHook,HookLifecycle,HookResult
    raw=NS(chat=NS(completions=NS(create=Mock(return_value=response(calls=[call('discarded')])))))
    hooks=HookManager([FunctionHook('critic',HookLifecycle.POST_MODEL_CALL,lambda _:HookResult.append_feedback('try another response'))])
    messages=[{'role':'user','content':'inspect'}]
    with patch('server.get_client',return_value=raw),patch('server.run_tool_with_context') as tool:
        with pytest.raises(TaskYield) as yielded:
            durable_invoke(lambda saved:server.run_agent(messages,resume_state=saved,hook_manager=hooks,
                step_controller=StepController('model')),messages,None)
    assert yielded.value.next_kind=='model'
    tool.assert_not_called()
    assert any(m.get('tool_call_id')=='discarded' and 'not executed' in m['content'] for m in messages)


@pytest.mark.parametrize('kind', ['model', 'tool'])
def test_pre_hooks_begin_after_a_durable_uncertain_marker(kind):
    import server
    from hooks import HookManager, FunctionHook, HookLifecycle, HookResult
    lifecycle = HookLifecycle.PRE_MODEL_CALL if kind == 'model' else HookLifecycle.PRE_TOOL_EXECUTE
    observed = []
    def before(context):
        task = TaskStore().get(task_owner(current_context()), 'continuation')
        observed.append((task['phase'], task['checkpoint']['next_kind']))
        return HookResult.return_early('deliberate stop')
    messages = [{'role': 'user', 'content': 'inspect'}]
    saved = None
    if kind == 'tool':
        messages.append(response(calls=[call('one')]).choices[0].message.model_dump(exclude_none=True))
        saved = {'phase': 'step_ready', 'next_kind': 'tool', 'iteration': 1, 'iteration_limit': 4}
    with patch('server.get_client') as model:
        durable_invoke(lambda _: server.run_agent(messages, resume_state=saved,
            hook_manager=HookManager([FunctionHook('before', lifecycle, before)]),
            step_controller=StepController(kind)), messages, None)
    assert observed == [('step_preparing', kind)]
    model.assert_not_called()
