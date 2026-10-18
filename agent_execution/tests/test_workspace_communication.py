from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock,patch

from agent_execution.policy import execution_scope
from agent_execution.workspace_lifecycle import LifecycleStore
from agent_execution.workspace_lifecycle.communication import DeletedConversation
from agent_runtime import AgentRuntime,AgentTurnResult
from communication_models import InboundMessage,InboundAttachment
from communication_store import CommunicationStore
from communication_worker import CommunicationWorker,agent_runtime_command_runner


@unittest.skipUnless(os.name=='nt','Windows native cleanup')
class CommunicationCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name);self.root=self.home/'workspace';self.root.mkdir()
        roles=self.home/'roles.json';roles.write_text('{"telegram:alice":"admin","telegram:bob":"admin"}')
        env=patch.dict(os.environ,{'AGENT_TASK_DB':str(self.home/'tasks.db'),
            'AGENT_APPROVAL_DB':str(self.home/'approvals.db'),'COMMUNICATION_DB_PATH':str(self.home/'comm.db'),
            'AGENT_TENANT_ROOT':str(self.home/'tenants'),'SCHEDULER_DB_PATH':str(self.home/'schedule.db'),
            'AGENT_TRACE_DB':str(self.home/'traces.db'),'COMPANION_EVENT_DB':str(self.home/'events.db'),
            'AGENT_PROJECT_WORKSPACES':json.dumps({'default':str(self.root)}),'ROLES_CONFIG':str(roles),'AGENT_WORKSPACE_CLEANUP':'1'})
        env.start();self.addCleanup(env.stop)
        self.store=CommunicationStore();self.counter=0;self.sent=[]
        runtime=AgentRuntime(messages=[{'role':'system','content':'system'}],guard=Mock(),memory=None,memory_searcher=None,
            skill_matcher=None,memory_worker=None,memory_classifier_factory=None,semantic_extractor=None,procedure_matcher=None,run_agent=Mock())
        self.runner=agent_runtime_command_runner(runtime,self.store)
        adapter=Mock();adapter.send_message.side_effect=lambda message:self.sent.append(message.text)
        self.worker=CommunicationWorker(self.store,{'telegram':adapter},self.runner)

    def incoming(self,text,sender='alice',attachments=()):
        self.counter+=1
        return self.store.ingest_inbound_message(InboundMessage('telegram',str(self.counter),'group',sender,text,
            raw_payload={'private':'raw payload'},attachments=attachments))[0]

    def run_job(self,command):
        self.worker.process_next()
        deadline=time.monotonic()+15
        while self.worker._threads and time.monotonic()<deadline:time.sleep(.02)
        self.assertFalse(self.worker._threads,'Worker failed to finish')
        with closing(self.store.connect()) as conn:return conn.execute('SELECT * FROM agent_command_jobs WHERE command_id=?',(command.command_id,)).fetchone()

    def scope(self,command):
        return execution_scope(source='communication',identity=command.platform+':'+command.sender_id,project=command.project,
            session=command.session,workspace=self.root,communication_db=self.store.db_path)

    def test_real_worker_purges_owned_files_messages_attachments_and_late_results(self):
        first=self.incoming('private request',attachments=(InboundAttachment('private.txt','text/plain',b'private bytes'),))
        def create(text,runtime,attachments=None):
            LifecycleStore().create('temporary','temporary')
            (self.root/'temporary/main.py').write_text('private source')
            return AgentTurnResult('private reply',None)
        with patch('communication_worker.run_agent_turn',side_effect=create):self.run_job(first)
        other=self.incoming('other user data','bob');self.store.complete_command(other.command_id,'keep')
        self.store.record_outbound_message('telegram','group','unprovable legacy reply','legacy')
        self.store.queue_delivery(first,'private pending',{})
        preview=self.incoming('/session-delete preview')
        frozen=json.loads(self.run_job(preview)['result_text'])
        self.assertFalse(frozen['blocking_tasks'])
        self.assertEqual(frozen['transports'][0]['legacy_unmanaged_messages'],1)
        confirm=self.incoming('/session-delete confirm '+frozen['plan_id'])
        self.run_job(confirm)
        self.assertEqual(self.store.lifecycle_state(first),'deleted')
        self.assertFalse((self.root/'temporary').exists())
        self.assertEqual(json.loads(self.sent[-1])['state'],'deleted')
        self.store.complete_command(first.command_id,'late private result')
        self.store.suspend_command(first.command_id,'late checkpoint','tool')
        with self.assertRaises(DeletedConversation):self.incoming('late new private input')
        with closing(self.store.connect()) as conn:
            row=conn.execute('SELECT text,result_text,state,status FROM agent_command_jobs WHERE command_id=?',(first.command_id,)).fetchone()
            self.assertEqual(tuple(row),('','',None,'deleted'))
            self.assertEqual(conn.execute('SELECT SUM(size) FROM comm_attachments').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT text FROM agent_command_jobs WHERE command_id=?',(other.command_id,)).fetchone()[0],'other user data')
            self.assertFalse(conn.execute("SELECT 1 FROM comm_messages WHERE owner_sender='alice' AND (text LIKE '%private%' OR raw_payload LIKE '%private%')").fetchone())
            self.assertEqual(conn.execute("SELECT text FROM comm_messages WHERE platform_message_id='legacy'").fetchone()[0],'unprovable legacy reply')
        status=self.incoming('/session-delete status '+frozen['plan_id']);self.run_job(status)
        self.assertEqual(json.loads(self.sent[-1])['state'],'deleted')
        new=self.incoming('/new-session');self.run_job(new)
        self.assertNotEqual(new.session,first.session)
        duplicate,inserted=self.store.ingest_inbound_message(InboundMessage('telegram',new.command_id.split(':',1)[1],'group','alice','/new-session'))
        self.assertFalse(inserted);self.assertEqual(duplicate.session,new.session)
        old_status=self.incoming('/session-delete status '+frozen['plan_id'])
        self.assertEqual(old_status.session,first.session)
        self.run_job(old_status)
        self.assertEqual(json.loads(self.sent[-1])['state'],'deleted')
        follow=self.incoming('new request');self.assertEqual(follow.session,new.session)

    def test_live_work_and_deliveries_block_deletion_and_fence_new_inputs(self):
        first=self.incoming('running');self.store.mark_command_running(first.command_id)
        with self.scope(first):
            store=LifecycleStore();store.ensure_session()
            preview=store.preview('delete_session')
            self.assertIn('communication:'+first.command_id,preview['blocking_tasks'])
        owner=json.dumps(['communication','telegram:alice','default',first.session],separators=(',',':'))
        with self.assertRaises(ValueError):self.store.begin_session_deletion(owner)
        with self.assertRaises(DeletedConversation):self.incoming('must not be stored')
        self.store.complete_command(first.command_id,'late private result')
        self.assertEqual(self.store.lifecycle_blockers(owner),[])
        self.store.erase_session(owner)
        with closing(self.store.connect()) as conn:self.assertEqual(conn.execute('SELECT result_text FROM agent_command_jobs').fetchone()[0],'')

    def test_expired_send_does_not_restore_erased_outbound_payload(self):
        command=self.incoming('hello');self.store.complete_command(command.command_id,'done')
        key=self.store.queue_delivery(command,'private reply',{})
        self.assertTrue(self.store.claim_delivery(key))
        owner=json.dumps(['communication','telegram:alice','default',command.session],separators=(',',':'))
        with self.assertRaises(ValueError):self.store.begin_session_deletion(owner)
        with closing(self.store.connect()) as conn,conn:conn.execute('UPDATE comm_deliveries SET lease=0 WHERE id=?',(key,))
        self.store.cleanup_expired();self.store.erase_session(owner)
        self.store.finish_delivery(key,'sent')
        with closing(self.store.connect()) as conn:
            self.assertFalse(conn.execute("SELECT 1 FROM comm_messages WHERE direction='outbound'").fetchone())

    def test_foreign_owner_cannot_confirm_and_cross_project_data_is_preserved(self):
        preview=self.incoming('/session-delete preview');plan=json.loads(self.run_job(preview)['result_text'])
        foreign=self.incoming('/session-delete confirm '+plan['plan_id'],'bob')
        self.assertIn('does not belong',self.run_job(foreign)['result_text'])
        other=self.store.ingest_inbound_message(InboundMessage('telegram','other-project','group','alice','keep project',project='other'))[0]
        self.store.complete_command(other.command_id,'keep')
        confirm=self.incoming('/session-delete confirm '+plan['plan_id']);self.run_job(confirm)
        self.assertEqual(self.store.lifecycle_state(other),'active')
        self.assertEqual(self.store.command_by_id(other.command_id).text,'keep project')

    def test_restart_preserves_tombstone_and_blocks_late_payload(self):
        command=self.incoming('old data');self.store.complete_command(command.command_id,'old result')
        owner=json.dumps(['communication','telegram:alice','default',command.session],separators=(',',':'))
        self.store.begin_session_deletion(owner);self.store.erase_session(owner)
        reopened=CommunicationStore(self.store.db_path)
        self.assertEqual(reopened.lifecycle_state(command),'deleted')
        with self.assertRaises(DeletedConversation):reopened.ingest_inbound_message(InboundMessage('telegram','late','group','alice','late data'))

    def test_blocked_cleanup_reports_and_resumes_after_new_session(self):
        from agent_execution.workspace_lifecycle.native import PinnedTree
        command=self.incoming('created');self.store.complete_command(command.command_id,'done')
        with self.scope(command):
            LifecycleStore().create('interrupted','temporary')
            (self.root/'interrupted/main.py').write_text('hello')
        preview=self.incoming('/session-delete preview');plan=json.loads(self.run_job(preview)['result_text'])
        confirmation=self.incoming('/session-delete confirm '+plan['plan_id'])
        with patch.object(PinnedTree,'move',side_effect=OSError('injected occupied file')):self.run_job(confirmation)
        self.assertEqual(json.loads(self.sent[-1])['state'],'blocked')
        new=self.incoming('/new-session');self.run_job(new)
        resume=self.incoming('/session-delete resume '+plan['plan_id']);self.run_job(resume)
        self.assertEqual(json.loads(self.sent[-1])['state'],'deleted')
        self.assertFalse((self.root/'interrupted').exists())
        self.assertEqual(self.incoming('new session input').session,new.session)

    def test_plan_recovery_cannot_cross_conversations_for_same_user(self):
        preview=self.incoming('/session-delete preview');plan=json.loads(self.run_job(preview)['result_text'])
        foreign=self.store.ingest_inbound_message(InboundMessage('telegram','another-chat','elsewhere','alice','/session-delete confirm '+plan['plan_id']))[0]
        self.assertNotEqual(foreign.session,preview.session)
        self.assertIn('does not belong',self.run_job(foreign)['result_text'])
