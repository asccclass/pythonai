from __future__ import annotations

import asyncio
from collections import deque
import json
import logging
import os
import re
import sqlite3
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
RELIABLE_TYPES = {"chat.end", "proactive", "notify", "tool.call", "tool.start", "tool.result", "task.evaluation", "approval.required", "approval.result", "approval.cancelled", "error", "workspace.result", "session.delete.preview", "session.delete.result", "turn.delete.result", "meeting.process.result"}
DEFAULT_FEATURES = {"stt": False, "tts": False, "tts_voices": []}
logger = logging.getLogger(__name__)


def _log_role_diagnostic(stage: str, identity: str, role: str, tool: str) -> None:
    """Log only authorization-routing metadata; never user content or tool arguments."""
    logger.info("SECURITY_DIAGNOSTIC %s", json.dumps({
        "stage": stage, "identity": identity, "role": role, "tool": tool,
    }, ensure_ascii=False, separators=(",", ":")))


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
    waiting_approvals: set = field(default_factory=set)
    durable_task: bool = False
    sent_seq: int = 0
    send_lock: Any = None
    connection_ready: bool = True

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
        event_key = payload.get('temporal_event_id')
        if event_key:
            ref = payload['task_id']
            message['ref'] = ref
        if type_ in RELIABLE_TYPES or event_key:
            if self.event_store is not None:
                try:
                    message = self.event_store.append(self.owner, message, event_key=event_key)
                    if message is None:
                        return {}
                except CapacityError:
                    if self.cancel_chat is not None and not self.durable_task:
                        self.cancel_chat.set()
                    if self.ws is not None:
                        await self.ws.close(code=4429, reason="Reliable outbox full; ACK required")
                        self.ws = None
                    raise
                self.seq = max(self.seq,message['seq'])
            else:
                if len(self.outbox) >= 200:
                    raise CapacityError('Reliable outbox full; ACK required')
                self.seq += 1
                message["seq"] = self.seq
            if not any(item.get('seq')==message.get('seq') for item in self.outbox):
                self.outbox.append(message)
            if self.event_store is not None:
                await self.flush()
                return message
        if self.ws is not None:
            try:
                await asyncio.wait_for(self.ws.send_text(json.dumps(message, ensure_ascii=False)), timeout=5)
            except Exception:
                if self.cancel_chat is not None and not self.durable_task:
                    self.cancel_chat.set()
                self.ws = None
        return message

    async def flush(self):
        if self.event_store is None or self.ws is None or not self.connection_ready:
            return
        if self.send_lock is None:
            self.send_lock = asyncio.Lock()
        async with self.send_lock:
            connection = self.ws
            if connection is None:
                return
            self.seq, floor, messages = self.event_store.snapshot(self.owner)
            self.outbox.clear()
            self.outbox.extend(messages)
            for message in messages:
                if message['seq'] <= self.sent_seq or self.ws is not connection:
                    continue
                try:
                    await asyncio.wait_for(connection.send_text(json.dumps(message,ensure_ascii=False)),timeout=5)
                    self.sent_seq = message['seq']
                except Exception:
                    if self.cancel_chat is not None and not self.durable_task:
                        self.cancel_chat.set()
                    if self.ws is connection:
                        self.ws = None
                    return

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
    from permissions import current_role, get_user_role, set_current_role
    identity = f"websocket:{session.user_id}"
    scoped_session = f"{session.project}:{session.scoped_device}"
    cancelled = session.cancel_chat or threading.Event()
    events = asyncio.Queue(maxsize=128)
    loop = asyncio.get_running_loop()

    detached = [False]
    def emit(kind, payload):
        if payload.get('temporal_event_id'):
            session.durable_task = True
        elif kind in {'model.start','tool.start'}:
            session.durable_task = False  # An existing SQLite binding still owns legacy cancellation.
        if session.durable_task and (detached[0] or session.ws is None):
            return
        if cancelled.is_set() and kind not in {"end", "approval.cancelled"}:
            return
        future = asyncio.run_coroutine_threadsafe(events.put((kind, payload)), loop)
        try:
            future.result(timeout=5)
        except Exception:
            future.cancel()
            if session.durable_task:
                detached[0] = True
                return
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
            from agent_execution.temporal_backend.router import backend_for_task
            from agent_execution.companion.desktop import desktop_bridge, human_desktop_command
            from agent_execution.companion.approval_bridge import approval_bridge
            def wait_approval(approval_id, name, arguments):
                from agent_execution.approvals import ApprovalStore
                from agent_execution.budgets import active_budget
                store = ApprovalStore()
                state = 'pending'
                completed_wait = False
                session.waiting_approvals.add(approval_id)
                try:
                    emit('approval.required', {'approval_id': approval_id, 'name': name,
                         'arguments': arguments, 'auto_resume': True})
                    while True:
                        if cancelled.is_set():
                            raise InterruptedError('Approval wait cancelled; operation was not executed')
                        budget = active_budget()
                        if budget is not None:
                            budget.check()
                        state = store.decision_state(approval_id, identity, scoped_session)
                        if state == 'approved':
                            completed_wait = True
                            return True
                        if state == 'denied':
                            completed_wait = True
                            return False
                        if state != 'pending':
                            raise PermissionError('Approval expired or unavailable; operation was not executed')
                        cancelled.wait(.1)
                finally:
                    if not completed_wait:
                        store.cancel_wait(approval_id, identity, scoped_session)
                    if not completed_wait:
                        emit('approval.cancelled', {'approval_id': approval_id, 'reason': 'cancelled' if cancelled.is_set() else 'expired'})
                    session.waiting_approvals.discard(approval_id)
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
                    project=session.project, conversation=session.conversation_id, task_id=session.request_id, workspace=project_workspace(session.project), check_cancelled=cancelled.is_set,
                    event_delivery=({'path':str(session.event_store.path.resolve()),'owner':session.owner,'ref':session.request_id} if session.event_store is not None else None)), \
                    set_current_role(get_user_role(identity)), auto_approve_command_runs(), desktop_bridge(request_desktop), approval_bridge(wait_approval if 'approval.auto_resume' in session.caps else None):
                session.durable_task = not user_text.startswith('/') and backend_for_task()=='temporal'
                from agent_execution.tasks import dispatch_task_input
                _log_role_diagnostic("task_dispatch", identity, current_role(), "dispatch_task_input")
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
                from agent_execution.temporal_backend.runtime_state import frontend_end
                end = frontend_end({"text":result.reply,"episode_id":result.episode_id,"suspended":result.suspended,
                    "failed":result.reply.startswith("Error:"),"emotion":"neutral","action":"none"})
                from agent_execution.companion.outputs import available
                end['files'] = available(session.request_id)
            emit("end",end)
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
                    if session.durable_task:
                        break
                    yield "end", {"text": "", "failed": True, "emotion": "neutral", "action": "none"}
                    break
                continue
            yield kind, value
            if kind == "end":
                break
        await task
    finally:
        if not session.durable_task:
            cancelled.set()
        # Keep the session busy until synchronous model/tool cleanup has completed.
        await asyncio.shield(task)


