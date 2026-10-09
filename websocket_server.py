from __future__ import annotations

import asyncio
from collections import deque
import json
import os
import re
import time
import uuid
import threading
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from fastapi import FastAPI, WebSocket, WebSocketDisconnect


PROTOCOL_VERSION = 1
RELIABLE_TYPES = {"chat.end", "proactive", "notify", "tool.call", "tool.start", "tool.result", "task.evaluation", "approval.required", "approval.result", "error"}
DEFAULT_FEATURES = {"stt": False, "tts": False, "tts_voices": []}


def new_id() -> str:
    return uuid.uuid4().hex


def now_ms() -> int:
    return int(time.time() * 1000)


def websocket_tokens() -> dict[str, str]:
    raw = os.environ.get("COMPANION_WS_TOKENS") or os.environ.get("COMPANION_WS_TOKEN") or ""
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
    project: str = "default"
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
    runtime: Any = None
    runtime_factory: Any = None
    cancel_chat: threading.Event | None = None

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
                await asyncio.wait_for(self.ws.send_text(json.dumps(message, ensure_ascii=False)), timeout=5)
            except Exception:
                if self.cancel_chat is not None:
                    self.cancel_chat.set()
                self.ws = None
        return message

    def ack(self, seq: int) -> None:
        while self.outbox and int(self.outbox[0].get("seq") or 0) <= seq:
            self.outbox.popleft()


class Hub:
    def __init__(self) -> None:
        self.sessions: dict[tuple[str, str, str], Session] = {}
        self.telegram_fallbacks: list[str] = []

    def get(self, device_id: str, device_type: str = "web", user_id: str = "user", project="default") -> Session:
        key = (user_id, project, device_id)
        session = self.sessions.get(key)
        if session is None:
            session = Session(device_id=device_id, device_type=device_type, user_id=user_id, project=project)
            self.sessions[key] = session
        session.device_type = device_type or session.device_type
        return session

    def pick_for_proactive(self, user_id: str, project="default") -> Session | None:
        online = [session for session in self.sessions.values() if session.ws is not None and session.user_id == user_id and session.project == project]
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
        user_id: str | None = None,
        project: str = "default",
    ) -> dict[str, Any] | None:
        payload = {"reason": reason, "text": text, "emotion": emotion, "tts": True, "priority": priority}
        if not user_id:
            raise ValueError("Proactive messages require a target user")
        session = self.pick_for_proactive(user_id, project)
        if session is not None:
            return await session.send("proactive", payload)
        if priority == "high":
            await send_via_telegram(text, self, user_id=user_id, project=project)
        return None


hub = Hub()
app = FastAPI(title="PythonAI Companion WebSocket")


async def send_via_telegram(text: str, target_hub: Hub = hub, user_id=None, project="default") -> None:
    from communication_adapters.telegram_adapter import TelegramAdapter
    from communication_models import OutboundMessage
    destinations = json.loads(os.environ.get("COMPANION_TELEGRAM_TARGETS", "{}"))
    destination = destinations.get(f"{user_id}:{project}") if isinstance(destinations, dict) else None
    if not isinstance(destination, str) or not destination:
        raise ValueError("No authorized Telegram fallback destination for this user/project")
    await asyncio.to_thread(TelegramAdapter().send_message, OutboundMessage("telegram", destination, text))
    target_hub.telegram_fallbacks.append(text)


