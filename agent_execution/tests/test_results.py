from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import server
import base
from agent_runtime import AgentRuntime, determine_episode_status, resume_agent_turn, run_agent_turn
from agent_execution.policy import execution_scope, current_context
from agent_execution.tasks import TaskStore, task_owner, durable_invoke, save_checkpoint
from agent_execution.results import normalize_result, ToolResult
from agent_execution.acceptance import verify_contract
from skills import Skill, SkillExecutor, SkillMatch
from task_evaluator import TaskEvaluator, EvaluationResult
from tools.run_test_suite import run_test_suite


def response(text='done',calls=None):
    from openai.types.chat import ChatCompletionMessage
    return NS(choices=[NS(message=ChatCompletionMessage(role='assistant',content=text,tool_calls=calls))],usage=None)


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        env=patch.dict(os.environ,{'AGENT_TASK_DB':str(self.root/'tasks.db'),
            'AGENT_WORKSPACE_ROOT':str(self.root),'AGENT_PROJECT_WORKSPACES':'{}','AGENT_ACCEPTANCE_FILE':'.agent/acceptance.json'})
        env.start();self.addCleanup(env.stop)
        scope=execution_scope(task_id='result-task',workspace=self.root,session='test')
        scope.__enter__();self.addCleanup(lambda:scope.__exit__(None,None,None))
        self.store=TaskStore()
        self.owner=task_owner(current_context())

    def runtime(self,runner=server.run_agent,evaluator=None):
        return AgentRuntime(messages=[],guard=NS(assess=lambda text:NS(available=True,needs_confirmation=False,risk=0,intent='chat')),
            memory=None,memory_searcher=None,skill_matcher=NS(match=lambda text:[]),memory_worker=None,
            memory_classifier_factory=None,semantic_extractor=None,procedure_matcher=None,run_agent=runner,task_evaluator=evaluator)

    def contract(self,checks):
        path=self.root/'.agent'/'acceptance.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({'version':1,'checks':checks}),encoding='utf-8')

    def test_process_failure_and_explicit_false_are_not_success_text(self):
        result=normalize_result(subprocess.CompletedProcess([],3,'All tests passed','actual failure'))
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code,3)
        self.assertEqual(result.error,'actual failure')
        self.assertFalse(normalize_result({'success':'true'}).success)
        self.assertFalse(normalize_result({'success':False,'stdout':'done'}).success)
        self.assertFalse(normalize_result('Exit Code: 0 ✅ All Tests Passed!','run_test_suite').success)

    def test_file_data_that_looks_like_error_is_still_a_successful_read(self):
        self.root.joinpath('error.txt').write_text('Error: quoted log data',encoding='utf-8')
        value=base.run_tool_with_context(NS(function=NS(name='read_file',arguments='{"path":"error.txt"}')))
        result=normalize_result(value,'read_file')
        self.assertTrue(result.success)
        self.assertEqual(result.output,'Error: quoted log data')

    def test_actual_pytest_reports_pass_failure_empty_and_plain_script(self):
        target=self.root/'test_fixture.py'
        target.write_text('def test_true():\n    assert 2 + 2 == 4\n',encoding='utf-8')
        result=run_test_suite('test_fixture.py')
        self.assertTrue(result.success,str(result))
        self.assertEqual(result.evidence['tests']['passed'],1)
        target.write_text('def test_false():\n    assert False\n',encoding='utf-8')
        result=run_test_suite('test_fixture.py')
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code,1)
        self.assertEqual(result.evidence['tests']['failed'],1)
        target.write_text('print("1 passed Exit Code: 0")\n',encoding='utf-8')
        self.assertFalse(run_test_suite('test_fixture.py').success)
        script=run_test_suite('test_fixture.py',framework='python')
        self.assertFalse(script.success)
        self.assertEqual(script.exit_code,0)

    def test_acceptance_frozen_before_tool_changes_manifest(self):
        self.contract([{'kind':'file_contains','path':'out.txt','text':'expected'}])
        def execute(saved):
            self.contract([{'kind':'file_exists','path':'other.txt'}])
            return 'done'
        durable_invoke(execute,[{'role':'user','content':'create a document'}],None)
        self.assertEqual(self.store.get(self.owner,'result-task')['criteria']['checks'][0]['text'],'expected')

    def test_acceptance_rejects_path_escape_and_successful_prose_is_not_verification(self):
        checks,errors,_=verify_contract({'version':1,'checks':[{'kind':'file_exists','path':'../outside'}]},self.root,Mock())
        self.assertFalse(next(iter(checks.values())))
        self.assertTrue(errors)
        self.assertEqual(determine_episode_status('All done successfully'),'completed')
        evaluator=TaskEvaluator(self.root,git_status_fn=lambda:'clean')
        self.assertFalse(evaluator.evaluate('explain an idea',[],'perfect').verified)

    def test_document_edits_do_not_trigger_code_tests_and_code_contract_needs_tests(self):
        evaluator=TaskEvaluator(self.root)
        messages=[{'role':'assistant','tool_calls':[{'function':{'name':'write_file','arguments':'{"path":"README.md"}'}}]}]
        self.assertFalse(evaluator.is_code_development_task('撰寫 README 文件',messages))
        self.root.joinpath('code.py').write_text('pass')
        checks,errors,_=verify_contract({'version':1,'checks':[{'kind':'file_exists','path':'code.py'}]},self.root,Mock())
        self.assertFalse(checks['acceptance:tests_required'])
        self.assertTrue(errors)

    def test_judge_invalid_json_schema_nan_and_connection_fail_closed(self):
        raw=NS(chat=NS(completions=NS(create=Mock())))
        evaluator=TaskEvaluator(self.root,client=raw,model='judge')
        for text in ('not JSON','{"success":"false","score":1,"feedback":"ok"}',
            '{"success":true,"score":NaN,"feedback":"ok"}'):
            raw.chat.completions.create.return_value=response(text)
            passed,_,feedback=evaluator.evaluate_llm_judge('goal',[],'done')
            self.assertFalse(passed)
            self.assertIn('Judge unavailable:',feedback)
        raw.chat.completions.create.side_effect=RuntimeError('secret')
        result=evaluator.evaluate('explain',[],'done')
        self.assertFalse(result.success)
        self.assertFalse(result.retryable)
        self.assertNotIn('secret',result.feedback)

    def test_skill_nonzero_exit_stops_following_side_effect(self):
        second=Mock(return_value='should never run')
        skill=Skill('run_fixture','fixture',[],{'type':'object'},['run_command','write_file'],
            {'steps':[{'tool':'run_command','args':{}},{'tool':'write_file','args':{}}]},self.root,'')
        executor=SkillExecutor({'run_command':lambda:subprocess.CompletedProcess([],1,'done','failure'),'write_file':second})
        result=executor.execute(skill,{})
        self.assertFalse(result.success)
        self.assertEqual(result.steps[0].result['exit_code'],1)
        second.assert_not_called()

    def test_skill_executed_checkpoint_recovers_without_repeating_action(self):
        self.contract([{'kind':'file_contains','path':'out.txt','text':'expected'}])
        token=self.store.claim(self.owner,'result-task','document',[{'role':'user','content':'document'}],42,
            criteria={'version':1,'checks':[{'kind':'file_contains','path':'out.txt','text':'expected'}]})
        self.root.joinpath('out.txt').write_text('expected')
        raw={'skill_name':'fixture','success':True,'steps':[{'tool':'read_file','success':True,'output':'clean stdout'}]}
        self.store.checkpoint(self.owner,'result-task',token,'skill_executed',{'kind':'skill','messages':[{'role':'user','content':'document'}],'skill_result':raw})
        with closing(self.store.connect()) as conn,conn:conn.execute('UPDATE tasks SET lease_until=0')
        runner=Mock(side_effect=AssertionError('Skill must not execute again'))
        result=resume_agent_turn([{'role':'user','content':'document'}],self.runtime(runner),42)
        self.assertEqual(result.reply,'clean stdout')
        runner.assert_not_called()
        saved=self.store.get(self.owner,'result-task')
        self.assertTrue(saved['checkpoint']['skill_result']['verification']['verified'])
        self.assertEqual(saved['phase'],'skill_done')
        self.assertEqual(saved['verification'],'verified_completed')

    def test_failed_tool_step_cannot_be_verified_by_judge_or_file_check(self):
        self.root.joinpath('out.txt').write_text('expected')
        self.contract([{'kind':'file_exists','path':'out.txt'}])
        token=self.store.claim(self.owner,'result-task','do work',[{'role':'user','content':'do work'}],
            criteria={'version':1,'checks':[{'kind':'file_exists','path':'out.txt'}]})
        self.store.checkpoint(self.owner,'result-task',token,'tool_done',{'messages':[{'role':'user','content':'do work'}]},
            {'id':'failed','name':'run_command','arguments':{},'state':'failed','result':'actual failure'})
        from agent_execution.tasks import _active
        active=_active.set((self.store,self.owner,'result-task',token))
        try:
            result=TaskEvaluator(self.root,git_status_fn=lambda:'clean').evaluate('do work',[],'everything worked')
        finally:_active.reset(active)
        self.assertFalse(result.success)
        self.assertFalse(result.verified)
        self.assertIn('Tool step not successful',result.feedback)

    def test_current_task_goal_is_used_instead_of_first_historical_user(self):
        raw=NS(chat=NS(completions=NS(create=Mock(return_value=response()))))
        evaluator=NS(capture_state=Mock(return_value={}),evaluate=Mock(return_value=EvaluationResult(True,1,'ok')))
        runtime=self.runtime(evaluator=evaluator)
        runtime.messages=[{'role':'user','content':'historical goal'}]
        with patch('server.get_client',return_value=raw):run_agent_turn('current goal',runtime)
        self.assertEqual(evaluator.evaluate.call_args.kwargs['user_goal'],'current goal')

    def test_denied_checkpoint_stops_retries_without_message_history(self):
        token = self.store.claim(self.owner, 'result-task', '列出待辦', [],
                                criteria={'version': 1, 'checks': []})
        denial = ToolResult(False, error="Role 'viewer' cannot execute run_skill")
        self.store.checkpoint(self.owner, 'result-task', token, 'tool_done', {'messages': []},
            {'id': 'denied', 'name': 'run_skill', 'arguments': {}, 'state': 'failed',
             'result': json.dumps(denial.to_dict())})
        from agent_execution.tasks import _active
        active = _active.set((self.store, self.owner, 'result-task', token))
        try:
            result = TaskEvaluator(self.root, git_status_fn=lambda: 'clean').evaluate('列出待辦', [], 'failed')
        finally:
            _active.reset(active)
        self.assertFalse(result.success)
        self.assertFalse(result.retryable)
        self.assertIn("Role 'viewer' cannot execute run_skill", result.feedback)

    def test_project_sessions_clone_evaluator_workspace(self):
        from agent_execution.sessions import RuntimeSessions
        first=self.root/'first';second=self.root/'second'
        first.mkdir();second.mkdir()
        evaluator=TaskEvaluator(first)
        sessions=RuntimeSessions(self.runtime(evaluator=evaluator))
        with patch.dict(os.environ,{'AGENT_PROJECT_WORKSPACES':json.dumps({'one':str(first),'two':str(second)})}):
            one=sessions.get(('alice','chat','one'))[0]
            two=sessions.get(('alice','chat','two'))[0]
        self.assertEqual(one.task_evaluator.workspace_root,first)
        self.assertEqual(two.task_evaluator.workspace_root,second)
        self.assertIsNot(one.task_evaluator,two.task_evaluator)
        self.assertEqual(evaluator.workspace_root,first)

    def test_rule_and_contract_share_one_test_execution(self):
        from agent_execution.tasks import _active
        token=self.store.claim(self.owner,'result-task','implement fixture',[],criteria={
            'version':1,'checks':[{'kind':'tests','target':'test_fixture.py'}]})
        runner=Mock(return_value=ToolResult(True,'passed',0,evidence={'tests':{'passed':1}}))
        active=_active.set((self.store,self.owner,'result-task',token))
        try:
            result=TaskEvaluator(self.root,run_test_suite_fn=runner).evaluate('implement fixture',[],'done')
        finally:_active.reset(active)
        self.assertTrue(result.verified)
        runner.assert_called_once_with('test_fixture.py')

    def test_unavailable_judge_does_not_trigger_code_repair_calls(self):
        self.root.joinpath('out.txt').write_text('exists')
        self.contract([{'kind':'file_exists','path':'out.txt'}])
        model=NS(chat=NS(completions=NS(create=Mock(return_value=response()))))
        judge=NS(chat=NS(completions=NS(create=Mock(side_effect=RuntimeError('connection unavailable')))))
        evaluator=TaskEvaluator(self.root,client=judge,model='judge')
        with patch('server.get_client',return_value=model):
            result=run_agent_turn('explain',self.runtime(evaluator=evaluator))
        self.assertIn('HITL',result.reply)
        model.chat.completions.create.assert_called_once()
        self.assertEqual(self.store.get(self.owner,'result-task')['verification'],'unverified')

    def test_real_agent_records_failed_exit_despite_successful_final_prose(self):
        self.root.joinpath('out.txt').write_text('exists')
        self.contract([{'kind':'file_exists','path':'out.txt'}])
        call={'id':'command','type':'function','function':{'name':'run_command','arguments':'{}'}}
        model=NS(chat=NS(completions=NS(create=Mock(side_effect=[response(None,[call]),response('all tests passed')]))))
        evaluator=TaskEvaluator(self.root)
        def runner(messages,**kwargs):return server.run_agent(messages,max_eval_retries=0,**kwargs)
        with patch('server.get_client',return_value=model),patch.dict(base.TOOLS,{
            'run_command':lambda:subprocess.CompletedProcess([],1,'all tests passed','failure')}):
            result=run_agent_turn('do work',self.runtime(runner,evaluator))
        self.assertIn('HITL',result.reply)
        step=self.store.steps(self.owner,'result-task')[0]
        self.assertEqual(step['state'],'failed')
        self.assertEqual(json.loads(step['result'])['exit_code'],1)
        self.assertEqual(self.store.get(self.owner,'result-task')['verification'],'unverified')