@app.websocket("/ws/v1")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    session: Session | None = None
    stream_pump = None
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
        from permissions import get_user_role
        _log_role_diagnostic("websocket_entry", f"websocket:{session.user_id}",
                             get_user_role(f"websocket:{session.user_id}"), "websocket_connect")
        if session.ws is not None:
            await session.ws.close(code=4409)
        for future, connection in list(session.desktop_requests.values()):
            if not future.done():
                future.set_result({'success': False, 'error': 'Desktop connection replaced'})
        session.desktop_requests.clear()
        session.ws = ws
        session.connection_ready = False
        session.state['desktop_connection_id'] = new_id()
        session.send_lock = asyncio.Lock()
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
        replayed_seq = last_seq if resumable else session.seq
        if resumable:
            session.ack(last_seq)

        await session.send(
            "welcome",
            {
                "session_id": new_id(),
                "connection_id": session.state['desktop_connection_id'],
                "server_time": now_ms(),
                "resumed": resumable,
                "features": {**__import__('agent_execution.media.audio',fromlist=['features']).features(), **desktop_features(), 'speech_polish': True, 'meeting_processing': True, 'approval_auto_resume': True, **lifecycle_features(session)},
                "last_seq": session.seq,
                "history_floor": floor,
            },
        )
        if resumable:
            for message in list(session.outbox):
                if int(message.get("seq") or 0) > last_seq:
                    await ws.send_text(json.dumps(message, ensure_ascii=False))
                    replayed_seq = max(replayed_seq,message['seq'])

        session.sent_seq = replayed_seq
        session.connection_ready = True
        async def pump():
            while session.ws is ws:
                await session.flush()
                await asyncio.sleep(.2)
        stream_pump = asyncio.create_task(pump())
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
    except (WebSocketDisconnect, asyncio.TimeoutError, asyncio.CancelledError):
        pass
    except CapacityError:
        await ws.close(code=4429, reason='Companion capacity reached; ACK required')
    except ValueError:
        if session is not None:
            await session.send("error", {"code": "bad_message", "message": "invalid JSON", "retryable": False})
    finally:
        if stream_pump is not None:
            stream_pump.cancel()
            try:
                await asyncio.gather(stream_pump,return_exceptions=True)
            except asyncio.CancelledError:
                pass  # Complete synchronous connection cleanup during server shutdown.
        if session is not None and session.ws is ws:
            for future, connection in list(session.desktop_requests.values()):
                if not future.done():
                    future.set_result({"success": False, "error": "Desktop disconnected; result may be uncertain"})
            session.desktop_requests.clear()
            if session.cancel_chat is not None and not session.durable_task:
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
    if isinstance(message_type,str) and message_type.startswith(('workspace.','session.delete.','turn.delete')):
        await handle_lifecycle_message(session,data)
    elif message_type == 'file.get':
        from agent_execution.companion.outputs import read_chunk
        def download():
            with lifecycle_scope(session):
                return read_chunk(payload.get('file_id'), payload.get('offset', 0))
        try:
            await session.send('file.chunk', await asyncio.to_thread(download), ref=message_id)
        except (ValueError, OSError, sqlite3.Error) as error:
            await session.send('error', {'code': 'file_unavailable', 'message': str(error), 'retryable': False}, ref=message_id)
    elif message_type == "ping":
        await session.send("pong", {}, ref=message_id)
    elif message_type == "ack":
        session.ack(int(payload.get("seq") or 0))
    elif message_type == "chat.send":
        text = str(payload.get("text") or "")
        from agent_execution.workspace_lifecycle import LifecycleStore
        from agent_execution.policy import execution_scope
        with execution_scope(source='websocket',identity='websocket:'+session.user_id,project=session.project,
                session=session.project+':'+session.scoped_device,conversation=session.conversation_id):
            try:LifecycleStore().ensure_session()
            except PermissionError:
                if not text.startswith(('/workspace resume ','/session-delete status ')):
                    await session.send('error',{'code':'session_deleted','message':'Conversation is deleting or deleted','retryable':False},ref=message_id)
                    return
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
    elif message_type == "speech.polish":
        text = str(payload.get("text") or "").strip()
        if not text:
            await session.send("error", {"code": "bad_message", "message": "speech.polish requires text", "retryable": False}, ref=message_id)
            return
        if session.current_chat_task is not None and not session.current_chat_task.done():
            await session.send("error", {"code": "chat_busy", "message": "Wait for the current request to finish", "retryable": True}, ref=message_id)
            return
        async def polish():
            prompt = (
                "請校正以下繁體中文語音辨識文字。只修正明顯同音錯字與標點，"
                "忠實保留原意；只回傳校正後文字，不要解釋、回答或使用工具。\n\n"
                f"【原始語音文字】\n{text}"
            )
            corrected = text
            try:
                async for kind, value in brain_reply_stream(prompt, session):
                    if kind == "end" and isinstance(value, dict):
                        corrected = str(value.get("text") or text).strip() or text
                    elif kind == "error":
                        raise RuntimeError(str(value.get("message") or "speech polishing failed"))
                await session.send("speech.polish.result", {"text": corrected}, ref=message_id)
            except Exception:
                await session.send("speech.polish.result", {"text": text, "fallback": True}, ref=message_id)
        session.cancel_chat = threading.Event()
        session.current_chat_task = asyncio.create_task(polish())
    elif message_type == "meeting.process":
        text = str(payload.get("text") or "").strip()
        language = str(payload.get("language") or "auto").strip()
        if not text:
            await session.send("error", {"code": "bad_message", "message": "meeting.process requires text", "retryable": False}, ref=message_id)
            return
        if session.current_chat_task is not None and not session.current_chat_task.done():
            await session.send("error", {"code": "chat_busy", "message": "Wait for the current request to finish", "retryable": True}, ref=message_id)
            return
        async def process_meeting():
            fallback_summary = re.sub(r"\s+", " ", text)[:160]
            prompt = (
                "你是會議逐字稿處理器。偵測語言代碼為 " + language + "。\n"
                "若原文完全不是繁體中文（包含簡體中文或其他語言），將完整內容翻譯為繁體中文；"
                "若原文含繁體中文，保留原本語言。校正明顯語音辨識錯字與標點，忠實保留原意。\n"
                "接著以繁體中文產生 2 至 4 個重點摘要。不得使用工具、不得回答問題或補充未提及資訊。\n"
                "務必只依下列格式回覆：\n<<<CONTENT>>>\n處理後內容\n<<<SUMMARY>>>\n摘要\n\n"
                f"【原始逐字稿】\n{text}"
            )
            processed, summary = text, fallback_summary
            try:
                reply = text
                async for kind, value in brain_reply_stream(prompt, session):
                    if kind == "end" and isinstance(value, dict):
                        reply = str(value.get("text") or text).strip() or text
                    elif kind == "error":
                        raise RuntimeError(str(value.get("message") or "meeting processing failed"))
                match = re.search(r"<<<CONTENT>>>\s*(.*?)\s*<<<SUMMARY>>>\s*(.+)", reply, re.S)
                if match:
                    processed = match.group(1).strip() or text
                    summary = match.group(2).strip() or fallback_summary
                else:
                    processed = reply
                await session.send("meeting.process.result", {"text": processed, "summary": summary}, ref=message_id)
            except Exception:
                await session.send("meeting.process.result", {"text": text, "summary": fallback_summary, "fallback": True}, ref=message_id)
        session.cancel_chat = threading.Event()
        session.current_chat_task = asyncio.create_task(process_meeting())
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
        waiting = approval_id in session.waiting_approvals
        with set_current_role(get_user_role(identity)):
            result = await asyncio.to_thread(human_command, f"/{decision} {approval_id}", identity, f"{session.project}:{session.scoped_device}")
        saved = result.startswith('Approval decision saved.')
        auto_resume = saved and decision == 'approve' and waiting
        if auto_resume:
            result = 'Approval decision saved. Continuing the original operation.'
        await session.send("approval.result", {"message": result, 'auto_resume': auto_resume,
                           'approved': saved and decision == 'approve'}, ref=message_id)
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
                await session.send("chat.end", {**dict(value), "interrupted": bool(value.get("interrupted") or session.cancel_chat and session.cancel_chat.is_set())}, ref=ref)
                if session.event_store is not None:
                    session.event_store.finish_input(session.owner, ref, 'uncertain' if value.get('task_state')=='uncertain' else 'interrupted' if value.get('interrupted') or session.cancel_chat and session.cancel_chat.is_set() else 'failed' if value.get('failed') else 'suspended' if value.get('suspended') else 'completed')
            else:
                await session.send(kind, value, ref=ref)
    except asyncio.CancelledError:
        if session.durable_task:
            return  # Subscriber shutdown is not a final result or a task cancellation.
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


