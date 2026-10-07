from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class InboundAttachment:
    filename: str
    content_type: str
    data: bytes
    platform_file_id: str | None = None

    @property
    def size_bytes(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class InboundMessage:
    platform: str
    platform_message_id: str
    conversation_id: str
    sender_id: str
    text: str
    raw_payload: dict[str, Any] = field(default_factory=dict)
    received_at: str | None = None
    attachments: tuple[InboundAttachment, ...] = field(default_factory=tuple)

    @property
    def idempotency_key(self) -> str:
        return f"{self.platform}:{self.platform_message_id}"

    @property
    def sender_key(self) -> str:
        return f"{self.platform}:{self.sender_id}"


@dataclass(frozen=True)
class AgentCommand:
    command_id: str
    platform: str
    conversation_id: str
    sender_id: str
    text: str
    status: str = "pending"
    requires_confirmation: bool = False
    source_message_id: int | None = None
    attachments: tuple[InboundAttachment, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class OutboundMessage:
    platform: str
    conversation_id: str
    text: str
    reply_to_message_id: str | None = None


class CommunicationAdapter(Protocol):
    platform: str

    def verify_request(self, headers: dict[str, str], body: bytes, query: dict[str, str] | None = None) -> bool:
        ...

    def parse_events(
        self,
        headers: dict[str, str],
        body: bytes,
        query: dict[str, str] | None = None,
    ) -> list[InboundMessage]:
        ...

    def send_message(self, message: OutboundMessage) -> None:
        ...