async def brain_reply_stream(user_text: str, session: Session) -> AsyncIterator[tuple[str, Any]]:
    from agent_runtime import run_agent_turn
    from agent_execution import execution_scope
    from agent_execution.sessions import RuntimeSessions, project_workspace
    from base import auto_approve_command_runs
    from permissions import get_user_role, set_current_role
    identity = f"websocket:{session.user_id}"
    scoped_session = f"{session.project}:{session.device_id}"
    cancelled = session.cancel_chat or threading.Event()
    events = asyncio.Queue(maxsize=128)
    loop = asyncio.get_running_loop()

    def emit(kind, payload):
        if cancelled.is_set() and kind != "end":
            return
        future = asyncio.run_coroutine_threadsafe(events.put((kind, payload)), loop)
        try:
            future.result(timeout=5)
        except Exception:
            future.cancel()
            cancelled.set()
            raise InterruptedError("WebSocket event consumer stalled")

    def execute():
        try:
            from agent_execution.approvals import human_command
            with set_current_role(get_user_role(identity)):
                decision = human_command(user_text, identity, scoped_session)
            if decision is not None:
                emit("end", {"text": decision, "emotion": "neutral", "action": "none"})
                return
            if session.runtime is None:
                template = session.runtime_factory()
                session.runtime = RuntimeSessions(template).get((identity, scoped_session, session.project))[0]
            runtime = session.runtime
            runtime.check_cancelled = cancelled.is_set
            runtime.event_callback = emit
            with execution_scope(source="websocket", identity=identity, session=scoped_session,
                    project=session.project, workspace=project_workspace(session.project), check_cancelled=cancelled.is_set), \
                    set_current_role(get_user_role(identity)), auto_approve_command_runs():
                result = run_agent_turn(user_text, runtime)
            emit("end", {"text": result.reply, "episode_id": result.episode_id,
                          "suspended": result.suspended, "failed": result.reply.startswith("Error:"),
                          "emotion": "neutral", "action": "none"})
        except Exception as error:
            try:
                emit("error", {"code": "agent_error", "message": str(error), "retryable": False})
                emit("end", {"text": "", "failed": True, "emotion": "neutral", "action": "none"})
            except InterruptedError:
                pass
        finally:
            if session.runtime is not None:
                session.runtime.check_cancelled = None
                session.runtime.event_callback = None

    task = asyncio.create_task(asyncio.to_thread(execute))
    try:
        while True:
            try:
                kind, value = await asyncio.wait_for(events.get(), timeout=.2)
            except asyncio.TimeoutError:
                if task.done():
                    yield "end", {"text": "", "failed": True, "emotion": "neutral", "action": "none"}
                    break
                continue
            yield kind, value
            if kind == "end":
                break
        await task
    finally:
        cancelled.set()
        # Keep the session busy until synchronous model/tool cleanup has completed.
        await asyncio.shield(task)


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
        project = str(payload.get("project") or "default")
        if len(device_id) > 128 or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", project):
            await ws.close(code=4400)
            return
        session = hub.get(device_id, str(payload.get("device_type") or "web"), tokens[token], project)
        if session.ws is not None:
            await session.ws.close(code=4409)
        session.ws = ws
        def runtime_factory():
            template = getattr(ws.app.state, "agent_runtime", None)
            if template is None:
                from communication_server import create_agent_runtime
                template = create_agent_runtime()
                ws.app.state.agent_runtime = template
            return template
        session.runtime_factory = runtime_factory
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
        if session is not None and session.ws is ws:
            if session.cancel_chat is not None:
                session.cancel_chat.set()
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
        if session.current_chat_task is not None and not session.current_chat_task.done():
            await session.send("error", {"code": "chat_busy", "message": "Wait for the previous chat to finish or interrupt it", "retryable": True}, ref=message_id)
            return
        session.cancel_chat = threading.Event()
        session.current_chat_task = asyncio.create_task(handle_chat(session, text, str(message_id or "")))
    elif message_type == "chat.interrupt":
        if session.current_chat_task is not None and not session.current_chat_task.done():
            session.cancel_chat.set()
    elif message_type == "approval.decide":
        from agent_execution.approvals import human_command
        from permissions import get_user_role, set_current_role
        decision = payload.get("decision")
        approval_id = payload.get("approval_id")
        if decision not in {"approve", "deny"} or not isinstance(approval_id, str):
            await session.send("error", {"code": "bad_approval", "message": "approval_id and approve/deny decision required", "retryable": False}, ref=message_id)
            return
        identity = f"websocket:{session.user_id}"
        with set_current_role(get_user_role(identity)):
            result = await asyncio.to_thread(human_command, f"/{decision} {approval_id}", identity, f"{session.project}:{session.device_id}")
        await session.send("approval.result", {"message": result}, ref=message_id)
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
        async for kind, value in brain_reply_stream(text, session):
            if kind == "model.delta":
                full.append(value["text"])
                await session.send("chat.delta", value, ref=ref)
            elif kind == "end":
                await session.send("chat.end", {**dict(value), "interrupted": bool(session.cancel_chat and session.cancel_chat.is_set())}, ref=ref)
            else:
                await session.send(kind, value, ref=ref)
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
        await hub.push_proactive("greeting", "歡迎回來！", "happy", user_id=session.user_id, project=session.project)
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
