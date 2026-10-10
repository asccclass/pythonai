import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock,patch

import server
from hooks import FunctionHook,HookContext,HookManager,HookLifecycle,HookDecision,HookResult,default_hook_manager
from agent_execution.policy import execution_scope,execute_tool,policy_tool,PolicyDenied
from agent_execution.hook_policies import POLICIES
from permissions import set_current_role
from scheduler import execute_job


class PolicyHookTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.events=[]
        self.memory=NS(add_event=lambda episode,kind,**kwargs:self.events.append((kind,kwargs)))
        env=patch.dict(os.environ,{'AGENT_HOOK_POLICIES':'{}','AGENT_WORKSPACE_ROOT':str(self.root)})
        env.start();self.addCleanup(env.stop)
        scope=execution_scope(memory=self.memory,episode_id=1,workspace=self.root)
        scope.__enter__();self.addCleanup(lambda:scope.__exit__(None,None,None))

    def names(self):return [event[1]['metadata']['hook'] for event in self.events if event[0]=='hook_execution']

    def test_config_enable_order_diagnostics_and_unknown_options(self):
        config={'argument_validation':{'priority':50},'model_budget':{'enabled':False}}
        with patch.dict(os.environ,{'AGENT_HOOK_POLICIES':json.dumps(config)}),patch('builtins.print') as output:
            manager=default_hook_manager(diagnose=True)
        self.assertEqual(manager.diagnostics()['active'],5)
        output.assert_called_once()
        ctx=HookContext(HookLifecycle.PRE_TOOL_EXECUTE,tool_name='read_file',tool_arguments={'path':'file'},metadata={'function':lambda path:None})
        manager.dispatch(HookLifecycle.PRE_TOOL_EXECUTE,ctx)
        self.assertEqual([event['hook'] for event in ctx.audit_metadata['hook_events']],['tool_admission','argument_validation'])
        for config in ({'unknown':{}},{'model_budget':{'enabled':'false'}},{'model_budget':{'priority':True}}):
            with patch.dict(os.environ,{'AGENT_HOOK_POLICIES':json.dumps(config)}),self.assertRaises(ValueError):default_hook_manager()

    def test_direct_decorated_and_routed_calls_execute_and_audit_once(self):
        effect=Mock(return_value='hello')
        @policy_tool('read_file')
        def read(path):return effect(path)
        execute_tool('read_file',{'path':'note.txt'},read)
        effect.assert_called_once_with('note.txt')
        self.assertEqual(self.names(),['argument_validation','tool_admission','result_audit'])
        self.events.clear()
        read('note.txt')
        self.assertEqual(self.names(),['argument_validation','tool_admission','result_audit'])

    def test_recursive_distinct_calls_are_audited_even_with_same_tool_name(self):
        @policy_tool('read_file')
        def read(path,depth=1):return read(path,depth-1) if depth else 'data'
        execute_tool('read_file',{'path':'note.txt'},read)
        self.assertEqual(self.names().count('tool_admission'),2)
        self.assertEqual(self.names().count('result_audit'),2)

    def test_model_native_decorated_tool_has_one_pre_and_post_audit(self):
        import base
        self.root.joinpath('note.txt').write_text('data')
        from openai.types.chat import ChatCompletionMessage
        call={'id':'read','type':'function','function':{'name':'read_file','arguments':'{"path":"note.txt"}'}}
        values=[NS(choices=[NS(message=ChatCompletionMessage(role='assistant',tool_calls=[call]))]),
            NS(choices=[NS(message=ChatCompletionMessage(role='assistant',content='done'))])]
        raw=NS(chat=NS(completions=NS(create=Mock(side_effect=values))))
        with patch('server.get_client',return_value=raw):
            server.run_agent([{'role':'user','content':'read'}],memory=self.memory,episode_id=1)
        self.assertEqual(self.names().count('argument_validation'),1)
        self.assertEqual(self.names().count('tool_admission'),1)
        self.assertEqual(self.names().count('result_audit'),1)

    def test_nested_skill_actions_each_have_own_hook_audit(self):
        @policy_tool('read_file')
        def read(path):return 'data'
        @policy_tool('run_skill')
        def workflow():
            with execution_scope(source='skill'):
                return execute_tool('read_file',{'path':'note.txt'},read)
        execute_tool('run_skill',{},workflow)
        self.assertEqual(self.names().count('argument_validation'),2)
        self.assertEqual(self.names().count('tool_admission'),2)
        self.assertEqual(self.names().count('result_audit'),2)
        sources=[event[1]['metadata']['metadata'].get('source') for event in self.events if event[0]=='hook_execution' and event[1]['metadata']['hook']=='tool_admission']
        self.assertEqual(sources,['local','skill'])

    def test_disabling_all_hooks_does_not_disable_mandatory_permissions(self):
        config={name:{'enabled':False} for name in POLICIES}
        effect=Mock()
        with patch.dict(os.environ,{'AGENT_HOOK_POLICIES':json.dumps(config)}),set_current_role('viewer'):
            manager=default_hook_manager()
            self.assertEqual(manager.diagnostics()['active'],0)
            with self.assertRaises(PolicyDenied):execute_tool('write_file',{'path':'x'},effect)
        effect.assert_not_called()

    def test_bad_arguments_and_path_block_before_effect_and_audit(self):
        effect=Mock()
        with self.assertRaises(PolicyDenied):execute_tool('read_file',{},lambda path:effect(path))
        self.assertEqual(self.names(),['argument_validation'])
        self.events.clear()
        with self.assertRaises(PolicyDenied):execute_tool('read_file',{'path':'../outside'},lambda path:effect(path))
        self.assertEqual(self.names(),['argument_validation','tool_admission'])
        effect.assert_not_called()

    def test_schema_primitive_types_and_enum_block_effects(self):
        effect=Mock()
        for name,args in [('run_command',{'command':'echo hello'}),('manage_memory',{'action':'unknown','memory_id':1}),
            ('manage_memory',{'action':'confirm','memory_id':True})]:
            with self.assertRaises(PolicyDenied):execute_tool(name,args,effect)
        effect.assert_not_called()

    def test_applies_failures_and_invalid_returns_obey_fail_closed(self):
        for fail_closed in (True,False):
            hook=FunctionHook('broken',HookLifecycle.PRE_MODEL_CALL,lambda context:HookResult.continue_(),fail_closed=fail_closed)
            hook.applies=Mock(side_effect=RuntimeError('predicate failure'))
            manager=HookManager([hook])
            result=manager.dispatch(HookLifecycle.PRE_MODEL_CALL,HookContext(HookLifecycle.PRE_MODEL_CALL))
            self.assertEqual(result.decision,HookDecision.BLOCK if fail_closed else HookDecision.CONTINUE)
        manager=HookManager([FunctionHook('invalid',HookLifecycle.PRE_MODEL_CALL,lambda context:False)])
        self.assertEqual(manager.dispatch(HookLifecycle.PRE_MODEL_CALL,HookContext(HookLifecycle.PRE_MODEL_CALL)).decision,HookDecision.BLOCK)
        with self.assertRaises(ValueError):manager.register(FunctionHook('invalid',HookLifecycle.PRE_MODEL_CALL,lambda context:None))

    def test_tool_exception_records_failed_result_before_reraising(self):
        def failed():raise RuntimeError('failed')
        with self.assertRaises(RuntimeError):execute_tool('read_file',{},failed)
        audit=[event[1]['metadata'] for event in self.events if event[0]=='hook_execution'][-1]
        self.assertEqual(audit['hook'],'result_audit')
        self.assertFalse(audit['metadata']['execution']['success'])

    def test_scheduler_hooks_preserve_owner_and_do_not_repeat_command(self):
        effect=Mock(return_value=subprocess.CompletedProcess([],0,'done',''))
        job={'job_type':'command','job_payload':json.dumps({'command':['echo','hi']}),'owner_identity':'owner','owner_role':'admin'}
        with patch('base.run_command',effect),patch('agent_execution.scheduling.engine.get_user_role',return_value='admin'):
            result=execute_job(job)
        self.assertEqual(result[0],'completed')
        effect.assert_called_once()
        admissions=[event[1]['metadata']['metadata']['identity'] for event in self.events if event[0]=='hook_execution' and event[1]['metadata']['hook']=='tool_admission']
        self.assertIn('owner',admissions)

    def test_model_request_mutation_is_used_and_bad_tool_arguments_do_not_execute(self):
        raw=NS(chat=NS(completions=NS(create=Mock(return_value=NS(choices=[NS(message=NS(content='done',tool_calls=None))])))))
        def route(context):context.model_request['model']='alternate';return HookResult.continue_()
        manager=HookManager([FunctionHook('route',HookLifecycle.PRE_MODEL_CALL,route)])
        with patch('server.get_client',return_value=raw):server.run_agent([{'role':'user','content':'hello'}],hook_manager=manager)
        self.assertEqual(raw.chat.completions.create.call_args.kwargs['model'],'alternate')
        call=NS(id='call',function=NS(name='write_file',arguments='[]'))
        raw.chat.completions.create.return_value=NS(choices=[NS(message=NS(content=None,tool_calls=[call]))])
        with patch('server.get_client',return_value=raw),patch('server.run_tool_with_context') as execute:
            reply=server.run_agent([{'role':'user','content':'hello'}])
        self.assertIn('object',reply)
        execute.assert_not_called()
