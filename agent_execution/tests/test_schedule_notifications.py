from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from agent_execution.policy import execution_scope
from agent_execution.scheduling.store import SchedulerStore
from agent_execution.scheduling.notifications import NotificationDispatcher
from communication_adapters.telegram_adapter import TelegramAdapter


class ScheduleNotificationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        env=patch.dict(os.environ,{'SCHEDULER_DB_PATH':str(self.root/'schedule.db'),'AGENT_WORKSPACE_ROOT':str(self.root)})
        env.start();self.addCleanup(env.stop);self.store=SchedulerStore();self.posts=[]
        self.adapter=TelegramAdapter(bot_token='fixture',http_post=lambda url,payload:(self.posts.append(payload) or {'ok':True}))
        role=patch('agent_execution.scheduling.notifications.get_user_role',return_value='admin');role.start();self.addCleanup(role.stop)
    def notification(self,identity='telegram:alice',session='telegram:chat',project='default'):
        with execution_scope(source='communication',identity=identity,session=session,project=project):
            key=self.store.add_job('fixture','* * * * *','watch',{'prompt':'inspect'})
            job=self.store.list_jobs()[0]
            with closing(self.store.connect()) as conn,conn:self.store.notify(conn,job,'completed','完成中文')
        return key
    def test_configured_adapter_delivers_once_to_original_conversation(self):
        self.notification();dispatcher=NotificationDispatcher({'telegram':self.adapter},{'telegram:alice'},self.store)
        self.assertEqual(dispatcher.dispatch_once(),1);self.assertEqual(dispatcher.dispatch_once(),0)
        self.assertEqual(self.posts[0]['chat_id'],'chat');self.assertIn('完成中文',self.posts[0]['text'])
        with execution_scope(source='communication',identity='telegram:alice',session='telegram:chat'):
            self.assertEqual(self.store.notifications()[0]['delivery'],'sent')
    def test_concurrent_dispatch_is_atomic_and_long_output_one_chunk(self):
        self.notification()
        with closing(self.store.connect()) as conn,conn:conn.execute('UPDATE schedule_notifications SET output=?',('長'*65536,))
        dispatcher=NotificationDispatcher({'telegram':self.adapter},store=self.store)
        threads=[threading.Thread(target=dispatcher.dispatch_once) for _ in range(4)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(len(self.posts),1);self.assertLessEqual(len(self.posts[0]['text']),1800)
    def test_unknown_send_and_crashed_sender_never_replay(self):
        self.notification();dispatcher=NotificationDispatcher({'telegram':self.adapter},store=self.store)
        with patch.object(self.adapter,'send_message',side_effect=TimeoutError('private')):self.assertEqual(dispatcher.dispatch_once(),0)
        self.assertEqual(dispatcher.dispatch_once(),0);self.assertEqual(self.posts,[])
        with closing(self.store.connect()) as conn,conn:
            conn.execute("UPDATE schedule_notification_delivery SET status='sending',expires=0")
        self.assertEqual(dispatcher.dispatch_once(),0)
        with closing(self.store.connect()) as conn:self.assertEqual(conn.execute('SELECT status FROM schedule_notification_delivery').fetchone()[0],'uncertain')
    def test_revoked_role_allowlist_and_malformed_owner_cancel_without_send(self):
        self.notification()
        with patch('agent_execution.scheduling.notifications.get_user_role',return_value='viewer'):
            NotificationDispatcher({'telegram':self.adapter},store=self.store).dispatch_once()
        self.notification(identity='telegram:bob')
        NotificationDispatcher({'telegram':self.adapter},{'telegram:alice'},self.store).dispatch_once()
        self.notification(session='discord:wrong')
        NotificationDispatcher({'telegram':self.adapter},store=self.store).dispatch_once()
        self.assertEqual(self.posts,[])
        with closing(self.store.connect()) as conn:self.assertEqual({r[0] for r in conn.execute('SELECT status FROM schedule_notification_delivery')},{'cancelled'})
    def test_wrong_service_does_not_claim_and_owner_disable_cancels_pending(self):
        key=self.notification();self.assertEqual(NotificationDispatcher({},store=self.store).dispatch_once(),0)
        with execution_scope(source='communication',identity='telegram:alice',session='telegram:chat'):
            self.assertTrue(self.store.disable(key))
        self.assertEqual(NotificationDispatcher({'telegram':self.adapter},store=self.store).dispatch_once(),0);self.assertEqual(self.posts,[])
    def test_shutdown_does_not_claim_an_unsent_notification(self):
        self.notification();dispatcher=NotificationDispatcher({'telegram':self.adapter},store=self.store)
        self.assertEqual(dispatcher.dispatch_once(lambda:True),0)
        self.assertEqual(self.posts,[])
        self.assertEqual(dispatcher.dispatch_once(),1)
    def test_broken_schedule_storage_does_not_stop_communication_loop(self):
        from communication_server import MultiWebhookService
        service=MultiWebhookService(None,{},None)
        def process():service._stop_event.set();return 0
        with patch.object(service,'process_pending_once',side_effect=process) as calls,patch(
            'agent_execution.scheduling.notifications.NotificationDispatcher',side_effect=OSError('private')):
            service._worker_loop()
        calls.assert_called_once()
    def test_long_polling_dispatches_and_starts_stops_scheduler(self):
        import telegram_polling_worker as module
        from unittest.mock import Mock
        self.notification();worker=Mock();worker.process_next.return_value=None
        polling=module.TelegramPollingWorker(None,self.adapter,worker)
        self.assertEqual(polling.process_pending_once(),1);self.assertEqual(len(self.posts),1)
        polling.run_forever=Mock(side_effect=KeyboardInterrupt)
        with patch.object(module,'create_telegram_polling_worker',return_value=polling),patch.object(module,'scheduler_worker') as scheduler:
            module.main()
        scheduler.start.assert_called_once();scheduler.stop.assert_called_once()
