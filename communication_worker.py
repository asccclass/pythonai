from __future__ import annotations

from typing import Callable, Iterable

from agent_runtime import AgentRuntime, run_agent_turn
from base import auto_approve_command_runs
from communication_models import AgentCommand, CommunicationAdapter, OutboundMessage
from communication_store import CommunicationStore


CommandRunner = Callable[[AgentCommand], str]


def agent_runtime_command_runner(runtime: AgentRuntime) -> CommandRunner:
    def run(command: AgentCommand) -> str:
        from permissions import get_user_role, set_current_role
        role = get_user_role(f"{command.platform}:{command.sender_id}")
        with auto_approve_command_runs(), set_current_role(role):
            return run_agent_turn(command.text, runtime).reply

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

        try:
            result_text = self.command_runner(command)
            self.store.complete_command(command.command_id, result_text)
            self._send_reply(command, result_text)
        except Exception as error:
            error_text = f"Command failed: {error}"
            self.store.fail_command(command.command_id, str(error))
            self._send_reply(command, error_text, is_error=True)
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