def lifecycle_scope(session):
    from agent_execution.policy import execution_scope
    from agent_execution.sessions import project_workspace
    return execution_scope(source='websocket',identity='websocket:'+session.user_id,
        project=session.project,session=session.project+':'+session.scoped_device,
        conversation=session.conversation_id,workspace=project_workspace(session.project))


def lifecycle_features(session):
    from contextlib import closing
    from agent_execution.workspace_lifecycle import LifecycleStore
    from agent_execution.workspace_lifecycle.store import session_key
    with lifecycle_scope(session):
        store=LifecycleStore()
        try:store.ensure_session()
        except PermissionError:pass
        with closing(store.connect()) as conn:
            row=conn.execute('SELECT state FROM wl_sessions WHERE id=?',(session_key(),)).fetchone()
            instance=conn.execute("SELECT value FROM wl_metadata WHERE key='instance'").fetchone()[0]
            canonical=session_key()
        state=row[0] if row else 'active'
    session.state['lifecycle_state']=state
    return {'workspace_lifecycle':{'version':1,'instance':instance,'session':canonical,'turn_cleanup':True,'recovery':os.name=='nt','mutable':os.name=='nt' and os.environ.get('AGENT_WORKSPACE_CLEANUP','1').lower() in {'1','true'}},'conversation_state':state}


