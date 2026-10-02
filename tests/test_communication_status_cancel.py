import pytest
from communication_models import InboundMessage, OutboundMessage, CommunicationAdapter
from communication_store import CommunicationStore
from communication_worker import CommunicationWorker

class MockAdapter(CommunicationAdapter):
    def __init__(self):
        super().__init__()
        self.sent_messages = []
        
    def send_message(self, message: OutboundMessage) -> None:
        self.sent_messages.append(message)
        
    def verify_request(self, headers, body, query): return True
    def parse_events(self, headers, body, query): return []

def test_communication_worker_status_command(tmp_path):
    temp_db = tmp_path / "test.db"
    store = CommunicationStore(temp_db)
    
    msg1 = InboundMessage(
        platform="test_platform",
        platform_message_id="msg1",
        conversation_id="conv1",
        sender_id="user1",
        text="some command",
        raw_payload={},
    )
    cmd1, _ = store.ingest_inbound_message(msg1)
    store.mark_command_running(cmd1.command_id)
    
    msg = InboundMessage(
        platform="test_platform",
        platform_message_id="msg2",
        conversation_id="conv1",
        sender_id="user1",
        text="/status",
        raw_payload={},
    )
    command, _ = store.ingest_inbound_message(msg)
    
    adapter = MockAdapter()
    worker = CommunicationWorker(
        store=store,
        adapters={"test_platform": adapter},
        command_runner=lambda c: "should not run"
    )
    
    processed = worker.process_next()
    
    assert processed is not None
    assert processed.command_id == command.command_id
    assert processed.status == "completed"
    
    assert len(adapter.sent_messages) == 1
    outbound = adapter.sent_messages[0]
    assert "Recent Jobs" in outbound.text
    assert msg1.idempotency_key[:8] in outbound.text

def test_communication_worker_cancel_command(tmp_path):
    temp_db = tmp_path / "test.db"
    store = CommunicationStore(temp_db)
    
    msg1 = InboundMessage(
        platform="test_platform",
        platform_message_id="msg1",
        conversation_id="conv1",
        sender_id="user1",
        text="some command",
        raw_payload={},
    )
    cmd1, _ = cmd1, _ = store.ingest_inbound_message(msg1)
    store.mark_command_running(cmd1.command_id)
    
    msg2 = InboundMessage(
        platform="test_platform",
        platform_message_id="msg2",
        conversation_id="conv1",
        sender_id="user1",
        text=f"/cancel {cmd1.command_id[:8]}",
        raw_payload={},
    )
    cmd2, _ = store.ingest_inbound_message(msg2)
    
    adapter = MockAdapter()
    worker = CommunicationWorker(
        store=store,
        adapters={"test_platform": adapter},
        command_runner=lambda c: "should not run"
    )
    
    processed = worker.process_next()
    assert processed is not None
    assert processed.command_id == cmd2.command_id
    assert processed.status == "completed"
    
    cancelled_job = store.command_by_id(cmd1.command_id)
    assert cancelled_job.status == "cancel_requested"
    
    assert len(adapter.sent_messages) == 1
    assert "Cancel requested" in adapter.sent_messages[0].text