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
from agent_execution.delivery import EventStore, CapacityError
from agent_execution.companion.protocol import (conversation_id, scope_device, decode_attachments,
    dedup_input, desktop_features, MAX_MESSAGE_BYTES)


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
    outbox: deque[dict[str, Any]] = field(default_factory=deque)
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
    event_store: Any = None
    request_id: str | None = None
    audio_turn: Any = None
    audio_task: asyncio.Task | None = None
    conversation_id: str = ""
    attachments: tuple = ()
    desktop_requests: dict = field(default_factory=dict)

    @property
    def scoped_device(self):
        return scope_device(self.device_id, self.conversation_id)

    @property
    def owner(self):
        return EventStore.owner(self.user_id, self.project, self.scoped_device)

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
            if self.event_store is not None:
                try:
                    message = self.event_store.append(self.owner, message)
                except CapacityError:
                    if self.cancel_chat is not None:
                        self.cancel_chat.set()
                    if self.ws is not None:
                        await self.ws.close(code=4429, reason="Reliable outbox full; ACK required")
                        self.ws = None
                    raise
                self.seq = message['seq']
            else:
                if len(self.outbox) >= 200:
                    raise CapacityError('Reliable outbox full; ACK required')
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
        if self.event_store is not None:
            self.event_store.ack(self.owner, seq)
        while self.outbox and int(self.outbox[0].get("seq") or 0) <= seq:
            self.outbox.popleft()


async def request_desktop_tool(session, action, arguments):
    connection = session.ws
    if connection is None or 'desktop.' + action not in session.caps:
        return {'success': False, 'error': 'Desktop tool unavailable; enable tools in MyAI settings'}
    key = new_id()
    future = asyncio.get_running_loop().create_future()
    session.desktop_requests[key] = (future, connection)
    try:
        # Register the waiter before sending: a fast client may respond immediately.
        message = {'v': 1, 'type': 'tool.call', 'id': key, 'ref': session.request_id,
                   'ts': now_ms(), 'payload': {'action': action, 'arguments': arguments, 'timeout_ms': 60000,
                                              'connection_id': session.state.get('desktop_connection_id')}}
        if session.event_store is not None:
            message = session.event_store.append(session.owner, message)
            session.seq = message['seq']
        else:
            if len(session.outbox) >= 200:
                raise CapacityError('Reliable outbox full')
            session.seq += 1
            message['seq'] = session.seq
        session.outbox.append(message)
        await asyncio.wait_for(connection.send_text(json.dumps(message, ensure_ascii=False)), 5)
        return await asyncio.wait_for(future, 60)
    except (asyncio.TimeoutError, OSError):
        return {'success': False, 'error': 'Desktop request expired; do not repeat without checking result'}
    finally:
        session.desktop_requests.pop(key, None)


