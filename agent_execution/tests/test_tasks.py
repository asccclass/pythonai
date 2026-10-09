from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import base
import server
from agent_execution import execution_scope
from agent_execution.policy import current_context, ExecutionContext
from agent_execution.tasks import TaskStore, TaskBusy, RecoveryRequired, task_owner, durable_invoke, dispatch_task_input
from agent_runtime import AgentRuntime, resume_agent_turn
from exceptions import TaskSuspendedException
from task_evaluator import EvaluationResult
from agent_execution.tests.checkpoint_fixture import append_artifact, response


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        env=patch.dict(os.environ,{'AGENT_TASK_DB':str(self.root/'tasks.db'),'AGENT_WORKSPACE_ROOT':str(self.root),'AGENT_PROJECT_WORKSPACES':'{}'})
        env.start();self.addCleanup(env.stop)
        scope=execution_scope(task_id='crash',session='session',workspace=self.root)
        scope.__enter__();self.addCleanup(lambda:scope.__exit__(None,None,None))
        self.owner=task_owner(current_context())
        self.store=TaskStore()

    def runtime(self,evaluator=None):
        return AgentRuntime(messages=[],guard=None,memory=None,memory_searcher=None,skill_matcher=None,
            memory_worker=None,memory_classifier_factory=None,semantic_extractor=None,procedure_matcher=None,
            run_agent=server.run_agent,task_evaluator=evaluator)

    def expire(self):
        with closing(self.store.connect()) as conn, conn:
            conn.execute('UPDATE tasks SET lease_until=0')

    def crash(self,phase):
        result=subprocess.run([sys.executable,'-m','agent_execution.tests.checkpoint_fixture',str(self.root),phase],
            capture_output=True,text=True,encoding='utf-8',timeout=25)
        self.assertEqual(result.returncode,23,result.stderr)
        self.assertEqual(self.store.get(self.owner,'crash')['phase'],phase)
        self.expire()

    def test_crash_at_model_tool_and_evaluation_boundaries_does_not_repeat_effects(self):
        for phase in ('model_received','tool_done','evaluation_pending','evaluation_done'):
            with self.subTest(phase=phase):
                # Each boundary uses an independent DB and artifact.
                with closing(self.store.connect()) as conn, conn:
                    conn.execute('DELETE FROM tasks');conn.execute('DELETE FROM task_steps')
                target=self.root/'artifact.txt'
                if target.exists():target.unlink()
                self.crash(phase)
                evaluator=NS(capture_state=Mock(return_value={'files':{}}),evaluate=Mock(return_value=EvaluationResult(True,1,'verified fixture')))
                client=NS(chat=NS(completions=NS(create=Mock(return_value=response('done')))))
                saved=self.store.get(self.owner,'crash')
                with patch('server.get_client',return_value=client),patch.dict(base.TOOLS,{'write_file':append_artifact}):
                    result=resume_agent_turn(saved['checkpoint']['messages'],self.runtime(evaluator))
                self.assertEqual(result.reply,'done')
                self.assertEqual(result.episode_id,99)
                self.assertEqual(target.read_text(),'hit')
                self.assertEqual(self.store.get(self.owner,'crash')['state'],'completed')
                if phase=='evaluation_done':
                    evaluator.evaluate.assert_not_called()
                if phase in {'evaluation_pending','evaluation_done'}:
                    client.chat.completions.create.assert_not_called()

    def test_crash_before_tool_execution_requires_human_resolution(self):
        self.crash('tool_started')
        with self.assertRaises(RecoveryRequired):
            self.store.claim(self.owner,'crash','write fixture',[])
        self.assertEqual(self.store.get(self.owner,'crash')['state'],'uncertain')
        self.assertFalse((self.root/'artifact.txt').exists())
        with self.assertRaises(RecoveryRequired):self.store.resolve(self.owner,'crash','wrong','not performed')
        self.store.resolve(self.owner,'crash','write-1','Human checked: not performed')
        result=dispatch_task_input('/steps crash',self.runtime())
        self.assertEqual(json.loads(result.reply)[0]['state'],'resolved_by_human')

    def test_active_worker_and_old_lease_cannot_execute_or_commit(self):
        token=self.store.claim(self.owner,'crash','goal',[])
        with self.assertRaises(TaskBusy):self.store.claim(self.owner,'crash','goal',[])
        self.expire()
        newer=self.store.claim(self.owner,'crash','goal',[])
        with self.assertRaises(TaskBusy):self.store.checkpoint(self.owner,'crash',token,'tool_started',{'messages':[]})
        self.assertTrue(self.store.renew(self.owner,'crash',newer))
        with self.assertRaises(KeyError):self.store.get(task_owner(ExecutionContext(identity='bob')),'crash')

    def test_suspended_answer_survives_restart_and_keeps_call_pair_and_episode(self):
        self.assertTrue(any(s['function']['name']=='ask_user' for s in base.TOOLS_SCHEMAS))
        call=NS(id='ask-1',function=NS(name='ask_user',arguments='{"question":"which file?"}'))
        client=NS(chat=NS(completions=NS(create=Mock(return_value=response(calls=[call])))))
        messages=[{'role':'user','content':'write fixture'}]
        with patch('server.get_client',return_value=client):
            result=resume_agent_turn(messages,self.runtime(),episode_id=99)
        self.assertTrue(result.suspended)
        self.assertEqual(result.reply,'which file?')
        self.assertEqual(TaskStore().get(self.owner,'crash')['state'],'suspended')
        next_client=NS(chat=NS(completions=NS(create=Mock(return_value=response('done')))))
        with patch('server.get_client',return_value=next_client):
            result=dispatch_task_input('note.txt',self.runtime())
        self.assertEqual(result.episode_id,99)
        sent=next_client.chat.completions.create.call_args.kwargs['messages']
        self.assertTrue(any(m.get('role')=='tool' and m.get('tool_call_id')=='ask-1' and m.get('content')=='note.txt' for m in sent))
        self.assertEqual(self.store.get(self.owner,'crash')['state'],'completed')

    def test_completed_task_is_cached_and_cancellation_is_owned(self):
        messages=[{'role':'user','content':'goal'}]
        fn=Mock(return_value='done')
        self.assertEqual(durable_invoke(fn,messages,99),'done')
        self.assertEqual(durable_invoke(fn,messages,99),'done')
        fn.assert_called_once()
        with self.assertRaises(RecoveryRequired):durable_invoke(fn,[{'role':'user','content':'different goal'}],99)
        with execution_scope(task_id='cancel'):
            token=self.store.claim(self.owner,'cancel','goal',[])
            self.assertFalse(self.store.cancel('other','cancel'))
            self.assertTrue(self.store.cancel(self.owner,'cancel'))
            self.assertFalse(self.store.renew(self.owner,'cancel',token))

    def test_recovery_attempts_are_bounded(self):
        self.store.claim(self.owner,'crash','goal',[])
        for _ in range(3):
            self.expire();self.store.claim(self.owner,'crash','goal',[])
        self.expire()
        with self.assertRaises(RecoveryRequired):self.store.claim(self.owner,'crash','goal',[])

    def test_remote_jobs_recover_only_when_lease_expired_and_phase_safe(self):
        from communication_models import InboundMessage
        from communication_store import CommunicationStore
        from communication_worker import CommunicationWorker
        comm=CommunicationStore(self.root/'comm.db')
        job,_=comm.ingest_inbound_message(InboundMessage('telegram','1','chat','alice','goal'))
        comm.mark_command_running(job.command_id)
        owner=task_owner(ExecutionContext(source='communication',identity='telegram:alice',session='telegram:chat'))
        token=self.store.claim(owner,job.command_id,'goal',[])
        runner=Mock();runner.task_store_enabled=True
        worker=CommunicationWorker(comm,{'telegram':Mock()},runner)
        worker.recover_tasks()
        self.assertEqual(comm.command_by_id(job.command_id).status,'running')
        self.expire();worker.recover_tasks()
        self.assertEqual(comm.command_by_id(job.command_id).status,'pending')
        comm.mark_command_running(job.command_id)
        token=self.store.claim(owner,job.command_id,'goal',[])
        self.store.checkpoint(owner,job.command_id,token,'tool_started',{'messages':[]})
        self.expire();worker.recover_tasks()
        self.assertEqual(comm.command_by_id(job.command_id).status,'recovery_required')
        self.assertEqual(self.store.get(owner,job.command_id)['state'],'uncertain')

    def test_new_turn_does_not_replay_unpaired_historical_tool(self):
        messages=[{'role':'assistant','tool_calls':[{'id':'old','type':'function','function':{'name':'write_file','arguments':'{"path":"artifact.txt","content":"hit"}'}}]},
                  {'role':'user','content':'a new unrelated request'}]
        client=NS(chat=NS(completions=NS(create=Mock(return_value=response('new answer')))))
        with patch('server.get_client',return_value=client),patch.dict(base.TOOLS,{'write_file':Mock()}) as tools:
            reply=server.run_agent(messages)
        self.assertEqual(reply,'new answer')
        self.assertFalse((self.root/'artifact.txt').exists())

    def test_storage_capacity_is_atomic_and_does_not_drop_existing_checkpoint(self):
        from agent_execution.delivery import CapacityError
        token=self.store.claim(self.owner,'crash','goal',[])
        before=self.store.get(self.owner,'crash')['checkpoint']
        with patch.dict(os.environ,{'AGENT_TASK_STORAGE_BYTES':'1'}):
            with self.assertRaises(CapacityError):
                self.store.checkpoint(self.owner,'crash',token,'tool_started',{'messages':[{'role':'user','content':'more'}]})
        self.assertEqual(self.store.get(self.owner,'crash')['checkpoint'],before)

    def test_skill_result_is_durable_and_cached_as_clean_stdout(self):
        messages=[{'role':'user','content':'skill goal'}]
        value={'skill_name':'fixture','success':True,'steps':[{'output':{'stdout':'中文結果','returncode':0}}]}
        function=Mock(return_value=value)
        self.assertEqual(durable_invoke(function,messages,99,skill=True),value)
        self.assertEqual(durable_invoke(function,messages,99,skill=True),value)
        function.assert_called_once()
        self.assertEqual(self.store.get(self.owner,'crash')['reply'],'中文結果')
        self.assertEqual(dispatch_task_input('/resume crash',self.runtime()).reply,'中文結果')

    def test_actual_process_exit_after_skill_effect_does_not_repeat_skill(self):
        self.crash('skill_executed')
        runtime=self.runtime()
        runtime.run_agent=Mock(side_effect=AssertionError('Skill must not execute again'))
        saved=self.store.get(self.owner,'crash')
        result=resume_agent_turn(saved['checkpoint']['messages'],runtime,99)
        self.assertEqual(result.reply,'hit')
        self.assertEqual(self.root.joinpath('artifact.txt').read_text(),'hit')
        runtime.run_agent.assert_not_called()
        self.assertEqual(self.store.get(self.owner,'crash')['verification'],'unverified')

    def test_viewer_cannot_supply_side_effect_resolution(self):
        from permissions import set_current_role
        self.crash('tool_started')
        self.store.mark_uncertain(self.owner,'crash')
        with set_current_role('viewer'):
            result=dispatch_task_input('/resolve crash write-1 fake result',self.runtime())
        self.assertIn('admin role',result.reply)
        self.assertEqual(self.store.get(self.owner,'crash')['state'],'uncertain')

    def test_clear_cancels_durable_suspension_without_affecting_another_owner(self):
        token=self.store.claim(self.owner,'crash','goal',[])
        self.store.checkpoint(self.owner,'crash',token,'waiting_user',{'messages':[],'suspended_tool_call_id':'ask1'})
        self.store.finish(self.owner,'crash',token,'suspended','question')
        self.store.claim('other','crash','goal',[])
        result=dispatch_task_input('/clear',self.runtime())
        self.assertIn('cleared',result.reply)
        self.assertEqual(self.store.get(self.owner,'crash')['state'],'cancelled')
        self.assertEqual(self.store.get('other','crash')['state'],'running')
        self.assertIsNone(dispatch_task_input('new request',self.runtime()))
