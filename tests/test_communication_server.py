import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from communication_adapters.telegram_adapter import TelegramAdapter
from communication_server import TelegramWebhookService, create_request_handler
from communication_store import CommunicationStore
from communication_worker import CommunicationWorker
from http.server import ThreadingHTTPServer


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


class CommunicationServerTests(unittest.TestCase):
    def test_health_endpoint_returns_ok(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self._running_server(self._service(temp_dir=temp_dir)) as address:
                status, payload = self._request(address, "GET", "/health")

        self.assertEqual(status, 200)
        self.assertEqual(payload["ok"], True)

    def test_telegram_webhook_enqueues_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(temp_dir=temp_dir, secret="secret")
            body = json.dumps(telegram_update("hello")).encode("utf-8")

            with self._running_server(service) as address:
                status, payload = self._request(
                    address,
                    "POST",
                    "/webhooks/telegram",
                    body=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Telegram-Bot-Api-Secret-Token": "secret",
                    },
                )

            pending = service.store.pending_commands()

        self.assertEqual(status, 202)
        self.assertEqual(payload, {"ok": True, "queued": 1})
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].text, "hello")

    def test_telegram_webhook_rejects_invalid_secret(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(temp_dir=temp_dir, secret="secret")
            body = json.dumps(telegram_update("hello")).encode("utf-8")

            with self._running_server(service) as address:
                status, payload = self._request(
                    address,
                    "POST",
                    "/webhooks/telegram",
                    body=body,
                    headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
                )

        self.assertEqual(status, 403)
        self.assertEqual(payload["ok"], False)

    def test_worker_loop_processes_pending_command(self):
        sent = []

        with tempfile.TemporaryDirectory() as temp_dir:
            store = CommunicationStore(Path(temp_dir) / "communication.db")
            adapter = TelegramAdapter(
                bot_token="token",
                http_post=lambda url, payload: sent.append(payload),
            )

            def runner(command):
                return f"reply to {command.text}"

            service = TelegramWebhookService(
                store=store,
                adapter=adapter,
                worker=CommunicationWorker(store, {"telegram": adapter}, runner),
                worker_interval_seconds=0.01,
            )
            service.handle_webhook({}, json.dumps(telegram_update("hello")).encode("utf-8"))
            service.start_worker_loop()
            try:
                for _ in range(100):
                    if sent:
                        break
                    threading.Event().wait(0.01)
            finally:
                service.stop_worker_loop()

            messages = store.recent_messages(limit=10)

        self.assertEqual(sent[0]["text"], "reply to hello")
        self.assertIn("outbound", {message["direction"] for message in messages})

    def _service(self, temp_dir, secret=""):
        db_path = Path(temp_dir) / "communication.db"
        store = CommunicationStore(db_path)
        adapter = TelegramAdapter(bot_token="token", webhook_secret=secret, http_post=lambda url, payload: None)
        worker = CommunicationWorker(store, {"telegram": adapter}, lambda command: "ok")
        return TelegramWebhookService(store=store, adapter=adapter, worker=worker)

    def _running_server(self, service):
        test_case = self

        class ServerContext:
            def __enter__(self):
                self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), create_request_handler(service))
                self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
                self.thread.start()
                return self.httpd.server_address

            def __exit__(self, exc_type, exc, tb):
                self.httpd.shutdown()
                self.thread.join(timeout=2.0)
                self.httpd.server_close()
                test_case.assertFalse(self.thread.is_alive())

        return ServerContext()

    def _request(self, address, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(address[0], address[1], timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, payload
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
