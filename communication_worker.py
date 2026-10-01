from __future__ import annotations

from typing import Callable

from communication_models import AgentCommand, CommunicationAdapter, OutboundMessage
from communication_store import CommunicationStore


CommandRunner = Callable[[AgentCommand], str]


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

    def process_next(self) -> AgentCommand | None:
        pending = self.store.pending_commands(limit=1)
        if not pending:
            return None
        command = pending[0]
        self.store.mark_command_running(command.command_id)
        try:
            result_text = self.command_runner(command)
            self.store.complete_command(command.command_id, result_text)
            adapter = self.adapters[command.platform]
            adapter.send_message(
                OutboundMessage(
                    platform=command.platform,
                    conversation_id=command.conversation_id,
                    text=result_text,
                    reply_to_message_id=str(command.source_message_id) if command.source_message_id is not None else None,
                )
            )
            self.store.record_outbound_message(
                command.platform,
                command.conversation_id,
                result_text,
                platform_message_id=f"{command.command_id}:outbound",
            )
        except Exception as error:
            self.store.fail_command(command.command_id, str(error))
        return self.store.command_by_id(command.command_id)


def enqueue_adapter_events(
    store: CommunicationStore,
    adapter: CommunicationAdapter,
    headers: dict[str, str],
    body: bytes,
    query: dict[str, str] | None = None,
) -> list[AgentCommand]:
    if not adapter.verify_request(headers, body, query):
        raise PermissionError(f"{adapter.platform} request verification failed")
    commands = []
    for inbound in adapter.parse_events(headers, body, query):
        command, inserted = store.ingest_inbound_message(inbound)
        if inserted:
            commands.append(command)
    return commands
