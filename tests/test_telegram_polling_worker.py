import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from communication_adapters.telegram_adapter import TelegramAdapter
from communication_store import CommunicationStore
from communication_worker import CommunicationWorker
from telegram_polling_worker import TelegramPollingWorker, create_telegram_polling_worker


def telegram_update(text="hello", update_id=100, message_id=7, sender_id=123):
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": {"id": sender_id, "is_bot": False, "first_name": "Ada"},
            "chat": {"id": 456, "type": "private"},
            "date": 1,
            "text": text,
        },
    }


class TelegramPollingWorkerTests(unittest.TestCase):
    def test_adapter_get_updates_calls_telegram_api(self):
        calls = []

        def http_get(url, params):
            calls.append((url, params))
            return {"ok": True, "result": [telegram_update()]}

        adapter = TelegramAdapter(bot_token="token", webhook_secret="", http_get=http_get)

        updates = adapter.get_updates(offset=10, timeout=5, limit=2)

        self.assertEqual(len(updates), 1)
        self.assertIn("/bottoken/getUpdates", calls[0][0])
        self.assertEqual(calls[0][1]["offset"], 10)
        self.assertEqual(calls[0][1]["timeout"], 5)
        self.assertEqual(calls[0][1]["limit"], 2)

    def test_poll_once_enqueues_and_processes_updates(self):
        sent = []
        updates = [[telegram_update("hello", update_id=100)]]

        with tempfile.TemporaryDirectory() as temp_dir:
            store = CommunicationStore(Path(temp_dir) / "communication.db")
            adapter = TelegramAdapter(
                bot_token="token",
                webhook_secret="",
                http_get=lambda url, params: {"ok": True, "result": updates.pop(0)},
                http_post=lambda url, payload: sent.append(payload),
            )

            def runner(command):
                return f"reply to {command.text}"

            worker = TelegramPollingWorker(
                store=store,
                adapter=adapter,
                worker=CommunicationWorker(store, {"telegram": adapter}, runner),
                poll_timeout_seconds=1,
            )

            result = worker.poll_once()
            messages = store.recent_messages(limit=10)

        self.assertEqual(result, {"updates": 1, "queued": 1, "processed": 1})
        self.assertEqual(worker.offset, 101)
        self.assertEqual(sent[0]["text"], "reply to hello")
        self.assertIn("outbound", {message["direction"] for message in messages})

    def test_poll_once_enforces_allowed_senders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = CommunicationStore(Path(temp_dir) / "communication.db")
            adapter = TelegramAdapter(
                bot_token="token",
                webhook_secret="",
                http_get=lambda url, params: {"ok": True, "result": [telegram_update(sender_id=123)]},
            )
            worker = TelegramPollingWorker(
                store=store,
                adapter=adapter,
                worker=CommunicationWorker(store, {"telegram": adapter}, lambda command: "ok"),
                allowed_senders={"telegram:999"},
            )

            with self.assertRaises(PermissionError):
                worker.poll_once()

            self.assertEqual(store.pending_commands(), [])

    def test_create_polling_worker_reads_env(self):
        with (
            patch.dict(
                "os.environ",
                {
                    "COMM_ALLOWED_SENDERS": "telegram:123",
                    "TELEGRAM_POLL_TIMEOUT": "3",
                    "TELEGRAM_POLL_IDLE_SLEEP": "0.05",
                    "TELEGRAM_POLL_ERROR_SLEEP": "0.5",
                },
                clear=False,
            ),
            patch("telegram_polling_worker.create_agent_runtime", return_value=object()),
            patch("telegram_polling_worker.CommunicationStore"),
        ):
            worker = create_telegram_polling_worker()

        self.assertEqual(worker.allowed_senders, {"telegram:123"})
        self.assertEqual(worker.poll_timeout_seconds, 3)
        self.assertEqual(worker.idle_sleep_seconds, 0.05)
        self.assertEqual(worker.error_sleep_seconds, 0.5)

    def test_run_forever_retries_transient_polling_http_errors(self):
        calls = []
        sleeps = []

        with tempfile.TemporaryDirectory() as temp_dir:
            store = CommunicationStore(Path(temp_dir) / "communication.db")
            adapter = TelegramAdapter(bot_token="token", webhook_secret="")
            worker = TelegramPollingWorker(
                store=store,
                adapter=adapter,
                worker=CommunicationWorker(store, {"telegram": adapter}, lambda command: "ok"),
                error_sleep_seconds=0.25,
                sleep=lambda seconds: sleeps.append(seconds),
            )

            def poll_once():
                calls.append("poll")
                if len(calls) == 1:
                    raise HTTPError("https://api.telegram.org", 502, "Bad Gateway", hdrs=None, fp=None)
                raise KeyboardInterrupt

            worker.poll_once = poll_once
            with patch("builtins.print") as print_mock:
                with self.assertRaises(KeyboardInterrupt):
                    worker.run_forever()

        self.assertEqual(calls, ["poll", "poll"])
        self.assertEqual(sleeps, [0.25])
        self.assertIn("Telegram polling warning:", print_mock.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