class Hub:
    def __init__(self, event_store=None) -> None:
        self.sessions: dict[tuple[str, str, str], Session] = {}
        self.telegram_fallbacks: list[str] = []
        self.event_store = event_store

    def get(self, device_id: str, device_type: str = "web", user_id: str = "user", project="default", conversation="") -> Session:
        key = (user_id, project, scope_device(device_id, conversation))
        session = self.sessions.get(key)
        if session is None:
            peers = [key for key, value in self.sessions.items() if value.user_id == user_id
                     and value.project == project and value.device_id == device_id]
            if len(peers) >= 50:
                idle = [key for key in peers if self.sessions[key].ws is None and
                        (self.sessions[key].current_chat_task is None or self.sessions[key].current_chat_task.done())]
                if not idle:
                    raise CapacityError("Conversation capacity reached")
                del self.sessions[min(idle, key=lambda item: self.sessions[item].last_active)]
            from agent_execution.delivery import positive_limit
            if len(self.sessions) >= positive_limit('COMPANION_MAX_SESSIONS',10000):
                idle = [key for key,value in self.sessions.items() if value.ws is None and (value.current_chat_task is None or value.current_chat_task.done())]
                if not idle:
                    raise CapacityError('Companion session capacity reached')
                del self.sessions[min(idle,key=lambda key:self.sessions[key].last_active)]
            if self.event_store is None:
                self.event_store = EventStore()
            session = Session(device_id=device_id, device_type=device_type, user_id=user_id, project=project,
                              event_store=self.event_store, conversation_id=conversation)
            session.seq, _, events = self.event_store.snapshot(session.owner)
            session.outbox.extend(events)
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
    scoped_session = f"{session.project}:{session.scoped_device}"
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
            from agent_execution.companion.desktop import desktop_bridge, human_desktop_command
            def request_desktop(action, arguments):
                future = asyncio.run_coroutine_threadsafe(request_desktop_tool(session, action, arguments), loop)
                while True:
                    try:
                        return future.result(timeout=.2)
                    except __import__('concurrent.futures', fromlist=['TimeoutError']).TimeoutError:
                        if cancelled.is_set():
                            future.cancel()
                            raise InterruptedError("Desktop tool cancelled")
            with execution_scope(source="websocket", identity=identity, session=scoped_session,
                    project=session.project, task_id=session.request_id, workspace=project_workspace(session.project), check_cancelled=cancelled.is_set), \
                    set_current_role(get_user_role(identity)), auto_approve_command_runs(), desktop_bridge(request_desktop):
                from agent_execution.tasks import dispatch_task_input
                desktop_result = human_desktop_command(user_text)
                if desktop_result is not None:
                    from agent_runtime import AgentTurnResult
                    result = AgentTurnResult(desktop_result, None)
                elif session.attachments:
                    result = dispatch_task_input(user_text, runtime, attachments=session.attachments)
                else:
                    result = dispatch_task_input(user_text,runtime)
                if result is None:
                    result = run_agent_turn(user_text, runtime, attachments=session.attachments) if session.attachments else run_agent_turn(user_text, runtime)
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
        conversation = conversation_id(payload.get("conversation_id", ""))
        session = hub.get(device_id, str(payload.get("device_type") or "web"), tokens[token], project, conversation)
        if session.ws is not None:
            await session.ws.close(code=4409)
        for future, connection in list(session.desktop_requests.values()):
            if not future.done():
                future.set_result({'success': False, 'error': 'Desktop connection replaced'})
        session.desktop_requests.clear()
        session.ws = ws
        session.state['desktop_connection_id'] = new_id()
        if session.audio_turn:session.audio_turn.cancelled.set()
        session.audio_turn=None
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
        session.seq, floor, events = session.event_store.snapshot(session.owner)
        session.outbox.clear()
        session.outbox.extend(events)
        resumable = floor <= last_seq <= session.seq
        if resumable:
            session.ack(last_seq)

        await session.send(
            "welcome",
            {
                "session_id": new_id(),
                "connection_id": session.state['desktop_connection_id'],
                "server_time": now_ms(),
                "resumed": resumable,
                "features": {**__import__('agent_execution.media.audio',fromlist=['features']).features(), **desktop_features()},
                "last_seq": session.seq,
                "history_floor": floor,
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
    except CapacityError:
        await ws.close(code=4429, reason='Companion capacity reached; ACK required')
    except ValueError:
        if session is not None:
            await session.send("error", {"code": "bad_message", "message": "invalid JSON", "retryable": False})
    finally:
        if session is not None and session.ws is ws:
            for future, connection in list(session.desktop_requests.values()):
                if not future.done():
                    future.set_result({"success": False, "error": "Desktop disconnected; result may be uncertain"})
            session.desktop_requests.clear()
            if session.cancel_chat is not None:
                session.cancel_chat.set()
            if session.audio_turn:session.audio_turn.cancelled.set();session.audio_turn=None
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
        try:
            attachments = decode_attachments(payload.get("attachments"))
            if attachments and text.lstrip().startswith('/'):
                raise ValueError('Attachments cannot accompany transport control commands')
        except ValueError as error:
            await session.send("error", {"code": "bad_attachment", "message": str(error), "retryable": False}, ref=message_id)
            return
        if not text:
            await session.send("error", {"code": "bad_message", "message": "chat text is required", "retryable": False}, ref=message_id)
            return
        if session.current_chat_task is not None and not session.current_chat_task.done():
            await session.send("error", {"code": "chat_busy", "message": "Wait for the previous chat to finish or interrupt it", "retryable": True}, ref=message_id)
            return
        if session.event_store is not None:
            if not isinstance(message_id, str) or not message_id or len(message_id) > 128:
                await session.send('error', {'code':'bad_message','message':'chat.send requires a stable ID (1..128 characters)','retryable':False})
                return
            previous = session.event_store.accept_input(session.owner, message_id, dedup_input(text, attachments))
            if previous is not None:
                await session.send('notify', {'request_id':message_id,'status':previous,'text':'Duplicate input was not executed; inspect replayed events.'}, ref=message_id)
                return
        session.cancel_chat = threading.Event()
        session.request_id = message_id
        session.attachments = attachments
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
            result = await asyncio.to_thread(human_command, f"/{decision} {approval_id}", identity, f"{session.project}:{session.scoped_device}")
        await session.send("approval.result", {"message": result}, ref=message_id)
    elif message_type == "audio.start":
        from agent_execution.media.audio import AudioTurn,features
        if not features()['stt']:raise ValueError('STT not configured')
        if session.audio_turn:session.audio_turn.cancelled.set()
        session.audio_turn=AudioTurn(payload.get('stream_id'),payload.get('format','wav'))
        session.audio_frames.clear()
    elif message_type == "audio.end":
        turn=session.audio_turn
        if turn is None or payload.get('stream_id')!=turn.stream_id or session.audio_task and not session.audio_task.done():raise ValueError('No matching idle audio stream')
        data=turn.finish();session.audio_task=asyncio.create_task(audio_result(session,turn,'stt',message_id,data=data,format=turn.format))
    elif message_type == 'audio.cancel':
        if session.audio_turn:session.audio_turn.cancelled.set();session.audio_turn=None
    elif message_type == 'tts.request':
        from agent_execution.media.audio import AudioTurn,features
        if not features()['tts'] or session.audio_task and not session.audio_task.done():raise ValueError('TTS unavailable/busy')
        turn=AudioTurn(payload.get('stream_id'),'mp3');session.audio_turn=turn
        session.audio_task=asyncio.create_task(audio_result(session,turn,'tts',message_id,text=payload.get('text',''),voice=payload.get('voice','alloy')))
    elif message_type == "event":
        await handle_event(session, payload)
    elif message_type == "state.report":
        session.state.update(payload)
    elif message_type == "tool.result":
        reference = data.get("ref")
        pending = session.desktop_requests.get(reference)
        if pending:
            future, connection = pending
            if connection is session.ws and not future.done() and type(payload.get("ok")) is bool:
                encoded = json.dumps(payload, ensure_ascii=False)
                if len(encoded.encode()) > 16384:
                    raise ValueError("Desktop result too large")
                future.set_result({"success": payload['ok'], "output": payload.get('data'), "error": payload.get('error')})
        else:
            handle_tool_result(session, reference, payload)
    elif message_type == "settings.update":
        session.settings.update(payload)
    else:
        await session.send(
            "error",
            {"code": "bad_message", "message": f"unknown type {message_type}", "retryable": False},
            ref=message_id,
        )


async def handle_chat(session: Session, text: str, ref: str) -> None:
    full: list[str] = []
    try:
        await session.send("chat.start", {"role": "assistant"}, ref=ref)
        async for kind, value in brain_reply_stream(text, session):
            if kind == "model.delta":
                full.append(value["text"])
                await session.send("chat.delta", value, ref=ref)
            elif kind == "end":
                await session.send("chat.end", {**dict(value), "interrupted": bool(session.cancel_chat and session.cancel_chat.is_set())}, ref=ref)
                if session.event_store is not None:
                    session.event_store.finish_input(session.owner, ref, 'interrupted' if session.cancel_chat and session.cancel_chat.is_set() else 'failed' if value.get('failed') else 'suspended' if value.get('suspended') else 'completed')
            else:
                await session.send(kind, value, ref=ref)
    except asyncio.CancelledError:
        await session.send("chat.end", {"text": "".join(full), "emotion": "neutral", "action": "none", "interrupted": True}, ref=ref)
    except CapacityError:
        # History remains intact and the request stays uncertain; never repeat effects.
        pass


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
    if session.audio_turn:session.audio_turn.append(data)
    if len(session.audio_frames) >= 256:
        session.audio_frames.pop(0)
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


async def audio_result(session,turn,kind,ref,**arguments):
    from agent_execution.media.audio import voice_call
    ws=session.ws
    try:
        value=await asyncio.to_thread(voice_call,kind,cancelled=turn.cancelled.is_set,**arguments)
        if session.audio_turn is turn and session.ws is ws and not turn.cancelled.is_set():
            await session.send('stt.result' if kind=='stt' else 'tts.result',{'stream_id':turn.stream_id,'final':True,**{k:v for k,v in value.items() if k!='success'}},ref=ref)
    except Exception as error:
        if session.audio_turn is turn and session.ws is ws and not turn.cancelled.is_set():await session.send('error',{'code':'audio_failed','message':type(error).__name__,'retryable':False},ref=ref)
    finally:
        if session.audio_turn is turn:session.audio_turn=None


def handle_tool_result(session: Session, ref: str | None, payload: dict[str, Any]) -> None:
    if ref:
        if len(session.tool_results) >= 256:
            session.tool_results.pop(next(iter(session.tool_results)))
        session.tool_results[str(ref)] = payload


def parse_text_message(text: str) -> dict[str, Any]:
    if len(text.encode()) > MAX_MESSAGE_BYTES:
        raise ValueError('message exceeds 4 MiB')
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError("invalid JSON") from error
    if not isinstance(data, dict):
        raise ValueError("message must be an object")
    return data


def reset_hub_for_tests() -> None:
    import tempfile
    hub.sessions.clear()
    hub.telegram_fallbacks.clear()
    previous = getattr(hub, '_test_storage', None)
    if previous is not None:
        previous.cleanup()
    hub._test_storage = tempfile.TemporaryDirectory()
    hub.event_store = EventStore(os.path.join(hub._test_storage.name, 'events.db'))
