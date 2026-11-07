"""Office runtime protocol, separate from agent_runtime.AgentRuntime.

No implementation is registered by this module. External agent runtimes own
their agent loop; adapters must not wrap it in a second PythonAI agent loop.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
import json
from typing import Protocol

from ..contracts import RuntimeCapabilities, RuntimeStartRequest, ToolDecision, TurnReceipt, _identifier


class RuntimeEventType(StrEnum):
    SESSION_STARTED = "session.started"
    TEXT_DELTA = "text.delta"
    TOOL_PROPOSED = "tool.proposed"
    APPROVAL_WAITING = "approval.waiting"
    USAGE_UPDATED = "usage.updated"
    TURN_COMPLETED = "turn.completed"
    TURN_FAILED = "turn.failed"
    SESSION_CLOSED = "session.closed"


@dataclass(frozen=True)
class RuntimeEvent:
    """Internal provider event, not an authorized client command or WS envelope.

    A canonical JSON snapshot prevents callers mutating recorded event contents.
    The transport must scope it to the admitted owner and assign its own durable
    sequence. Text and provider output remain untrusted data.
    """

    event_id: str
    type: RuntimeEventType
    session_id: str
    generation: int
    turn_id: str | None = None
    payload_json: str = field(default="{}", repr=False)

    def __post_init__(self):
        for name in ("event_id", "session_id"):
            _identifier(getattr(self, name), name)
        if not isinstance(self.type, RuntimeEventType):
            raise ValueError("type must be a RuntimeEventType")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("generation must be a positive integer")
        if self.turn_id is not None:
            _identifier(self.turn_id, "turn_id")
        if self.type not in {RuntimeEventType.SESSION_STARTED, RuntimeEventType.SESSION_CLOSED} and self.turn_id is None:
            raise ValueError("Turn events require a turn_id")
        if not isinstance(self.payload_json, str) or len(self.payload_json.encode("utf-8")) > 1024 * 1024:
            raise ValueError("Runtime event payload exceeds 1 MiB or is not JSON text")
        try:
            payload = json.loads(
                self.payload_json, parse_constant=self._reject_constant,
                object_pairs_hook=self._unique_object,
            )
            if not isinstance(payload, dict):
                raise ValueError("Runtime event payload must be a JSON object")
            canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if len(canonical.encode("utf-8")) > 1024 * 1024:
                raise ValueError("Canonical event payload exceeds 1 MiB")
        except (ValueError, RecursionError) as error:
            raise ValueError("Invalid runtime event payload") from error
        object.__setattr__(self, "payload_json", canonical)

    @staticmethod
    def _reject_constant(value):
        raise ValueError(f"Nonfinite JSON constant: {value}")

    @staticmethod
    def _unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON object key")
            result[key] = value
        return result

    @property
    def payload(self) -> dict:
        return json.loads(self.payload_json)


class AgentSessionRuntime(Protocol):
    """One managed staff session; every send is serialized by the dispatcher.

    start returns the provider session ID. send returns an acceptance receipt;
    only a terminal event ends that turn, and office acceptance remains separate.
    events is a single-consumer async stream. interrupt targets an exact turn;
    close must be idempotent and release owned resources. None of these methods
    grants permission to execute tools.
    """

    @property
    def capabilities(self) -> RuntimeCapabilities: ...

    async def start(self, request: RuntimeStartRequest) -> str: ...

    async def send(self, text: str, *, request_id: str) -> TurnReceipt: ...

    def events(self) -> AsyncIterator[RuntimeEvent]: ...

    async def interrupt(self, turn_id: str) -> None: ...

    async def close(self) -> None: ...


class ResumableRuntime(AgentSessionRuntime, Protocol):
    """Optional reattachment; capability must declare session vs durable resume."""

    async def resume(self, session_id: str, request: RuntimeStartRequest) -> str: ...


class ToolApprovalRuntime(AgentSessionRuntime, Protocol):
    """Optional approval reply; mismatched session/generation/turn is rejected.

    A request remains blocked until a matching decision or cancellation arrives.
    Implementations must fail closed on disconnect and unknown decision outcomes.
    """

    async def decide_tool(self, decision: ToolDecision) -> None: ...
