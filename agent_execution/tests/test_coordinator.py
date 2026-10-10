from contextlib import closing
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from agent_execution.coordinator import Coordinator,CoordinatorService,scope_owner,merge_node,wait_node,node_task_owner,human_agent_command,snapshot_commit
from agent_execution.coordinator_worker import review_result
from agent_execution.policy import execution_scope,current_context,execute_tool,PolicyDenied
from agent_execution.tasks import TaskStore,task_owner,_active
from agent_execution.budgets import budget_scope,BudgetExhausted,_depth
from agent_execution.worktrees import git,WorktreeStore


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.repo=self.root/'repo';self.repo.mkdir()
        git(self.repo,'init');git(self.repo,'config','user.name','Test');git(self.repo,'config','user.email','test@example.invalid')
        (self.repo/'original.txt').write_text('user baseline');git(self.repo,'add','original.txt');git(self.repo,'commit','-m','base')
        env=patch.dict(os.environ,{'AGENT_TASK_DB':str(self.root/'tasks.db'),'AGENT_TASK_MAX_CHILDREN':'8',
            'AGENT_TASK_MAX_REQUESTS':'40','AGENT_TASK_MAX_TOKENS':'1000000','AGENT_TASK_DEADLINE_SECONDS':'600',
            'AGENT_WORKTREE_ROOT':str(self.root/'worktrees'),'AGENT_SUBAGENT_CONCURRENCY':'2','AGENT_SUBAGENT_TIMEOUT_SECONDS':'120',
            'AGENT_TASK_WORKTREE':'0'})
        env.start();self.addCleanup(env.stop)
        context=execution_scope(workspace=self.repo,task_id='root',session='test');context.__enter__();self.addCleanup(lambda:context.__exit__(None,None,None))
        self.owner=scope_owner();self.store=Coordinator()
        self.criteria={'version':1,'checks':[{'kind':'file_contains','path':'result.txt','text':'result'}]}
        token=self.store.tasks.claim(self.owner,'root','delegate',[{'role':'user','content':'delegate'}],criteria=self.criteria)
        active=_active.set((self.store.tasks,self.owner,'root',token));self.addCleanup(lambda:_active.reset(active))
        scope=budget_scope();self.budget=scope.__enter__();self.addCleanup(lambda:scope.__exit__(None,None,None))
        self.service=CoordinatorService(self.store,[sys.executable,'-m','agent_execution.tests.coordinator_fixture'])
        self.addCleanup(self.service.close)

    def until(self,key,states,timeout=40):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            node=self.store.get(self.owner,key)
            if node['state'] in states:
                with closing(self.store.connect()) as conn:
                    node['events']=[dict(row) for row in conn.execute('SELECT event,data FROM agent_events WHERE node=? ORDER BY id',(key,))]
                return node
            time.sleep(.05)
        self.fail('Agent did not settle: '+json.dumps(self.store.get(self.owner,key),default=str))

    def test_graph_dependencies_owner_and_atomic_child_budget(self):
        first=self.store.submit('planner','Plan only')
        second=self.store.submit('coder','Create result',[first])
        self.assertEqual(self.budget.snapshot()['children'],2)

        claim=self.store.claim();self.assertEqual(claim['id'],first)
        self.assertIsNone(self.store.claim())
        with execution_scope(identity='someone else'),self.assertRaises(KeyError):self.store.get(scope_owner(),first)
        with self.assertRaises(KeyError):self.store.submit('coder','bad',['0'*32])
        with self.assertRaises(ValueError):self.store.submit('coder','bad',[first,first])
        depth=_depth.set(self.budget.max_depth)
        try:
            with self.assertRaises(BudgetExhausted):self.store.submit('coder','no recursive spawn')
        finally:_depth.reset(depth)
        self.assertEqual(self.budget.snapshot()['children'],2)

    def test_snapshot_rejects_link_before_adding_external_contents(self):
        outside=self.root/'outside';outside.mkdir();(outside/'private.txt').write_text('outside content')
        link=self.repo/'linked'
        if os.name=='nt':
            command=['powershell','-NoProfile','-Command','New-Item -ItemType Junction -Path '+"'"+str(link)+"' -Target '"+str(outside)+"' | Out-Null"]
            __import__('subprocess').run(command,check=True,capture_output=True)
        else:link.symlink_to(outside,target_is_directory=True)
        self.addCleanup(lambda:link.rmdir() if os.name=='nt' else link.unlink())
        # Git may hide an ignored link; a tracked path must also be checked.
        with patch('agent_execution.coordinator.git',return_value='linked/private.txt\x00') as calls,self.assertRaises(ValueError):snapshot_commit(self.repo)
        self.assertEqual(calls.call_count,1)
        self.assertEqual((outside/'private.txt').read_text(),'outside content')

    def test_cross_process_claim_concurrency_is_bounded(self):
        keys=[self.store.submit('planner','Plan '+str(n)) for n in range(3)]
        claims=[]
        threads=[threading.Thread(target=lambda:claims.append(Coordinator(self.store.path).claim())) for _ in range(5)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(len([c for c in claims if c]),2)
        self.assertEqual(len({c['id'] for c in claims if c}),2)

    def test_review_schema_and_role_tool_isolation(self):
        self.assertTrue(review_result('{"approve":true,"findings":[]}')['approve'])
        for value in ('yes','{"approve":"true","findings":[]}','{"approve":true,"findings":[{"path":"a","line":1,"message":"bug"}]}','{"approve":false,"findings":[{"path":"../escape","line":1,"message":"bug"}]}'):
            with self.subTest(value=value),self.assertRaises((ValueError,json.JSONDecodeError)):review_result(value)
        effect=[]
        with execution_scope(allowed_tools=frozenset({'read_file'})),self.assertRaises(PolicyDenied):
            execute_tool('write_file',{'path':'file.txt','content':'no'},lambda **kwargs:effect.append(True))
        self.assertEqual(effect,[])

    def test_coder_review_and_selected_merge_preserve_parent_index(self):
        (self.repo/'original.txt').write_text('staged user change');git(self.repo,'add','original.txt')
        staged=git(self.repo,'diff','--cached')
        key=self.store.submit('coder','Create result');self.service.start()
        node=self.until(key,{'reviewed','rejected','failed','needs_input','needs_resolution'})
        self.assertEqual(node['state'],'reviewed',node)
        self.assertFalse((self.repo/'result.txt').exists())
        evidence=merge_node(self.store,self.owner,key)
        self.assertEqual((self.repo/'result.txt').read_text(),'result')
        self.assertEqual(git(self.repo,'diff','--cached'),staged)
        self.assertEqual((self.repo/'original.txt').read_text(),'staged user change')
        self.assertEqual(evidence['files'],['result.txt'])
        self.assertEqual(merge_node(self.store,self.owner,key),evidence)

    def test_rejected_reviewer_and_parent_conflict_cannot_deliver(self):
        bad=self.store.submit('coder','Create result reject');good=self.store.submit('coder','Create result')
        self.service.start()
        self.assertEqual(self.until(bad,{'rejected','failed','needs_input'})['state'],'rejected')
        with self.assertRaises(ValueError):merge_node(self.store,self.owner,bad)
        self.assertEqual(self.until(good,{'reviewed','failed','needs_input'})['state'],'reviewed')
        (self.repo/'result.txt').write_text('user new content')
        with self.assertRaises(ValueError):merge_node(self.store,self.owner,good)
        self.assertEqual((self.repo/'result.txt').read_text(),'user new content')
        self.assertEqual(self.store.get(self.owner,good)['state'],'conflict')

    def test_followup_and_dependency_dag_use_isolated_snapshots(self):
        first=self.store.submit('coder','Create result reject');self.service.start()
        old=self.until(first,{'rejected','failed','needs_input'})
        self.assertEqual(old['state'],'rejected')
        second=self.store.submit('coder','Create result',previous=old)
        self.assertEqual(self.until(second,{'reviewed','rejected','failed','needs_input'})['state'],'reviewed')
        next_node=self.store.submit('coder','Create second',[second])
        node=self.until(next_node,{'reviewed','rejected','failed','needs_input','needs_resolution'})
        self.assertEqual(node['state'],'reviewed',node)
        self.assertTrue(Path(node['result']['workspace'],'result.txt').exists())
        self.assertTrue(Path(node['result']['workspace'],'second.txt').exists())
        self.assertFalse((self.repo/'second.txt').exists())

    def test_lost_lease_is_fenced_and_review_creation_is_idempotent(self):
        key=self.store.submit('coder','Create result');node=self.store.claim()
        with closing(self.store.connect()) as conn,conn:conn.execute('UPDATE agent_nodes SET expires=0 WHERE id=?',(key,))
        self.store.recover();replacement=self.store.claim()
        with self.assertRaises(RuntimeError):self.store.publish(node,{'success':True,'verified':True,'commit':node['payload']['snapshot']})
        self.store.publish(replacement,{'success':True,'verified':True,'commit':node['payload']['snapshot'],'base':node['payload']['snapshot'],'reply':'done'})
        count=self.budget.snapshot()['children']
        self.store.ensure_review(self.owner,key);self.store.ensure_review(self.owner,key)
        self.assertEqual(self.budget.snapshot()['children'],count)
        with closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM agent_nodes WHERE review_of=?',(key,)).fetchone()[0],1)

    def test_real_crash_after_tool_done_recovers_without_repeating_effect(self):
        key=self.store.submit('coder','Create result crash-after');self.service.start()
        node=self.until(key,{'reviewed','failed','needs_input','needs_resolution'})
        self.assertEqual(node['state'],'reviewed',node)
        child=self.store.child_task(node)
        self.assertEqual(child['recoveries'],1)
        steps=self.store.tasks.steps(node_task_owner(node['payload']),'subagent:'+key)
        self.assertEqual(len([step for step in steps if step['name']=='write_file']),1)
        self.assertEqual(Path(node['result']['workspace'],'result.txt').read_text(),'result')
        self.assertEqual(Path(node['payload']['storage'],key+'.effects.log').read_text(),'hit\n')

    def test_uncertain_crash_requires_human_resolution(self):
        key=self.store.submit('coder','Create result crash-before');self.service.start()
        node=self.until(key,{'needs_resolution','failed','needs_input'})
        self.assertEqual(node['state'],'needs_resolution',node)
        worktrees=WorktreeStore(node['payload']['repository'],node['payload']['storage'])
        workspace=worktrees.location(node_task_owner(node['payload']),'subagent:'+key)
        self.assertFalse((workspace/'result.txt').exists())
        child=self.store.child_task(node)
        self.assertEqual(child['phase'],'tool_started')
        self.assertEqual(child['state'],'uncertain')
        with self.assertRaises(ValueError):self.store.submit('coder','Retry blindly',previous=node)
        self.service.close()
        with patch('agent_execution.coordinator.start_coordinator'):
            human_agent_command('/agents root resolve '+key+' effect Observed no file write')
        child=self.store.child_task(node)
        self.assertEqual(child['phase'],'tool_done')
        self.assertEqual(child['state'],'pending')
        self.assertFalse((workspace/'result.txt').exists())

    def test_expired_root_stops_queued_work_and_wait_is_bounded(self):
        key=self.store.submit('planner','Plan')
        self.assertEqual(wait_node(self.store,self.owner,key,0)['state'],'queued')
        with self.assertRaises(ValueError):wait_node(self.store,self.owner,key,31)
        with closing(self.store.connect()) as conn,conn:conn.execute('UPDATE agent_nodes SET deadline=0 WHERE id=?',(key,))
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.get(self.owner,key)['state'],'timed_out')

    def test_new_supervisor_recovers_persisted_expired_worker(self):
        key=self.store.submit('planner','Plan after supervisor restart')
        old=self.store.claim()
        with closing(self.store.connect()) as conn,conn:conn.execute('UPDATE agent_nodes SET expires=0 WHERE id=?',(key,))
        restarted=CoordinatorService(Coordinator(self.store.path),self.service.command)
        self.addCleanup(restarted.close);restarted.start()
        node=self.until(key,{'completed','failed','needs_input'})
        self.assertEqual(node['state'],'completed',node)
        self.assertEqual(node['attempts'],2)
        with self.assertRaises(RuntimeError):self.store.publish(old,{'success':True,'verified':False})

    def test_reviewer_worker_failure_rejects_waiting_coder(self):
        key=self.store.submit('coder','Create result');coder=self.store.claim()
        self.store.publish(coder,{'success':True,'verified':True,'commit':coder['payload']['snapshot'],'reply':'done'})
        reviewer=self.store.claim();self.assertEqual(reviewer['review_of'],key)
        self.store.fail(reviewer,'failed','ConnectionError')
        self.assertEqual(self.store.get(self.owner,key)['state'],'rejected')
        with self.assertRaises(ValueError):merge_node(self.store,self.owner,key)

    def test_parent_cancellation_fences_late_worker_publication(self):
        key=self.store.submit('planner','Plan');node=self.store.claim()
        with closing(self.store.connect()) as conn,conn:conn.execute("UPDATE tasks SET state='cancelled' WHERE owner=? AND id='root'",(self.owner,))
        with self.assertRaises(RuntimeError):self.store.publish(node,{'success':True,'verified':False})
        self.store.recover();self.assertEqual(self.store.get(self.owner,key)['state'],'cancel_requested')

    def test_uncertain_preparation_can_be_abandoned_after_inspection(self):
        key=self.store.submit('coder','Create result');node=self.store.claim()
        self.store.fail(node,'needs_resolution','RecoveryRequired')
        note=human_agent_command('/agents root abandon '+key+' Inspected parent and retained worktree')
        self.assertIn('Abandoned',note)
        previous=self.store.get(self.owner,key)
        self.assertEqual(previous['state'],'conflict')
        replacement=self.store.submit('coder','Create result',previous=previous)
        self.assertNotEqual(replacement,key)

    def test_merge_io_failure_retains_evidence_without_overwriting_parent(self):
        key=self.store.submit('coder','Create result');self.service.start()
        self.assertEqual(self.until(key,{'reviewed','failed','needs_input'})['state'],'reviewed')
        with patch('agent_execution.coordinator.os.replace',side_effect=OSError('fixture')),self.assertRaises(OSError):merge_node(self.store,self.owner,key)
        self.assertFalse((self.repo/'result.txt').exists())
        self.assertEqual(self.store.get(self.owner,key)['state'],'needs_resolution')
        with closing(self.store.connect()) as conn:
            event=conn.execute("SELECT data FROM agent_events WHERE node=? AND event='merge_started'",(key,)).fetchone()
        self.assertTrue(Path(json.loads(event['data'])['archive'],'before.zip').exists())

    def test_cancellation_kills_real_worker_command_and_does_not_restart(self):
        key=self.store.submit('coder','Run long');self.service.start()
        end=time.monotonic()+30;pid_file=None
        while time.monotonic()<end:
            node=self.store.get(self.owner,key)
            child=self.store.child_task(node)
            if child:
                worktrees=WorktreeStore(node['payload']['repository'],node['payload']['storage'])
                path=worktrees.location(node_task_owner(node['payload']),'subagent:'+key)/'worker.pid'
                if path.exists():pid_file=path;break
            time.sleep(.05)
        self.assertIsNotNone(pid_file,self.store.get(self.owner,key))
        pid=int(pid_file.read_text());self.store.cancel(self.owner,key)
        self.assertEqual(self.until(key,{'cancelled','failed','needs_resolution'})['state'],'cancelled')
        if os.name=='nt':
            result=__import__('subprocess').run(['tasklist','/FI',f'PID eq {pid}','/NH'],capture_output=True,text=True)
            self.assertNotIn(str(pid),result.stdout)
        else:
            with self.assertRaises(ProcessLookupError):os.kill(pid,0)
        self.assertIsNone(self.store.claim())


if __name__=='__main__':unittest.main()
