from __future__ import annotations
from contextvars import copy_context

from typing import Callable, Iterable
import threading

from agent_runtime import AgentRuntime, run_agent_turn, resume_agent_turn, AgentTurnResult
from base import auto_approve_command_runs
from communication_models import AgentCommand, CommunicationAdapter, OutboundMessage
from communication_store import CommunicationStore


CommandRunner = Callable[[AgentCommand, list | None], AgentTurnResult]


def agent_runtime_command_runner(runtime: AgentRuntime, store: CommunicationStore) -> CommandRunner:
    from agent_execution.sessions import RuntimeSessions, project_workspace
    sessions = RuntimeSessions(runtime)

    def key(command):
        return (f"{command.platform}:{command.sender_id}", f"{command.platform}:{command.conversation_id}", command.project)

    def run(command: AgentCommand, resumed_messages: list | None = None) -> AgentTurnResult:
        from permissions import get_user_role, set_current_role
        role = get_user_role(f"{command.platform}:{command.sender_id}")
        session_runtime, lock = sessions.get(key(command))
        cancelled = lambda: store.is_cancel_requested(command.command_id)
        while not lock.acquire(timeout=.1):
            if cancelled():
                return AgentTurnResult("Task cancelled", None)
        from agent_execution import execution_scope
        try:
            if cancelled():
                return AgentTurnResult("Task cancelled", None)
            session_runtime.check_cancelled = cancelled
            with auto_approve_command_runs(), set_current_role(role), execution_scope(
                source="communication", identity=key(command)[0], session=key(command)[1],
                project=command.project, task_id=command.command_id, workspace=project_workspace(command.project), check_cancelled=cancelled):
                if resumed_messages is not None:
                    return resume_agent_turn(resumed_messages, session_runtime)
                from agent_execution.tasks import dispatch_task_input
                controlled = dispatch_task_input(command.text,session_runtime)
                if controlled is not None:
                    return controlled
                from agent_execution.tasks import TaskStore, task_owner
                from agent_execution.policy import current_context
                try:
                    previous = TaskStore().get(task_owner(current_context()),command.command_id)
                except KeyError:
                    previous = None
                if previous and previous['state'] not in {'suspended','uncertain'}:
                    return resume_agent_turn(previous['checkpoint']['messages'],session_runtime,previous['episode'])
                return run_agent_turn(command.text, session_runtime, attachments=command.attachments)
        finally:
            session_runtime.check_cancelled = None
            lock.release()

    run.clear_session = lambda command: sessions.clear(key(command))
    run.task_store_enabled = True
    return run


