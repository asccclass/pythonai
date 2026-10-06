import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from communication_adapters.telegram_adapter import TelegramAdapter
from communication_server import (
    TelegramWebhookService,
    create_agent_runtime,
    create_asgi_app,
    create_request_handler,
    create_telegram_service,
    parse_allowed_senders,
    print_loaded_skills,
    print_telegram_connection_status,
    run_http_server,
)
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

    def test_asgi_health_endpoint_returns_ok(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(temp_dir=temp_dir)
            with (
                patch.object(service, "start_worker_loop"),
                patch.object(service, "stop_worker_loop"),
                patch("communication_server.scheduler_worker"),
                patch("communication_server.print_telegram_connection_status"),
                TestClient(create_asgi_app(service)) as client,
            ):
                response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})

    def test_root_serves_memory_management_interface(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self._running_server(self._service(temp_dir=temp_dir)) as address:
                status, content_type, body = self._raw_request(address, "GET", "/")

        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn("Agent Memory Manager", body)

    def test_asgi_root_serves_memory_management_interface(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(temp_dir=temp_dir)
            with (
                patch.object(service, "start_worker_loop"),
                patch.object(service, "stop_worker_loop"),
                patch("communication_server.scheduler_worker"),
                patch("communication_server.print_telegram_connection_status"),
                TestClient(create_asgi_app(service)) as client,
            ):
                response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("Agent Memory Manager", response.text)

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

    def test_asgi_telegram_webhook_enqueues_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(temp_dir=temp_dir, secret="secret")
            with (
                patch.object(service, "start_worker_loop"),
                patch.object(service, "stop_worker_loop"),
                patch("communication_server.scheduler_worker"),
                patch("communication_server.print_telegram_connection_status"),
                TestClient(create_asgi_app(service)) as client,
            ):
                response = client.post(
                    "/webhooks/telegram",
                    content=json.dumps(telegram_update("hello")),
                    headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
                )
            pending = service.store.pending_commands()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"ok": True, "queued": 1})
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].text, "hello")

    def test_asgi_exposes_companion_websocket(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(temp_dir=temp_dir)
            with (
                patch.dict("os.environ", {"COMPANION_WS_TOKENS": "token-1:andy"}, clear=False),
                patch.object(service, "start_worker_loop"),
                patch.object(service, "stop_worker_loop"),
                patch("communication_server.scheduler_worker"),
                patch("communication_server.print_telegram_connection_status"),
                TestClient(create_asgi_app(service)) as client,
            ):
                with client.websocket_connect("/ws/v1") as ws:
                    ws.send_json({"type": "hello", "payload": {"token": "token-1", "device_id": "web-1"}})
                    welcome = ws.receive_json()

        self.assertEqual(welcome["type"], "welcome")

    def test_telegram_webhook_prints_received_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(temp_dir=temp_dir)
            body = json.dumps(telegram_update("hello")).encode("utf-8")

            with patch("builtins.print") as print_mock:
                result = service.handle_webhook({}, body)

        self.assertEqual(result, {"ok": True, "queued": 1})
        self.assertEqual(
            print_mock.call_args.args[0],
            "Telegram message received: conversation=456 sender=123 text=hello",
        )

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

    def test_telegram_webhook_rejects_unallowed_sender(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(temp_dir=temp_dir, allowed_senders={"telegram:999"})
            body = json.dumps(telegram_update("hello")).encode("utf-8")

            with self._running_server(service) as address:
                status, payload = self._request(address, "POST", "/webhooks/telegram", body=body)

            pending = service.store.pending_commands()

        self.assertEqual(status, 403)
        self.assertEqual(payload["ok"], False)
        self.assertEqual(pending, [])

    def test_parse_allowed_senders_returns_none_for_empty_value(self):
        self.assertIsNone(parse_allowed_senders(""))
        self.assertEqual(parse_allowed_senders("telegram:123, line:U1 "), {"telegram:123", "line:U1"})

    def test_print_loaded_skills_outputs_operational_skill_names(self):
        class Skill:
            name = "read_note"

        class Registry:
            def load_results(self):
                return [(Skill(), None), (None, "Missing skill.json")]

        with patch("builtins.print") as print_mock:
            loaded = print_loaded_skills(Registry())

        self.assertEqual(loaded, ["read_note"])
        self.assertEqual(print_mock.call_args.args[0], "Loaded operational skills: read_note")

    def test_print_telegram_connection_status_outputs_bot_info(self):
        adapter = TelegramAdapter(
            bot_token="token",
            webhook_secret="",
            http_get=lambda url, params: {"ok": True, "result": {"id": 123, "username": "agent_bot"}},
        )

        with patch("builtins.print") as print_mock:
            bot = print_telegram_connection_status(adapter)

        self.assertEqual(bot["username"], "agent_bot")
        self.assertEqual(print_mock.call_args.args[0], "Telegram connected: @agent_bot (id=123)")

    def test_print_telegram_connection_status_outputs_failure(self):
        adapter = TelegramAdapter(
            bot_token="token",
            webhook_secret="",
            http_get=lambda url, params: {"ok": False, "description": "Unauthorized"},
        )

        with patch("builtins.print") as print_mock:
            bot = print_telegram_connection_status(adapter)

        self.assertIsNone(bot)
        self.assertIn("Telegram connection check failed:", print_mock.call_args.args[0])

    def test_create_telegram_service_reads_allowed_senders_from_env(self):
        with (
            patch.dict("os.environ", {"COMM_ALLOWED_SENDERS": "telegram:123"}, clear=False),
            patch("communication_server.create_agent_runtime", return_value=object()),
            patch("communication_server.CommunicationStore"),
        ):
            service = create_telegram_service()

        self.assertEqual(service.allowed_senders, {"telegram:123"})

    def test_create_agent_runtime_uses_built_system_prompt(self):
        with (
            patch("server.build_system_prompt", return_value="system with agents"),
            patch("server.LayaGuard", return_value=object()),
            patch("server.LayaMemoryClassifier", return_value=object()),
            patch("server.safe_memory_call", return_value=None),
            patch("server.VectorMemorySearcher", return_value=object()),
            patch("server.SkillRegistry"),
            patch("server.SkillMatcher", return_value=object()),
            patch("server.LLMSemanticExtractor", return_value=object()),
            patch("server.LLMProcedureSimilarityMatcher", return_value=object()),
            patch("server.BackgroundWorkerLease.acquire", return_value=type("Lease", (), {"acquired": False})()),
            patch("server.QueueOnlyMemoryWorker", return_value=object()),
            patch("builtins.print"),
        ):
            runtime = create_agent_runtime()

        self.assertEqual(runtime.messages[0], {"role": "system", "content": "system with agents"})

    def test_run_http_server_treats_ctrl_c_as_shutdown(self):
        class Httpd:
            def __init__(self, address, handler):
                self.address = address
                self.handler = handler
                self.closed = False

            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(temp_dir=temp_dir)
            with (
                patch("communication_server.ThreadingHTTPServer", Httpd),
                patch.object(service, "start_worker_loop") as start_mock,
                patch.object(service, "stop_worker_loop") as stop_mock,
                patch("communication_server.print_telegram_connection_status") as telegram_status_mock,
                patch("builtins.print"),
            ):
                run_http_server(service, host="127.0.0.1", port=0)

        telegram_status_mock.assert_called_once_with(service.adapter)
        start_mock.assert_called_once()
        stop_mock.assert_called_once()

    def test_worker_loop_processes_pending_command(self):
        sent = []

        with tempfile.TemporaryDirectory() as temp_dir:
            store = CommunicationStore(Path(temp_dir) / "communication.db")
            adapter = TelegramAdapter(
                bot_token="token",
                webhook_secret="",
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

    def test_stop_worker_loop_drains_command_threads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = CommunicationStore(Path(temp_dir) / "communication.db")
            adapter = TelegramAdapter(
                bot_token="token",
                webhook_secret="",
                http_post=lambda url, payload: None,
            )
            done = threading.Event()

            def runner(command):
                done.set()
                return f"reply to {command.text}"

            service = TelegramWebhookService(
                store=store,
                adapter=adapter,
                worker=CommunicationWorker(store, {"telegram": adapter}, runner),
                worker_interval_seconds=0.01,
            )
            service.handle_webhook({}, json.dumps(telegram_update("hello")).encode("utf-8"))
            service.start_worker_loop()
            self.assertTrue(done.wait(2.0))
            service.stop_worker_loop()

            self.assertEqual(service.worker._threads, [])

    def _service(self, temp_dir, secret="", allowed_senders=None):
        db_path = Path(temp_dir) / "communication.db"
        store = CommunicationStore(db_path)
        adapter = TelegramAdapter(bot_token="token", webhook_secret=secret, http_post=lambda url, payload: None)
        worker = CommunicationWorker(store, {"telegram": adapter}, lambda command: "ok")
        return TelegramWebhookService(
            store=store,
            adapter=adapter,
            worker=worker,
            allowed_senders=allowed_senders,
        )

    def _running_server(self, service):
        test_case = self

        class ServerContext:
            def __enter__(self):
                self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), create_request_handler(service))
                self.thread = threading.Thread(
                    target=lambda: self.httpd.serve_forever(poll_interval=0.01),
                    daemon=True,
                )
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

    def _raw_request(self, address, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(address[0], address[1], timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            payload = response.read().decode("utf-8")
            return response.status, response.getheader("Content-Type", ""), payload
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
