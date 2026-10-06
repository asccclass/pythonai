from __future__ import annotations

import asyncio
from collections import deque
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from fastapi import FastAPI, WebSocket, WebSocketDisconnect


PROTOCOL_VERSION = 1
RELIABLE_TYPES = {"chat.end", "proactive", "notify", "tool.call"}
DEFAULT_FEATURES = {"stt": False, "tts": False, "tts_voices": []}


def new_id() -> str:
    return uuid.uuid4().hex


def now_ms() -> int:
    return int(time.time() * 1000)


def websocket_tokens() -> dict[str, str]:
    raw = os.environ.get("COMPANION_WS_TOKENS") or os.environ.get("COMPANION_WS_TOKEN") or "change-me:andy"
    tokens: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            token, user_id = item.split(":", 1)
        else:
            token, user_id = item, "user"
        if token:
            tokens[token] = user_id or "user"
    return tokens


@dataclass
class Session:
    device_id: str
    device_type: str = "web"
    user_id: str = "user"
    caps: set[str] = field(default_factory=set)
    ws: WebSocket | None = None
    seq: int = 0
    outbox: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=200))
    last_active: float = field(default_factory=time.time)
    focused: bool = False
    current_chat_task: asyncio.Task | None = None
    audio_frames: list[dict[str, Any]] = field(default_factory=list)
    tool_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)

    async def send(self, type_: str, payload: dict[str, Any], ref: str | None = None) -> dict[str, Any]:
        message: dict[str, Any] = {
            "v": PROTOCOL_VERSION,
            "type": type_,
            "id": new_id(),
            "ref": ref,
            "ts": now_ms(),
            "payload": payload,
        }
        if type_ in RELIABLE_TYPES:
            self.seq += 1
            message["seq"] = self.seq
            self.outbox.append(message)
        if self.ws is not None:
            try:
                await self.ws.send_text(json.dumps(message, ensure_ascii=False))
            except Exception:
                self.ws = None
        return message

    def ack(self, seq: int) -> None:
        while self.outbox and int(self.outbox[0].get("seq") or 0) <= seq:
            self.outbox.popleft()


class Hub:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.telegram_fallbacks: list[str] = []

    def get(self, device_id: str, device_type: str = "web", user_id: str = "user") -> Session:
        session = self.sessions.get(device_id)
        if session is None:
            session = Session(device_id=device_id, device_type=device_type, user_id=user_id)
            self.sessions[device_id] = session
        session.device_type = device_type or session.device_type
        session.user_id = user_id or session.user_id
        return session

    def pick_for_proactive(self) -> Session | None:
        online = [session for session in self.sessions.values() if session.ws is not None]
        if not online:
            return None
        online.sort(
            key=lambda session: (
                session.focused,
                session.device_type == "desktop",
                session.last_active,
            ),
            reverse=True,
        )
        return online[0]

    async def push_proactive(
        self,
        reason: str,
        text: str,
        emotion: str = "happy",
        priority: str = "normal",
    ) -> dict[str, Any] | None:
        payload = {"reason": reason, "text": text, "emotion": emotion, "tts": True, "priority": priority}
        session = self.pick_for_proactive()
        if session is not None:
            return await session.send("proactive", payload)
        if priority == "high":
            await send_via_telegram(text, self)
        return None


hub = Hub()
app = FastAPI(title="PythonAI Companion WebSocket")


async def send_via_telegram(text: str, target_hub: Hub = hub) -> None:
    target_hub.telegram_fallbacks.append(text)


async def brain_reply_stream(user_text: str) -> AsyncIterator[tuple[str, Any]]:
    for chunk in f"（示範）你說：{user_text}":
        await asyncio.sleep(0)
        yield "delta", chunk
    yield "end", {"emotion": "happy", "action": "nod"}


