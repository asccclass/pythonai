from contextlib import closing
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from agent_execution.policy import execution_scope
from agent_execution.scheduling.store import SchedulerStore,natural_cron,next_time
from agent_execution.scheduling.engine import process_due_jobs,execute_payload
from agent_execution.approvals import ApprovalRequired,ApprovalStore
from tools.manage_schedule import manage_schedule

class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        env=patch.dict(os.environ,{'SCHEDULER_DB_PATH':str(self.root/'schedule.db'),'AGENT_TASK_DB':str(self.root/'tasks.db'),
            'AGENT_APPROVAL_DB':str(self.root/'approvals.db'),'AGENT_WORKSPACE_ROOT':str(self.root),'AGENT_PROJECT_WORKSPACES':'{}'})
        env.start();self.addCleanup(env.stop);self.store=SchedulerStore()
    def due(self,kind='command',payload=None):
        key=self.store.add_job('test','* * * * *',kind,payload or {'command':['python','-c','print("verified")']},max_runs=10)
        with closing(self.store.connect()) as conn,conn:conn.execute("UPDATE scheduled_jobs SET next_run_at='2000-01-01T00:00:00+00:00' WHERE id=?",(key,))
        return self.store.get_due_jobs()[0]
    def test_taipei_clock_and_natural_grammar(self):
        self.assertEqual(natural_cron('每天 09:30'),'30 9 * * *');self.assertEqual(natural_cron('every 5 minutes'),'*/5 * * * *')
        self.assertEqual(next_time('30 9 * * *','Asia/Taipei',datetime(2026,10,10,0,0,tzinfo=timezone.utc)),'2026-10-10T01:30:00+00:00')
        for text in ['tomorrow morning','每 99 分鐘','每天 25:10']:
            with self.assertRaises(ValueError):natural_cron(text)

    def test_schedule_can_reenter_registered_program_directory(self):
        from agent_execution.workspace_lifecycle import program_directory
        from agent_execution.scheduling.engine import execute_payload
        with execution_scope(workspace=self.root):
            with program_directory('program'):
                job=self.due('command',{'command':['python','-c','print("verified")']})
            with patch('base.run_command',return_value=__import__('subprocess').CompletedProcess([],0,'verified','')) as run:
                self.assertEqual(execute_payload(job)[0],'completed')
                run.assert_called_once()
    def test_atomic_claim_and_stale_due_snapshot(self):
        job=self.due();results=[]
        threads=[threading.Thread(target=lambda token=str(i):results.append(self.store.claim_due(job,token))) for i in range(4)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(sum(value is not None for value in results),1)
        with closing(self.store.connect()) as conn:token=conn.execute('SELECT locked_by FROM scheduled_job_locks').fetchone()[0]
        self.store.complete(job,token,next(v for v in results if v),'completed','verified')
        self.assertIsNone(self.store.claim_due(job,'stale'))
    def test_old_worker_cannot_release_or_complete_new_lease(self):
        job=self.due();run=self.store.claim_due(job,'first')
        with closing(self.store.connect()) as conn,conn:conn.execute('UPDATE scheduled_job_locks SET expires_at=0')
        self.assertTrue(self.store.acquire_lock(job['id'],'replacement'))
        self.store.release_lock(job['id'],'first');self.assertTrue(self.store.alive(job['id'],'replacement'))
        self.assertFalse(self.store.complete(job,'first',run,'completed','wrong'))
    def test_crash_with_possible_write_pauses_for_human(self):
        job=self.due();self.store.claim_due(job,'crashed')
        with closing(self.store.connect()) as conn,conn:conn.execute('UPDATE scheduled_job_locks SET expires_at=0')
        self.assertIsNone(self.store.claim_due(job,'next'));self.assertFalse(self.store.get_due_jobs())
        self.assertEqual(self.store.notifications()[0]['state'],'needs_input')
    def test_watch_silence_change_completion_and_stop(self):
        job=self.due('watch',{'prompt':'inspect only'})
        for state,text in [('waiting','same'),('waiting','same'),('waiting','changed'),('completed','done')]:
            with closing(self.store.connect()) as conn,conn:conn.execute("UPDATE scheduled_jobs SET next_run_at='2000-01-01T00:00:00+00:00'")
            current=self.store.get_due_jobs()[0];run=self.store.claim_due(current,'worker')
            self.assertTrue(self.store.complete(current,'worker',run,state,text))
        self.assertEqual(len(self.store.notifications()),2);self.assertFalse(self.store.get_due_jobs())
        self.assertEqual(len(json.loads(self.store.list_jobs()[0]['history'])),4)
    def test_bounded_readonly_retries(self):
        job=self.due('watch',{'prompt':'inspect'})
        for _ in range(3):
            with closing(self.store.connect()) as conn,conn:conn.execute("UPDATE scheduled_jobs SET next_run_at='2000-01-01T00:00:00+00:00'")
            current=self.store.get_due_jobs()[0];run=self.store.claim_due(current,'worker');self.store.complete(current,'worker',run,'failed','','timeout')
        self.assertFalse(self.store.get_due_jobs())
        self.assertEqual(self.store.notifications()[0]['state'],'needs_input')
        self.assertIn('retry limit',self.store.notifications()[0]['output'])
    def test_watch_run_limit_notifies_even_when_observation_is_unchanged(self):
        job=self.due('watch',{'prompt':'inspect'})
        with closing(self.store.connect()) as conn,conn:conn.execute('UPDATE scheduled_jobs SET max_runs=2')
        for _ in range(2):
            with closing(self.store.connect()) as conn,conn:conn.execute("UPDATE scheduled_jobs SET next_run_at='2000-01-01T00:00:00+00:00'")
            current=self.store.get_due_jobs()[0];run=self.store.claim_due(current,'worker')
            self.store.complete(current,'worker',run,'waiting','unchanged observation')
        self.assertFalse(self.store.get_due_jobs());notifications=self.store.notifications()
        self.assertEqual(len(notifications),1);self.assertEqual(notifications[0]['state'],'needs_input')
        self.assertIn('run limit',notifications[0]['output']);self.assertIn('unchanged observation',notifications[0]['output'])
    def test_changed_failure_evidence_notifies_but_identical_retry_is_quiet(self):
        job=self.due('watch',{'prompt':'inspect'})
        for error in ['first failure','different failure','different failure']:
            with closing(self.store.connect()) as conn,conn:conn.execute("UPDATE scheduled_jobs SET next_run_at='2000-01-01T00:00:00+00:00'")
            current=self.store.get_due_jobs()[0];run=self.store.claim_due(current,'worker')
            self.store.complete(current,'worker',run,'failed','',error)
        notifications=self.store.notifications();self.assertEqual(len(notifications),3)
        self.assertEqual(notifications[0]['state'],'needs_input')
        self.assertEqual({row['output'] for row in notifications if row['state']=='failed'},{'first failure','different failure'})
    def test_owner_session_isolation_and_disable(self):
        job=self.due()
        with execution_scope(identity='other',session='other',project='other'):
            self.assertEqual(self.store.list_jobs(),[]);self.assertFalse(self.store.disable(job['id']));self.assertEqual(self.store.notifications(),[])
        self.assertTrue(self.store.disable(job['id']))
    def test_add_model_tool_requires_exact_one_use_human_approval(self):
        values={'action':'add','name':'watch','schedule':'每天 09:30','payload':{'prompt':'inspect'}}
        with self.assertRaises(ApprovalRequired) as raised:manage_schedule(**values)
        key=raised.exception.approval_id;ApprovalStore().decide(key,'local','local',True)
        value=json.loads(manage_schedule(**values,approval_id=key));self.assertIn('job_id',value)
        with self.assertRaises(PermissionError):manage_schedule(**values,approval_id=key)
    def test_invalid_schedule_does_not_request_approval(self):
        with self.assertRaises(ValueError):manage_schedule(action='add',name='bad',schedule='tomorrow',payload={})
    def test_legacy_remote_jobs_are_quarantined_not_exposed_to_local(self):
        job=self.due()
        with closing(self.store.connect()) as conn,conn:
            conn.execute("UPDATE scheduled_jobs SET owner=NULL,owner_identity='telegram:private' WHERE id=?",(job['id'],))
        store=SchedulerStore()
        self.assertEqual(store.list_jobs(),[]);self.assertEqual(store.get_due_jobs(),[])
    def test_real_worker_process_executes_once_and_timeout_is_uncertain(self):
        job=self.due();process_due_jobs()
        with closing(self.store.connect()) as conn:self.assertEqual(conn.execute('SELECT status,output FROM scheduled_job_runs').fetchone()['status'],'completed')
        self.assertEqual(self.store.list_jobs()[0]['run_count'],1)
        job=self.due(payload={'command':['python','-c','import time; time.sleep(30)']})
        with closing(self.store.connect()) as conn,conn:conn.execute('UPDATE scheduled_jobs SET timeout=1 WHERE id=?',(job['id'],))
        started=time.monotonic();process_due_jobs();self.assertLess(time.monotonic()-started,8)
        with closing(self.store.connect()) as conn:self.assertEqual(conn.execute('SELECT status FROM scheduled_job_runs WHERE job_id=?',(job['id'],)).fetchone()[0],'uncertain')
    def test_watch_routes_agent_readonly_and_carries_prior_observation(self):
        job=self.due('watch',{'prompt':'inspect'});job['history']=json.dumps([{'state':'waiting','summary':'last'}])
        job['occurrence']='watch-fixture'
        def fake(messages,**kwargs):
            from agent_execution.policy import current_context
            self.assertNotIn('write_file',current_context().allowed_tools);self.assertIn('last',str(messages))
            return '{"state":"waiting","summary":"same"}'
        with patch('server.run_agent',side_effect=fake):self.assertEqual(execute_payload(job),('waiting','same',''))
    def test_workspace_script_uses_only_trusted_interpreter(self):
        script=self.root/'report.py';script.write_text('print("script verified")')
        self.due('script',{'path':'report.py'});process_due_jobs()
        with closing(self.store.connect()) as conn:
            row=conn.execute('SELECT status,output FROM scheduled_job_runs').fetchone()
        self.assertEqual(row['status'],'completed');self.assertIn('script verified',row['output'])