class CommunicationWorker:
    def __init__(
        self,
        store: CommunicationStore,
        adapters: dict[str, CommunicationAdapter],
        command_runner: CommandRunner,
        session_clearer: Callable[[AgentCommand], None] | None = None,
    ) -> None:
        self.store = store
        self.adapters = adapters
        self.command_runner = command_runner
        self.session_clearer = session_clearer
        self._threads: list[threading.Thread] = []
        self._threads_lock = threading.Lock()
        self._admission_lock = threading.Lock()

    def _send_reply(self, command: AgentCommand, text: str, is_error: bool = False) -> None:
        key = self.store.queue_delivery(command, text, self.store.reply_context(command.source_message_id), is_error)
        self.deliver_pending(key)

    def deliver_pending(self, delivery_id=None):
        self.store.cleanup_expired()
        for row in self.store.pending_deliveries(delivery_id):
            if not self.store.claim_delivery(row['id']):
                continue
            try:
                import json
                self.adapters[row['platform']].send_message(OutboundMessage(row['platform'], row['conversation'], row['text'], reply_context=json.loads(row['reply_context'])))
                self.store.finish_delivery(row['id'], 'sent')
            except Exception as error:
                # A timeout may occur after the platform accepted a message. Do not blindly retry.
                self.store.finish_delivery(row['id'], 'uncertain', type(error).__name__)
                print(f"Delivery requires verification: id={row['id']} error={type(error).__name__}")

    def process_next(self) -> AgentCommand | None:
        with self._admission_lock:
            return self._process_next()

    def _process_next(self) -> AgentCommand | None:
        self.recover_tasks()
        from agent_execution.budgets import numeric
        limit=int(numeric('AGENT_TASK_CONCURRENCY',4))
        with self._threads_lock:
            busy=len(self._threads)>=limit
        pending = self.store.pending_commands(limit=64 if busy else 1)
        if busy:
            # Control commands remain reachable while ordinary jobs wait durably.
            pending=[command for command in pending if command.text.strip().split(maxsplit=1)[0:1]
                and command.text.strip().split(maxsplit=1)[0] in {'/status','/cancel','/clear','/reset','/approvals','/approve','/deny','/agents','/tasks','/steps','/budget','/cancel-task','/delivery'}]
        if not pending:
            return None
        command = pending[0]
        if not self.store.mark_command_running(command.command_id):
            return None

        text = command.text.strip()
        from agent_execution.approvals import human_command
        from permissions import get_user_role, set_current_role
        identity = f"{command.platform}:{command.sender_id}"
        with set_current_role(get_user_role(identity)):
            decision = human_command(text, identity, f"{command.platform}:{command.conversation_id}")
        if decision is not None:
            self.store.complete_command(command.command_id, decision)
            self._send_reply(command, decision)
            return self.store.command_by_id(command.command_id)
        if text.startswith("/status"):
            jobs = self.store.query_jobs_status(limit=5, owner=command)
            lines = ["📋 Recent Jobs:"]
            for j in jobs:
                status_icon = "⏳" if j.status == "pending" else "🏃" if j.status == "running" else "✅" if j.status == "completed" else "❌"
                lines.append(f"{status_icon} [{j.status}] {j.command_id[:8]}: {j.text[:30]}")
            result_text = "\n".join(lines)
            self.store.complete_command(command.command_id, result_text)
            self._send_reply(command, result_text)
            return self.store.command_by_id(command.command_id)

        if text == "/clear" or text == "/reset":
            if getattr(self.command_runner,'task_store_enabled',False):
                from agent_execution.tasks import TaskStore, task_owner
                from agent_execution.policy import ExecutionContext
                TaskStore().cancel_session(task_owner(ExecutionContext(source='communication',identity=identity,session=f'{command.platform}:{command.conversation_id}',project=command.project)))
            self.store.cancel_session(command)
            if hasattr(self.command_runner, "clear_session"):
                self.command_runner.clear_session(command)
            elif self.session_clearer is not None:
                self.session_clearer(command)
            self.store.clear_suspended_command(command.conversation_id, owner=command)
            result_text = "✅ 對話 session 已清除，後續訊息將從新的上下文開始。"
            self.store.complete_command(command.command_id, result_text)
            self._send_reply(command, result_text)
            return self.store.command_by_id(command.command_id)
            
        elif text.startswith("/cancel"):
            parts = text.split()
            if len(parts) > 1:
                target_id = parts[1]
                resolved_id = self.store.resolve_command_id(target_id, owner=command)
                if resolved_id:
                    if self.store.cancel_command(resolved_id, owner=command):
                        result_text = f"✅ Cancel requested for job {resolved_id[:8]}"
                    else:
                        result_text = f"❌ Could not cancel job {resolved_id[:8]} (may be already completed or cancelled)"
                else:
                    result_text = f"❌ Job {target_id} not found."
            else:
                result_text = "❌ Please specify a job id: /cancel <job_id>"
                
            self.store.complete_command(command.command_id, result_text)
            self._send_reply(command, result_text)
            return self.store.command_by_id(command.command_id)

        suspended_job = self.store.get_suspended_command(command.conversation_id, owner=command) if not text.startswith("/") else None

        import json

        def background_run():
            try:
                from dataclasses import replace
                loaded_command = replace(command, attachments=self.store.load_attachments(command.command_id, command))
                resumed_messages = None
                target_command = loaded_command
                
                if suspended_job:
                    suspended_id, state_json, tool_call_id = suspended_job
                    try:
                        state = json.loads(state_json)
                        messages = state.get("messages", [])
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": text
                        })
                        resumed_messages = messages
                        if not self.store.mark_command_running(suspended_id):
                            raise PermissionError("Suspended job was already resumed or cancelled")
                        self.store.complete_command(command.command_id, "Forwarded to suspended job")
                        target_command = self.store.command_by_id(suspended_id)
                    except Exception as e:
                        resumed_messages = None
                        print(f"Error resuming job: {e}")
                
                import inspect
                try:
                    inspect.signature(self.command_runner).bind(target_command, resumed_messages)
                except TypeError:
                    turn_result = self.command_runner(target_command)
                else:
                    turn_result = self.command_runner(target_command, resumed_messages)
                if self.store.is_cancel_requested(target_command.command_id):
                    self.store.complete_command(target_command.command_id, "Task cancelled")
                    self._send_reply(target_command, "Task cancelled")
                    return
                
                if isinstance(turn_result, str):
                    self.store.complete_command(target_command.command_id, turn_result)
                    self._send_reply(target_command, turn_result)
                elif getattr(turn_result, "suspended", False):
                    state_str = json.dumps(turn_result.suspended_state)
                    self.store.suspend_command(target_command.command_id, state_str, turn_result.suspended_tool_call_id)
                    self._send_reply(target_command, turn_result.reply)
                else:
                    reply = getattr(turn_result, "reply", str(turn_result))
                    self.store.complete_command(target_command.command_id, reply)
                    self._send_reply(target_command, reply)
            except Exception as error:
                import traceback
                traceback.print_exc()
                error_text = f"Command failed: {error}"
                c_id = target_command.command_id if 'target_command' in locals() else command.command_id
                cmd = target_command if 'target_command' in locals() else command
                self.store.fail_command(c_id, str(error))
                self._send_reply(cmd, error_text, is_error=True)
            finally:
                current = threading.current_thread()
                with self._threads_lock:
                    self._threads = [thread for thread in self._threads if thread is not current]

        # Run normal agent commands in a background thread so the worker can keep processing /cancel and /status
        thread = threading.Thread(target=copy_context().run, args=(background_run,))
        thread.daemon = True
        with self._threads_lock:
            self._threads.append(thread)
        thread.start()
        
        return self.store.command_by_id(command.command_id)

    def recover_tasks(self):
        if not getattr(self.command_runner,'task_store_enabled',False):
            return
        from agent_execution.policy import ExecutionContext
        from agent_execution.tasks import TaskStore, task_owner
        import json
        import time
        tasks = TaskStore()
        for command in self.store.recoverable_commands():
            owner = task_owner(ExecutionContext(source='communication',identity=f'{command.platform}:{command.sender_id}',session=f'{command.platform}:{command.conversation_id}',project=command.project))
            try:
                row = tasks.get(owner,command.command_id)
            except KeyError:
                continue
            if row['lease_until'] and row['lease_until'] > time.time():
                continue
            if row['state'] in {'completed','failed','cancelled'}:
                reply = row['reply'] or 'Task ended without a reply'
                if row['state']=='failed':
                    self.store.fail_command(command.command_id,reply)
                else:
                    if row['state']=='cancelled':
                        self.store.cancel_command(command.command_id)
                    self.store.complete_command(command.command_id,reply)
                self._send_reply(command,reply)
            elif row['phase'] in {'tool_started','skill_started'} or row['state']=='uncertain':
                tasks.mark_uncertain(owner,command.command_id)
                self.store.set_recovery_state(command.command_id,'recovery_required')
            elif row['state']=='suspended':
                self.store.suspend_command(command.command_id,json.dumps({'messages':row['checkpoint']['messages'],'episode_id':row['episode']}),row['checkpoint']['suspended_tool_call_id'])
                self._send_reply(command,row['reply'] or 'Human input is required')
            else:
                self.store.set_recovery_state(command.command_id,'pending')

    def drain(self, timeout: float = 2.0) -> None:
        end_time = None
        if timeout is not None:
            import time
            end_time = time.monotonic() + timeout

        while True:
            with self._threads_lock:
                threads = list(self._threads)
            if not threads:
                return
            for thread in threads:
                join_timeout = 0.1
                if end_time is not None:
                    import time
                    remaining = end_time - time.monotonic()
                    if remaining <= 0:
                        return
                    join_timeout = min(join_timeout, remaining)
                thread.join(join_timeout)