@app.websocket("/ws/v1")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    session: Session | None = None
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=5)
        hello = parse_text_message(raw)
        payload = hello.get("payload", {})
        token = payload.get("token")
        tokens = websocket_tokens()
        if hello.get("type") != "hello" or token not in tokens:
            await ws.close(code=4401)
            return
        if hello.get("v", PROTOCOL_VERSION) != PROTOCOL_VERSION:
            await ws.close(code=4400)
            return

        device_id = str(payload.get("device_id") or "").strip()
        if not device_id:
            await ws.close(code=4400)
            return
        session = hub.get(device_id, str(payload.get("device_type") or "web"), tokens[token])
        session.ws = ws
        session.caps = {str(capability) for capability in payload.get("capabilities", [])}
        last_seq = int(payload.get("last_seq") or 0)
        resumable = not session.outbox or int(session.outbox[0]["seq"]) <= last_seq + 1

        await session.send(
            "welcome",
            {
                "session_id": new_id(),
                "server_time": now_ms(),
                "resumed": resumable,
                "features": DEFAULT_FEATURES,
            },
        )
        if resumable:
            for message in list(session.outbox):
                if int(message.get("seq") or 0) > last_seq:
                    await ws.send_text(json.dumps(message, ensure_ascii=False))

        while True:
            incoming = await asyncio.wait_for(ws.receive(), timeout=60)
            if incoming.get("type") == "websocket.disconnect":
                break
            session.last_active = time.time()
            if incoming.get("bytes") is not None:
                handle_audio_frame(session, incoming["bytes"])
                continue
            text = incoming.get("text")
            if text is None:
                continue
            data = parse_text_message(text)
            await handle_client_message(session, data)
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except ValueError:
        if session is not None:
            await session.send("error", {"code": "bad_message", "message": "invalid JSON", "retryable": False})
    finally:
        if session is not None:
            session.ws = None


async def handle_client_message(session: Session, data: dict[str, Any]) -> None:
    version = data.get("v", PROTOCOL_VERSION)
    message_type = data.get("type")
    payload = data.get("payload") or {}
    message_id = data.get("id")
    if version != PROTOCOL_VERSION:
        await session.send(
            "error",
            {"code": "unsupported_version", "message": f"unsupported version {version}", "retryable": False},
            ref=message_id,
        )
        return
    if message_type == "ping":
        await session.send("pong", {}, ref=message_id)
    elif message_type == "ack":
        session.ack(int(payload.get("seq") or 0))
    elif message_type == "chat.send":
        text = str(payload.get("text") or "")
        if not text:
            await session.send("error", {"code": "bad_message", "message": "chat text is required", "retryable": False}, ref=message_id)
            return
        session.current_chat_task = asyncio.create_task(handle_chat(session, text, str(message_id or "")))
    elif message_type == "chat.interrupt":
        if session.current_chat_task is not None and not session.current_chat_task.done():
            session.current_chat_task.cancel()
        await session.send("chat.end", {"text": "", "emotion": "neutral", "action": "none", "interrupted": True}, ref=data.get("ref") or message_id)
    elif message_type == "audio.start":
        session.state["audio"] = payload
    elif message_type == "audio.end":
        await session.send("stt.result", {"stream_id": payload.get("stream_id"), "text": "", "final": True}, ref=message_id)
    elif message_type == "event":
        await handle_event(session, payload)
    elif message_type == "state.report":
        session.state.update(payload)
    elif message_type == "tool.result":
        handle_tool_result(session, data.get("ref"), payload)
    elif message_type == "settings.update":
        session.settings.update(payload)
    else:
        await session.send(
            "error",
            {"code": "bad_message", "message": f"unknown type {message_type}", "retryable": False},
            ref=message_id,
        )


async def handle_chat(session: Session, text: str, ref: str) -> None:
    await session.send("chat.start", {"role": "assistant"}, ref=ref)
    full: list[str] = []
    try:
        async for kind, value in brain_reply_stream(text):
            if kind == "delta":
                full.append(str(value))
                await session.send("chat.delta", {"text": str(value)}, ref=ref)
            else:
                await session.send("chat.end", {"text": "".join(full), **dict(value)}, ref=ref)
    except asyncio.CancelledError:
        await session.send("chat.end", {"text": "".join(full), "emotion": "neutral", "action": "none", "interrupted": True}, ref=ref)


async def handle_event(session: Session, event: dict[str, Any]) -> None:
    name = event.get("name")
    data = event.get("data") or {}
    if name == "app.focus":
        session.focused = bool(data.get("focused"))
    elif name == "user.active":
        session.last_active = time.time()
    elif name == "user.arrived":
        await hub.push_proactive("greeting", "歡迎回來！", "happy")
    elif name == "user.idle":
        session.state["idle_seconds"] = data.get("seconds")


def handle_audio_frame(session: Session, data: bytes) -> None:
    if len(data) < 9:
        session.audio_frames.append({"error": "short_frame", "size": len(data)})
        return
    session.audio_frames.append(
        {
            "kind": data[0],
            "stream_id": int.from_bytes(data[1:5], "big"),
            "seq": int.from_bytes(data[5:9], "big"),
            "size": len(data) - 9,
        }
    )


def handle_tool_result(session: Session, ref: str | None, payload: dict[str, Any]) -> None:
    if ref:
        session.tool_results[str(ref)] = payload


def parse_text_message(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError("invalid JSON") from error
    if not isinstance(data, dict):
        raise ValueError("message must be an object")
    return data


def reset_hub_for_tests() -> None:
    hub.sessions.clear()
    hub.telegram_fallbacks.clear()
