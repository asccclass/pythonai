from __future__ import annotations

from typing import Callable, Iterable

from agent_runtime import AgentRuntime, run_agent_turn, resume_agent_turn, AgentTurnResult
from base import auto_approve_command_runs
from communication_models import AgentCommand, CommunicationAdapter, OutboundMessage
from communication_store import CommunicationStore


CommandRunner = Callable[[AgentCommand, list | None], AgentTurnResult]


def agent_runtime_command_runner(runtime: AgentRuntime, store: CommunicationStore) -> CommandRunner:
    def run(command: AgentCommand, resumed_messages: list | None = None) -> AgentTurnResult:
        from permissions import get_user_role, set_current_role
        role = get_user_role(f"{command.platform}:{command.sender_id}")
        runtime.check_cancelled = lambda: store.is_cancel_requested(command.command_id)
        with auto_approve_command_runs(), set_current_role(role):
            if resumed_messages is not None:
                return resume_agent_turn(resumed_messages, runtime)
            return run_agent_turn(command.text, runtime)

    return run


class CommunicationWorker:
    def __init__(
        self,
        store: CommunicationStore,
        adapters: dict[str, CommunicationAdapter],
        command_runner: CommandRunner,
    ) -> None:
        self.store = store
        self.adapters = adapters
        self.command_runner = command_runner

    def _send_reply(self, command: AgentCommand, text: str, is_error: bool = False) -> None:
        adapter = self.adapters[command.platform]
        adapter.send_message(
            OutboundMessage(
                platform=command.platform,
                conversation_id=command.conversation_id,
                text=text,
                reply_to_message_id=str(command.source_message_id) if command.source_message_id is not None else None,
            )
        )
        self.store.record_outbound_message(
            command.platform,
            command.conversation_id,
            text,
            platform_message_id=f"{command.command_id}:{'error' if is_error else 'outbound'}",
        )

    def process_next(self) -> AgentCommand | None:
        pending = self.store.pending_commands(limit=1)
        if not pending:
            return None
        command = pending[0]
        self.store.mark_command_running(command.command_id)

        text = command.text.strip()
        if text.startswith("/status"):
            jobs = self.store.query_jobs_status(limit=5)
            lines = ["📋 Recent Jobs:"]
            for j in jobs:
                status_icon = "⏳" if j.status == "pending" else "🏃" if j.status == "running" else "✅" if j.status == "completed" else "❌"
                lines.append(f"{status_icon} [{j.status}] {j.command_id[:8]}: {j.text[:30]}")
            result_text = "\\n".join(lines)
            self.store.complete_command(command.command_id, result_text)
            self._send_reply(command, result_text)
            return self.store.command_by_id(command.command_id)
            
        elif text.startswith("/cancel"):
            parts = text.split()
            if len(parts) > 1:
                target_id = parts[1]
                resolved_id = self.store.resolve_command_id(target_id)
                if resolved_id:
                    if self.store.cancel_command(resolved_id):
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

        suspended_job = self.store.get_suspended_command(command.conversation_id) if not text.startswith("/") else None

        import threading
        import json

        def background_run():
            try:
                resumed_messages = None
                target_command = command
                
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
                        self.store.complete_command(command.command_id, "Forwarded to suspended job")
                        target_command = self.store.command_by_id(suspended_id)
                        self.store.mark_command_running(suspended_id)
                    except Exception as e:
                        print(f"Error resuming job: {e}")
                
                turn_result = self.command_runner(target_command, resumed_messages)
                
                if turn_result.suspended:
                    state_str = json.dumps(turn_result.suspended_state)
                    self.store.suspend_command(target_command.command_id, state_str, turn_result.suspended_tool_call_id)
                    self._send_reply(target_command, turn_result.reply)
                else:
                    self.store.complete_command(target_command.command_id, turn_result.reply)
                    self._send_reply(target_command, turn_result.reply)
            except Exception as error:
                import traceback
                traceback.print_exc()
                error_text = f"Command failed: {error}"
                c_id = target_command.command_id if 'target_command' in locals() else command.command_id
                cmd = target_command if 'target_command' in locals() else command
                self.store.fail_command(c_id, str(error))
                self._send_reply(cmd, error_text, is_error=True)

        # Run normal agent commands in a background thread so the worker can keep processing /cancel and /status
        thread = threading.Thread(target=background_run)
        thread.daemon = True
        if not hasattr(self, "_threads"):
            self._threads = []
        self._threads.append(thread)
        thread.start()
        
        return self.store.command_by_id(command.command_id)


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
    commands = []
    allowed = normalize_allowed_senders(allowed_senders)
    for inbound in adapter.parse_events(headers, body, query):
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
