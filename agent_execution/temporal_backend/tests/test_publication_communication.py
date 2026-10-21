"""Communication final/wait publication uses the original durable platform outbox."""
from contextlib import closing
import json
from unittest.mock import Mock

import pytest

from agent_execution import execution_scope
from agent_execution.policy import current_context
from agent_execution.tasks import task_owner
from agent_execution.temporal_backend.store import Store, canonical
from agent_execution.temporal_backend.config import Settings
from agent_execution.temporal_backend.context import execution_context
from agent_execution.temporal_backend.contracts import READ_TOOLS
from agent_execution.temporal_backend import publication as pub
from agent_execution.temporal_backend.communication_publication import event_key
from communication_models import InboundMessage
from communication_store import CommunicationStore
from communication_worker import CommunicationWorker


@pytest.fixture
def communication(tmp_path,monkeypatch):
    monkeypatch.setenv('AGENT_TASK_DB',str(tmp_path/'tasks.db'))
    monkeypatch.setenv('AGENT_TENANT_ROOT',str(tmp_path/'tenants'))
    monkeypatch.setenv('AGENT_WORKSPACE_ROOT',str(tmp_path))
    monkeypatch.setenv('AGENT_TASK_WORKTREE','0')
    comm = CommunicationStore(tmp_path/'communication.db')
    message = InboundMessage('telegram','one','chat','alice','read data')
    command,_ = comm.ingest_inbound_message(message)
    with execution_scope(source='communication',identity='telegram:alice',session=command.session,
                         project=command.project,task_id=command.command_id,workspace=tmp_path,communication_db=comm.db_path):
        store = Store()
        owner = task_owner(current_context())
        store.bind(owner,command.command_id,'temporal')
        context = {key:getattr(current_context(),key) for key in ('source','identity','session','project','conversation')}
        target = {'kind':'communication','path':str(comm.db_path.resolve()),'owner':owner,'ref':command.command_id}
        context.update(workspace=str(tmp_path),allowed_tools=sorted(READ_TOOLS),communication_db=str(comm.db_path.resolve()),
            publication={'version':1,'episode':None,'memory_db':None,'review':False,'delivery':target})
        row = store.submit(owner,command.command_id,[{'role':'user','content':message.text}],context,'admin',Settings.load())
        row = store.claim(row['id'],Settings.load())
        yield store,row,comm,command


def test_worker_final_outbox_and_foreground_reply_share_one_delivery(communication):
    store,row,comm,command = communication
    with closing(store.tasks.connect()) as conn,conn:
        conn.execute("UPDATE tasks SET state='completed' WHERE owner=? AND id=?",(row['owner'],row['task_id']))
    store.finish(row['id'],row['fence'],'completed',{'reply':'one final','messages':[]})
    pub.publish(store,store.get(row['id']))
    assert len(comm.pending_deliveries())==1
    key = event_key(command,'one final')
    adapter = Mock()
    runner = Mock()
    runner.task_store_enabled = True
    worker = CommunicationWorker(comm,{'telegram':adapter},runner)
    worker._send_reply(command,'one final')
    adapter.send_message.assert_called_once()
    # Idle polling sends Worker-created outbox rows, but never re-sends sent deliveries.
    worker.process_next()
    adapter.send_message.assert_called_once()
    assert comm.command_by_id(command.command_id).status=='completed'
    with closing(comm.connect()) as conn:
        assert conn.execute('SELECT count(*) FROM comm_deliveries').fetchone()[0]==1
        assert conn.execute('SELECT id FROM comm_deliveries').fetchone()[0].endswith(key)


def test_communication_identity_key_survives_delivery_expiry(communication):
    store,row,comm,command = communication
    key = 'a'*64
    delivery = comm.queue_delivery(command,'result',{},event_key=key)
    with closing(comm.connect()) as conn,conn:
        conn.execute('DELETE FROM comm_deliveries')
    assert comm.queue_delivery(command,'result',{},event_key=key)==delivery
    assert not comm.pending_deliveries()
    with pytest.raises(ValueError):
        comm.queue_delivery(command,'different',{},event_key=key)


def test_platform_unknown_send_is_not_retried(communication):
    store,row,comm,command = communication
    comm.complete_command(command.command_id,'ready')
    comm.queue_delivery(command,'result',{},event_key='b'*64)
    adapter = Mock()
    adapter.send_message.side_effect = TimeoutError('platform outcome unknown')
    worker = CommunicationWorker(comm,{'telegram':adapter},Mock())
    worker.process_next()
    worker.process_next()
    adapter.send_message.assert_called_once()
    with closing(comm.connect()) as conn:
        assert conn.execute('SELECT state FROM comm_deliveries').fetchone()[0]=='uncertain'
