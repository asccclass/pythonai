from contextlib import closing
import base64
import hashlib
import hmac
import json
import os
import http.client
from http.server import ThreadingHTTPServer
import threading
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from nacl.signing import SigningKey
from communication_adapters.discord_adapter import DiscordAdapter
from communication_adapters.line_adapter import LineAdapter
from communication_adapters.telegram_adapter import TelegramAdapter
from communication_models import OutboundMessage
from communication_server import MultiWebhookService, create_asgi_app, create_request_handler
from communication_store import CommunicationStore
from communication_worker import CommunicationWorker, enqueue_adapter_events
from telegram_polling_worker import TelegramPollingWorker


class WebhookSecurityTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"COMM_WEBHOOK_MODE": "production", "COMM_WEBHOOK_MAX_AGE_SECONDS": "300"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CommunicationStore(Path(self.temp.name) / "comm.db")
        self.key = SigningKey.generate()

    def discord(self, payload, timestamp=None):
        body = json.dumps(payload).encode()
        timestamp = str(int(time.time()) if timestamp is None else timestamp)
        headers = {"X-Signature-Timestamp": timestamp,
                   "X-Signature-Ed25519": self.key.sign(timestamp.encode() + body).signature.hex()}
        return headers, body

    def payload(self):
        return {"type": 2, "id": "12345", "application_id": "9876", "token": "interaction-secret",
                "channel_id": "456", "user": {"id": "123"},
                "data": {"name": "ask", "options": [{"value": "hello"}]}}

    def test_missing_secrets_or_invalid_mode_reject(self):
        for mode in ("production", "unknown"):
            with patch.dict(os.environ, {"COMM_WEBHOOK_MODE": mode}):
                for adapter in (TelegramAdapter(webhook_secret=""), LineAdapter(channel_secret=""), DiscordAdapter(public_key="")):
                    self.assertFalse(adapter.verify_request({}, b"{}"))
                    with self.assertRaises(PermissionError):
                        enqueue_adapter_events(self.store, adapter, {}, b"{}")
        self.assertEqual(self.store.pending_commands(), [])

    def test_explicit_development_bypass(self):
        with patch.dict(os.environ, {"COMM_WEBHOOK_MODE": "development"}):
            self.assertTrue(TelegramAdapter(webhook_secret="").verify_request({}, b"{}"))
            self.assertFalse(TelegramAdapter(webhook_secret="configured").verify_request({}, b"{}"))

    def test_real_ed25519_signature_tamper_timestamp_and_missing_dependency(self):
        adapter = DiscordAdapter(public_key=self.key.verify_key.encode().hex())
        headers, body = self.discord(self.payload())
        self.assertTrue(adapter.verify_request(headers, body))
        self.assertFalse(adapter.verify_request(headers, body + b" "))
        old_headers, old_body = self.discord(self.payload(), int(time.time()) - 400)
        self.assertFalse(adapter.verify_request(old_headers, old_body))
        future_headers, future_body = self.discord(self.payload(), int(time.time()) + 60)
        self.assertFalse(adapter.verify_request(future_headers, future_body))
        with patch.dict("sys.modules", {"nacl.signing": None}):
            self.assertFalse(adapter.verify_request(headers, body))

    def test_discord_deferred_ping_dedup_and_persisted_followup(self):
        sent = []
        adapter = DiscordAdapter(public_key=self.key.verify_key.encode().hex(), http_post=lambda *args: sent.append(args), http_patch=lambda *args: sent.append(args))
        service = MultiWebhookService(self.store, {"discord": adapter}, CommunicationWorker(self.store, {"discord": adapter}, lambda command: "answer"))
        with patch("communication_server.scheduler_worker"), patch.object(service, "start_worker_loop"), patch.object(service, "stop_worker_loop"), TestClient(create_asgi_app(service)) as client:
            ping_headers, ping = self.discord({"type": 1})
            self.assertEqual(client.post("/webhooks/discord", content=ping, headers=ping_headers).json(), {"type": 1})
            headers, body = self.discord(self.payload())
            start = time.monotonic()
            response = client.post("/webhooks/discord", content=body, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"type": 5})
            self.assertLess(time.monotonic() - start, 3)
            self.assertEqual(client.post("/webhooks/discord", content=body, headers=headers).json(), {"type": 5})
        # Metadata survives store reconstruction and is propagated only to the adapter.
        restarted = CommunicationStore(self.store.db_path)
        with closing(restarted.connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_command_jobs").fetchone()[0], 1)
        restarted_worker = CommunicationWorker(restarted, {"discord": adapter}, lambda command: "answer")
        restarted_worker.process_next()
        restarted_worker.drain(3)
        self.assertEqual(len(sent), 1)
        self.assertIn("/webhooks/9876/interaction-secret", sent[0][0])
        self.assertTrue(sent[0][0].endswith("/messages/@original"))
        self.assertEqual(sent[0][1]["content"], "answer")
        self.assertEqual(sent[0][2], {})

    def test_line_signature_expiry_and_redelivery_dedup(self):
        adapter = LineAdapter(channel_secret="secret")
        payload = {"events": [{"type": "message", "timestamp": int(time.time() * 1000),
            "webhookEventId": "event-1", "source": {"userId": "u1"}, "message": {"type": "text", "id": "m1", "text": "hello"}}]}
        body = json.dumps(payload).encode()
        signature = base64.b64encode(hmac.new(b"secret", body, hashlib.sha256).digest()).decode()
        headers = {"X-Line-Signature": signature}
        self.assertFalse(adapter.verify_request(headers, body + b" "))
        self.assertEqual(len(enqueue_adapter_events(self.store, adapter, headers, body)), 1)
        self.assertEqual(enqueue_adapter_events(self.store, adapter, headers, body), [])
        payload["events"][0]["timestamp"] = 1
        with self.assertRaises(PermissionError):
            adapter.parse_events({}, json.dumps(payload).encode())

    def test_telegram_stale_event_and_authenticated_polling_separate_from_webhook(self):
        payload = {"update_id": 1, "message": {"message_id": 1, "date": int(time.time()),
                "from": {"id": 123}, "chat": {"id": 456}, "text": "hello"}}
        adapter = TelegramAdapter(bot_token="token", webhook_secret="", http_get=lambda *args: {"ok": True, "result": [payload]})
        self.assertFalse(adapter.verify_request({}, json.dumps(payload).encode()))
        worker = TelegramPollingWorker(self.store, adapter, CommunicationWorker(self.store, {"telegram": Mock()}, lambda command: "answer"))
        self.assertEqual(worker.poll_once()["queued"], 1)
        self.assertEqual(worker.poll_once()["queued"], 0)
        payload["message"]["date"] = 1
        with self.assertRaises(PermissionError):
            adapter.parse_events({}, json.dumps(payload).encode())

    def test_discord_expired_token_uses_channel_fallback_and_chunks(self):
        send = Mock(return_value={})
        adapter = DiscordAdapter(bot_token="bot", http_post=send)
        adapter.send_message(OutboundMessage("discord", "456", "x" * 2100,
            reply_context={"token": "old", "application_id": "9876", "received_at": time.time() - 900}))
        self.assertEqual(send.call_count, 2)
        self.assertIn("/channels/456/messages", send.call_args.args[0])
        self.assertEqual(send.call_args.args[2]["Authorization"], "Bot bot")

    def test_discord_job_cannot_start_before_deferred_response(self):
        adapter = DiscordAdapter(public_key=self.key.verify_key.encode().hex())
        headers, body = self.discord(self.payload())
        commands = enqueue_adapter_events(self.store, adapter, headers, body)
        self.assertEqual(len(commands), 1)
        self.assertEqual(self.store.pending_commands(), [])
        self.store.release_discord_interaction("12345")
        self.assertEqual(len(self.store.pending_commands()), 1)

    def test_platform_failure_not_silently_reported_as_delivery(self):
        for adapter, platform in ((TelegramAdapter(bot_token="token", http_post=lambda *args: {"ok": False}), "telegram"),
            (LineAdapter(channel_access_token="token", http_post=lambda *args: {"error": "rejected"}), "line"),
            (DiscordAdapter(bot_token="token", http_post=lambda *args: b"invalid"), "discord")):
            with self.assertRaises(RuntimeError):
                adapter.send_message(OutboundMessage(platform, "456", "hello"))

    def test_line_push_long_messages_are_bounded(self):
        calls = []
        adapter = LineAdapter(channel_access_token="token", http_post=lambda *args: calls.append(args))
        adapter.send_message(OutboundMessage("line", "u1", "x" * 5100))
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(len(call[1]["messages"][0]["text"]) <= 5000 for call in calls))
        self.assertEqual(calls[0][0], "https://api.line.me/v2/bot/message/push")

    def test_native_http_discord_ack_releases_job(self):
        adapter = DiscordAdapter(public_key=self.key.verify_key.encode().hex())
        service = MultiWebhookService(self.store, {"discord": adapter}, CommunicationWorker(self.store, {}, Mock()))
        server = ThreadingHTTPServer(("127.0.0.1", 0), create_request_handler(service))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            headers, body = self.discord(self.payload())
            start = time.monotonic()
            connection.request("POST", "/webhooks/discord", body, headers)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"type": 5})
            self.assertLess(time.monotonic() - start, 3)
            # The transport releases only after writing the response; wait for that small race.
            for _ in range(100):
                if self.store.pending_commands():
                    break
                threading.Event().wait(.01)
            self.assertEqual(len(self.store.pending_commands()), 1)
        finally:
            connection.close()
            server.shutdown()
            thread.join(5)
            server.server_close()
