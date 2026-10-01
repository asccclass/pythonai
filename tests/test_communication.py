import json
from types import SimpleNamespace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from communication_adapters.telegram_adapter import TelegramAdapter, split_telegram_message
from communication_models import AgentCommand, OutboundMessage
from communication_store import CommunicationStore
from communication_worker import CommunicationWorker, agent_runtime_command_runner, enqueue_adapter_events


def telegram_update(text="hello", update_id=100, message_id=7):
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": {"id": 123, "is_bot": False, "first_name": "Ada"},
            "chat": {"id": 456, "type": "private"},
            "date": 1,
            "text": text,
        },
    }


class CommunicationTests(unittest.TestCase):
    def test_telegram_adapter_parses_text_message(self):
        adapter = TelegramAdapter(bot_token="token", webhook_secret="")
        body = json.dumps(telegram_update("run status")).encode("utf-8")

        events = adapter.parse_events({}, body)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].platform, "telegram")
        self.assertEqual(events[0].platform_message_id, "100:7")
        self.assertEqual(events[0].conversation_id, "456")
        self.assertEqual(events[0].sender_id, "123")
        self.assertEqual(events[0].text, "run status")

    def test_telegram_adapter_ignores_non_text_updates(self):
        adapter = TelegramAdapter(bot_token="token", webhook_secret="")
        payload = telegram_update()
        del payload["message"]["text"]

        events = adapter.parse_events({}, json.dumps(payload).encode("utf-8"))

        self.assertEqual(events, [])

    def test_telegram_adapter_verifies_optional_secret_header(self):
        adapter = TelegramAdapter(bot_token="token", webhook_secret="secret")

        self.assertTrue(adapter.verify_request({"X-Telegram-Bot-Api-Secret-Token": "secret"}, b"{}"))
        self.assertFalse(adapter.verify_request({"X-Telegram-Bot-Api-Secret-Token": "wrong"}, b"{}"))

    def test_telegram_adapter_allows_explicit_empty_secret_when_env_is_set(self):
        with patch.dict("os.environ", {"TELEGRAM_WEBHOOK_SECRET": "env-secret"}):
            adapter = TelegramAdapter(bot_token="token", webhook_secret="")

        self.assertTrue(adapter.verify_request({}, b"{}"))

    def test_telegram_adapter_sends_split_messages(self):
        calls = []
        adapter = TelegramAdapter(
            bot_token="token",
            webhook_secret="",
            http_post=lambda url, payload: calls.append((url, payload)),
        )

        adapter.send_message(OutboundMessage("telegram", "456", "a" * 4100))

        self.assertEqual(len(calls), 2)
        self.assertIn("/bottoken/sendMessage", calls[0][0])
        self.assertEqual(calls[0][1]["chat_id"], "456")
        self.assertEqual(len(calls[0][1]["text"]), 4096)

    def test_split_telegram_message_keeps_short_text_single_chunk(self):
        self.assertEqual(split_telegram_message("hello", limit=10), ["hello"])

    def test_store_ingests_inbound_message_once(self):
        adapter = TelegramAdapter(bot_token="token", webhook_secret="")
        body = json.dumps(telegram_update("hello")).encode("utf-8")
        with tempfile.TemporaryDirectory() as temp_dir:
            store = CommunicationStore(Path(temp_dir) / "communication.db")

            first = enqueue_adapter_events(store, adapter, {}, body)
            second = enqueue_adapter_events(store, adapter, {}, body)
            messages = store.recent_messages(limit=10)
            pending = store.pending_commands(limit=10)

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(len(messages), 1)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].text, "hello")

    def test_enqueue_adapter_events_enforces_allowed_senders(self):
        adapter = TelegramAdapter(bot_token="token", webhook_secret="")
        body = json.dumps(telegram_update("hello")).encode("utf-8")

        with tempfile.TemporaryDirectory() as temp_dir:
            store = CommunicationStore(Path(temp_dir) / "communication.db")
            commands = enqueue_adapter_events(store, adapter, {}, body, allowed_senders={"telegram:123"})

        self.assertEqual(len(commands), 1)

        with tempfile.TemporaryDirectory() as temp_dir:
            store = CommunicationStore(Path(temp_dir) / "communication.db")
            with self.assertRaises(PermissionError):
                enqueue_adapter_events(store, adapter, {}, body, allowed_senders={"telegram:999"})

    def test_worker_runs_command_and_sends_reply(self):
        sent = []
        adapter = TelegramAdapter(bot_token="token", webhook_secret="", http_post=lambda url, payload: sent.append(payload))
        body = json.dumps(telegram_update("hello")).encode("utf-8")

        with tempfile.TemporaryDirectory() as temp_dir:
            store = CommunicationStore(Path(temp_dir) / "communication.db")
            enqueue_adapter_events(store, adapter, {}, body)

            def runner(command: AgentCommand):
                return f"reply to {command.text}"

            worker = CommunicationWorker(store, {"telegram": adapter}, runner)
            command = worker.process_next()
            messages = store.recent_messages(limit=10)

        self.assertEqual(command.status, "completed")
        self.assertEqual(sent[0]["text"], "reply to hello")
        self.assertIn("outbound", {message["direction"] for message in messages})

    def test_agent_runtime_command_runner_uses_shared_turn_flow(self):
        runtime = object()
        command = AgentCommand(
            command_id="1",
            platform="telegram",
            conversation_id="456",
            sender_id="123",
            text="hello agent",
            status="pending",
            source_message_id=7,
        )

        with patch(
            "communication_worker.run_agent_turn",
            return_value=SimpleNamespace(reply="agent reply"),
        ) as run_agent_turn:
            runner = agent_runtime_command_runner(runtime)
            reply = runner(command)

        self.assertEqual(reply, "agent reply")
        run_agent_turn.assert_called_once_with("hello agent", runtime)

    def test_enqueue_adapter_events_rejects_failed_verification(self):
        adapter = TelegramAdapter(bot_token="token", webhook_secret="secret")
        with tempfile.TemporaryDirectory() as temp_dir:
            store = CommunicationStore(Path(temp_dir) / "communication.db")
            with self.assertRaises(PermissionError):
                enqueue_adapter_events(store, adapter, {}, b"{}")


if __name__ == "__main__":
    unittest.main()
