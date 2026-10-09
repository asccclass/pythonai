import asyncio
from contextlib import closing
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from agent_execution.delivery import CapacityError, EventStore
from communication_models import InboundAttachment, InboundMessage
from communication_store import CommunicationStore
from communication_worker import CommunicationWorker
from websocket_server import Hub


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = CommunicationStore(self.root/'comm.db')
        self.message = InboundMessage('telegram','1','chat','alice','process', attachments=(
            InboundAttachment('圖.png','image/png',b'\x89PNG\r\n\x1a\nfixture'),
            InboundAttachment('文字.txt','text/plain','繁體中文'.encode()),))

    def test_attachments_survive_actual_process_restart(self):
        command, _ = self.store.ingest_inbound_message(self.message)
        code = '''import sys,json,hashlib
from communication_store import CommunicationStore
s=CommunicationStore(sys.argv[1]); c=s.command_by_id('telegram:1',include_attachments=True)
print(json.dumps([hashlib.sha256(a.data).hexdigest() for a in c.attachments]))'''
        result = subprocess.run([sys.executable,'-c',code,str(self.store.db_path)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        import hashlib
        self.assertEqual(json.loads(result.stdout),[hashlib.sha256(a.data).hexdigest() for a in command.attachments])

    def test_attachment_owner_integrity_and_expiry_are_enforced(self):
        command, _ = self.store.ingest_inbound_message(self.message)
        for owner in (replace(command,sender_id='bob'),replace(command,project='other'),replace(command,conversation_id='other')):
            with self.assertRaises(PermissionError):
                self.store.load_attachments(command.command_id, owner)
        with closing(self.store.connect()) as conn, conn:
            conn.execute("UPDATE comm_attachments SET data=X'00' WHERE ordinal=0")
        with self.assertRaisesRegex(ValueError,'integrity'):
            self.store.load_attachments(command.command_id,command)
        with closing(self.store.connect()) as conn, conn:
            conn.execute('UPDATE comm_attachments SET expires=0')
        self.store.cleanup_expired()
        with self.assertRaisesRegex(ValueError,'expired'):
            self.store.load_attachments(command.command_id,command)
        with closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute('SELECT SUM(length(data)) FROM comm_attachments').fetchone()[0],0)

    def test_invalid_and_oversized_attachments_leave_no_half_ingested_job(self):
        for attachment in (InboundAttachment('../secret','text/plain',b'x'),
                           InboundAttachment('x','invalid',b'x'),InboundAttachment('x','text/plain',b'xx')):
            with patch.dict(os.environ,{'COMM_ATTACHMENT_MAX_BYTES':'1'}):
                with self.assertRaises((CapacityError,ValueError)):
                    self.store.ingest_inbound_message(replace(self.message,attachments=(attachment,)))
        self.assertEqual(self.store.recent_messages(),[])
        self.assertEqual(self.store.pending_commands(),[])

    def test_worker_reads_persisted_file_and_does_not_repeat_uncertain_delivery(self):
        command, _ = self.store.ingest_inbound_message(self.message)
        runner = Mock(return_value='done')
        adapter = Mock()
        adapter.send_message.side_effect = TimeoutError('ambiguous transport response')
        worker = CommunicationWorker(CommunicationStore(self.store.db_path),{'telegram':adapter},runner)
        worker.process_next()
        worker.drain()
        self.assertEqual(runner.call_args.args[0].attachments,self.message.attachments)
        self.assertEqual(self.store.command_by_id(command.command_id).status,'completed')
        self.assertEqual(self.store.delivery_status(command)[0]['state'],'uncertain')
        worker.deliver_pending()
        adapter.send_message.assert_called_once()

    def test_pending_delivery_survives_restart_and_claim_is_atomic(self):
        command, _ = self.store.ingest_inbound_message(self.message)
        key = self.store.queue_delivery(command,'done',{})
        second = CommunicationStore(self.store.db_path)
        self.assertTrue(second.claim_delivery(key))
        self.assertFalse(self.store.claim_delivery(key))
        with closing(self.store.connect()) as conn, conn:
            conn.execute('UPDATE comm_deliveries SET lease=0')
        second.cleanup_expired()
        self.assertEqual(second.delivery_status(command)[0]['state'],'uncertain')
        self.assertEqual(second.pending_deliveries(),[])
        other = replace(command,command_id='other')
        second.queue_delivery(other,'reply',{})
        adapter=Mock()
        worker=CommunicationWorker(CommunicationStore(self.store.db_path),{'telegram':adapter},Mock())
        worker.deliver_pending()
        adapter.send_message.assert_called_once()
        self.assertEqual(second.delivery_status(other)[0]['state'],'sent')

    def test_websocket_restart_replay_ack_owner_and_input_deduplication(self):
        path=self.root/'events.db'
        first=Hub(EventStore(path))
        session=first.get('device',user_id='alice')
        event=asyncio.run(session.send('notify',{'text':'private'}))
        self.assertIsNone(first.event_store.accept_input(session.owner,'request','write once'))
        second=Hub(EventStore(path))
        restarted=second.get('device',user_id='alice')
        self.assertEqual(list(restarted.outbox),[event])
        self.assertEqual(second.get('device',user_id='bob').seq,0)
        self.assertEqual(second.get('device',user_id='alice',project='other').seq,0)
        self.assertEqual(second.event_store.accept_input(session.owner,'request','write once'),'uncertain')
        with self.assertRaises(ValueError):
            second.event_store.accept_input(session.owner,'request','different')
        with self.assertRaises(ValueError):
            restarted.ack(999)
        restarted.ack(1)
        third=Hub(EventStore(path)).get('device',user_id='alice')
        self.assertEqual(list(third.outbox),[])
        self.assertEqual(asyncio.run(third.send('notify',{'text':'next'}))['seq'],2)

    def test_capacity_rejects_without_evicting_and_expiry_records_gap(self):
        store=EventStore(self.root/'events.db')
        owner=store.owner('alice','default','device')
        with patch.dict(os.environ,{'COMPANION_OUTBOX_LIMIT':'1'}):
            original=store.append(owner,{'type':'notify','payload':{'text':'one'}})
            with self.assertRaises(CapacityError):
                store.append(owner,{'type':'notify','payload':{'text':'two'}})
        self.assertEqual(store.snapshot(owner)[2],[original])
        with closing(store.connect()) as conn, conn:
            conn.execute('UPDATE ws_events SET expires=0')
        self.assertEqual(store.snapshot(owner),(1,1,[]))
        self.assertEqual(store.append(owner,{'type':'notify','payload':{}})['seq'],2)

    def test_replayed_chat_id_never_invokes_runtime_twice(self):
        from websocket_server import handle_client_message
        async def check():
            session=Hub(EventStore(self.root/'events.db')).get('device',user_id='alice')
            session.event_store.accept_input(session.owner,'request','write once')
            with patch('websocket_server.handle_chat') as run:
                await handle_client_message(session,{'type':'chat.send','id':'request','payload':{'text':'write once'}})
                run.assert_not_called()
                self.assertEqual(session.outbox[-1]['payload']['status'],'uncertain')
        asyncio.run(check())