def matching_conversations(session):
    return [other for other in list(hub.sessions.values())
        if other.user_id==session.user_id and other.project==session.project and
        ((session.conversation_id and other.conversation_id==session.conversation_id) or
         (not session.conversation_id and not other.conversation_id and other.device_id==session.device_id))]


async def handle_lifecycle_message(session,data):
    from contextlib import closing
    from agent_execution.workspace_lifecycle import LifecycleStore
    from agent_execution.workspace_lifecycle.erasure import finalize
    from permissions import set_current_role,get_user_role
    kind=data['type'];payload=data.get('payload') or {};reference=data.get('id')
    allowed={'workspace.list','workspace.preview','workspace.request','workspace.confirm','workspace.retain',
        'session.delete.preview','session.delete.confirm','session.delete.status','session.delete.resume',
        'turn.delete','turn.delete.status'}
    if kind not in allowed:
        await session.send('error',{'code':'bad_lifecycle','message':'Unknown workspace operation','retryable':False},ref=reference)
        return
    peers=matching_conversations(session) or [session]
    busy=[other.scoped_device for other in peers if
        other.current_chat_task and not other.current_chat_task.done() or
        other.audio_task and not other.audio_task.done()]
    if busy and kind not in {'workspace.list','session.delete.status','turn.delete.status'}:
        await session.send('error',{'code':'cleanup_busy','message':'Wait for active conversation work to finish or interrupt it first','retryable':True},ref=reference)
        return
    def run():
        with lifecycle_scope(session),set_current_role(get_user_role('websocket:'+session.user_id)):
            store=LifecycleStore()
            if kind in {'turn.delete','turn.delete.status'}:
                from agent_execution.companion.turn_cleanup import delete, status
                return (delete if kind=='turn.delete' else status)(payload.get('request_id'))
            if kind=='workspace.list':return store.inventory()
            if kind=='workspace.retain':
                store.retain(payload.get('directory_id'))
                return {'state':'retained'}
            if kind=='workspace.preview':return store.preview(payload.get('action'),payload.get('directory_id'))
            if kind=='session.delete.preview':return store.preview('delete_session')
            key=payload.get('plan_id')
            plan=store.plan(key)
            if kind.startswith('session.delete.') and plan['action']!='delete_session':raise ValueError('Not a session deletion plan')
            if kind.startswith('workspace.') and plan['action']=='delete_session':raise ValueError('Use the session deletion confirmation')
            if kind=='workspace.request':return store.request(key)
            if kind in {'workspace.confirm','session.delete.confirm'}:result=store.confirm(key)
            elif kind=='session.delete.resume':
                from agent_execution.workspace_lifecycle.operations import resume
                result=resume(store,key)
            else:
                with closing(store.connect()) as conn:
                    row=conn.execute('SELECT state,error FROM wl_operations WHERE id=?',(key,)).fetchone()
                result={'operation_id':key,**(dict(row) if row else {'state':'preview'})}
            if plan['action']=='delete_session' and result['state']=='completed':
                result=finalize(store,key,session.runtime,session.event_store)
            return result
    try:
        result=await asyncio.to_thread(run)
        if result.get('state')=='deleted' and kind.startswith('session.delete.'):
            for peer in peers:
                peer.state['lifecycle_state']='deleted'
                peer.outbox.clear();peer.attachments=()
                if peer.audio_turn:peer.audio_turn.cancelled.set();peer.audio_turn=None
                if peer.runtime is not None:peer.runtime.messages=[m for m in peer.runtime.messages if m.get('role')=='system']
        response='turn.delete.result' if kind.startswith('turn.delete') else 'session.delete.preview' if kind=='session.delete.preview' else 'session.delete.result' if kind.startswith('session.delete.') else 'workspace.result'
        await session.send(response,result,ref=reference)
        if result.get('state')=='deleted':
            for peer in peers:
                if peer is not session and peer.ws:
                    await peer.send(response,result)
    except (ValueError,PermissionError,OSError,sqlite3.Error) as error:
        await session.send('error',{'code':'cleanup_blocked','message':str(error),'retryable':False},ref=reference)


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
