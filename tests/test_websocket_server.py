import asyncio
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from websocket_server import (
    Session,
    app,
    handle_audio_frame,
    hub,
    reset_hub_for_tests,
)


class WebSocketServerTests(unittest.TestCase):
    def setUp(self):
        reset_hub_for_tests()

    def tearDown(self):
        reset_hub_for_tests()

    def test_hello_returns_welcome(self):
        with patch.dict("os.environ", {"COMPANION_WS_TOKENS": "token-1:andy"}, clear=False):
            with TestClient(app).websocket_connect("/ws/v1") as ws:
                ws.send_json(
                    {
                        "v": 1,
                        "type": "hello",
                        "payload": {
                            "token": "token-1",
                            "device_id": "desktop-andy-01",
                            "device_type": "desktop",
                            "last_seq": 0,
                            "capabilities": ["tool.open_url"],
                        },
                    }
                )
                message = ws.receive_json()

        self.assertEqual(message["type"], "welcome")
        self.assertEqual(message["payload"]["resumed"], True)
        self.assertIn("session_id", message["payload"])
        self.assertEqual(hub.sessions["desktop-andy-01"].user_id, "andy")

    def test_ping_returns_pong_with_ref(self):
        with patch.dict("os.environ", {"COMPANION_WS_TOKENS": "token-1"}, clear=False):
            with TestClient(app).websocket_connect("/ws/v1") as ws:
                ws.send_json({"type": "hello", "payload": {"token": "token-1", "device_id": "web-1"}})
                ws.receive_json()
                ws.send_json({"v": 1, "type": "ping", "id": "p1", "payload": {}})
                message = ws.receive_json()

        self.assertEqual(message["type"], "pong")
        self.assertEqual(message["ref"], "p1")

    def test_reconnect_replays_reliable_messages_after_last_seq(self):
        session = hub.get("desktop-1", "desktop", "andy")
        asyncio.run(session.send("notify", {"title": "one", "body": "first"}))
        asyncio.run(session.send("notify", {"title": "two", "body": "second"}))

        with patch.dict("os.environ", {"COMPANION_WS_TOKENS": "token-1:andy"}, clear=False):
            with TestClient(app).websocket_connect("/ws/v1") as ws:
                ws.send_json(
                    {
                        "type": "hello",
                        "payload": {"token": "token-1", "device_id": "desktop-1", "last_seq": 1},
                    }
                )
                welcome = ws.receive_json()
                replayed = ws.receive_json()

        self.assertEqual(welcome["type"], "welcome")
        self.assertEqual(replayed["type"], "notify")
        self.assertEqual(replayed["seq"], 2)
        self.assertEqual(replayed["payload"]["title"], "two")

    def test_ack_clears_reliable_outbox(self):
        session = hub.get("desktop-1", "desktop", "andy")
        asyncio.run(session.send("notify", {"title": "one"}))
        asyncio.run(session.send("notify", {"title": "two"}))

        with patch.dict("os.environ", {"COMPANION_WS_TOKENS": "token-1:andy"}, clear=False):
            with TestClient(app).websocket_connect("/ws/v1") as ws:
                ws.send_json({"type": "hello", "payload": {"token": "token-1", "device_id": "desktop-1"}})
                ws.receive_json()
                ws.receive_json()
                ws.receive_json()
                ws.send_json({"v": 1, "type": "ack", "payload": {"seq": 2}})
                ws.send_json({"v": 1, "type": "ping", "id": "after-ack", "payload": {}})
                self.assertEqual(ws.receive_json()["ref"], "after-ack")

        self.assertEqual(list(session.outbox), [])

    def test_chat_send_streams_start_delta_and_end(self):
        with patch.dict("os.environ", {"COMPANION_WS_TOKENS": "token-1:andy"}, clear=False):
            with TestClient(app).websocket_connect("/ws/v1") as ws:
                ws.send_json({"type": "hello", "payload": {"token": "token-1", "device_id": "web-1"}})
                ws.receive_json()
                ws.send_json({"v": 1, "type": "chat.send", "id": "c1", "payload": {"text": "你好"}})
                start = ws.receive_json()
                deltas = []
                final = None
                for _ in range(80):
                    message = ws.receive_json()
                    if message["type"] == "chat.delta":
                        deltas.append(message["payload"]["text"])
                    if message["type"] == "chat.end":
                        final = message
                        break

        self.assertEqual(start["type"], "chat.start")
        self.assertEqual(start["ref"], "c1")
        self.assertIsNotNone(final)
        self.assertIn("你好", final["payload"]["text"])
        self.assertGreater(len(deltas), 0)

    def test_event_updates_focus_and_routes_proactive_to_focused_device(self):
        with patch.dict("os.environ", {"COMPANION_WS_TOKENS": "token-1:andy"}, clear=False):
            with TestClient(app).websocket_connect("/ws/v1") as ws:
                ws.send_json({"type": "hello", "payload": {"token": "token-1", "device_id": "desktop-1", "device_type": "desktop"}})
                ws.receive_json()
                ws.send_json(
                    {
                        "v": 1,
                        "type": "event",
                        "payload": {"name": "app.focus", "data": {"focused": True}},
                    }
                )
                ws.send_json(
                    {
                        "v": 1,
                        "type": "event",
                        "payload": {"name": "user.arrived", "data": {}},
                    }
                )
                message = ws.receive_json()

        self.assertEqual(message["type"], "proactive")
        self.assertEqual(message["payload"]["reason"], "greeting")

    def test_audio_frame_parser_records_binary_metadata(self):
        session = Session("desktop-1")
        frame = bytes([0x02]) + (3).to_bytes(4, "big") + (7).to_bytes(4, "big") + b"pcm"

        handle_audio_frame(session, frame)

        self.assertEqual(session.audio_frames[0], {"kind": 2, "stream_id": 3, "seq": 7, "size": 3})


if __name__ == "__main__":
    unittest.main()