def enqueue_adapter_events(
    store: CommunicationStore,
    adapter: CommunicationAdapter,
    headers: dict[str, str],
    body: bytes,
    query: dict[str, str] | None = None,
    allowed_senders: Iterable[str] | None = None,
) -> list[AgentCommand]:
    if not adapter.verify_request(headers, body, query):
        raise PermissionError(f"{adapter.platform} request verification failed")
    return _enqueue_verified_events(store, adapter, headers, body, query, allowed_senders)


def _enqueue_verified_events(store, adapter, headers, body, query=None, allowed_senders=None):
    """Trusted transport only: webhook verification or authenticated Bot API polling."""
    commands = []
    allowed = normalize_allowed_senders(allowed_senders)
    for inbound in adapter.parse_events(headers, body, query):
        from dataclasses import replace
        import os
        inbound = replace(inbound, project=os.environ.get("COMM_PROJECT", "default"))
        if allowed is not None and inbound.sender_key not in allowed:
            raise PermissionError(f"{inbound.platform} sender is not allowed")
        command, inserted = store.ingest_inbound_message(inbound)
        if inserted:
            commands.append(command)
    return commands


def normalize_allowed_senders(allowed_senders: Iterable[str] | None) -> set[str] | None:
    if allowed_senders is None:
        return None
    normalized = {sender.strip() for sender in allowed_senders if sender and sender.strip()}
    return normalized or None
