"""Validated office values, independent of provider SDKs and storage creation."""

from dataclasses import dataclass
from enum import StrEnum
import json
from pathlib import Path


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be a nonempty string without NUL")


def _identifier(value: str, name: str) -> None:
    _text(value, name)
    if len(value.encode("utf-8")) > 256:
        raise ValueError(f"{name} exceeds 256 UTF-8 bytes")


@dataclass(frozen=True)
class OwnerScope:
    """The existing TaskStore owner tuple, not an identity credential.

    HTTP/WS payloads must never select this value. Use from_current() only after
    the transport has established its authenticated execution_scope.
    """

    source: str
    identity: str
    project: str
    session: str

    def __post_init__(self):
        for name in ("source", "identity", "project", "session"):
            _text(getattr(self, name), name)

    @classmethod
    def from_current(cls) -> "OwnerScope":
        from agent_execution.policy import current_context
        context = current_context()
        return cls(context.source, context.identity, context.project, context.session)

    @property
    def key(self) -> str:
        # Match tasks.task_owner exactly, including JSON Unicode escaping.
        return json.dumps(
            [self.source, self.identity, self.project, self.session], separators=(",", ":"),
        )


class Provider(StrEnum):
    PYTHONAI = "pythonai"
    CODEX = "codex"
    CLAUDE = "claude"


class ResumeMode(StrEnum):
    NONE = "none"
    SESSION = "session"  # Can reattach a provider conversation, not pending effects.
    DURABLE = "durable"  # Restores confirmed execution boundaries after restart.


class UnsupportedCapability(ValueError):
    """A runtime cannot satisfy the requested admission requirements."""


@dataclass(frozen=True)
class RuntimeCapabilities:
    """Adapter claims; production claims require version-specific live evidence.

    Approval support alone is insufficient: every path to tools must be covered.
    Capabilities never replace role, path, budget or sandbox authorization.
    """

    resume: ResumeMode = ResumeMode.NONE
    tool_approval: bool = False
    all_tools_gated: bool = False
    usage: bool = False
    mcp: bool = False
    attachments: bool = False

    def __post_init__(self):
        if not isinstance(self.resume, ResumeMode):
            raise ValueError("resume must be a ResumeMode")
        for name in ("tool_approval", "all_tools_gated", "usage", "mcp", "attachments"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        if self.all_tools_gated and not self.tool_approval:
            raise ValueError("Gating all tools requires tool approval support")

    def require(self, *, writes: bool = False, resume: ResumeMode = ResumeMode.NONE) -> None:
        """Reject unsupported work before starting a provider or sending a turn."""
        if type(writes) is not bool or not isinstance(resume, ResumeMode):
            raise ValueError("Invalid runtime admission requirements")
        if writes and not (self.tool_approval and self.all_tools_gated):
            raise UnsupportedCapability("Managed writes require approval before every tool path")
        if resume == ResumeMode.DURABLE and self.resume != ResumeMode.DURABLE:
            raise UnsupportedCapability("Runtime cannot restore durable execution boundaries")
        if resume == ResumeMode.SESSION and self.resume == ResumeMode.NONE:
            raise UnsupportedCapability("Runtime cannot resume a provider session")


@dataclass(frozen=True)
class RuntimeStartRequest:
    """Trusted application input after owner/workspace/role/budget admission.

    An absolute path is only a shape check, not workspace authorization. Consumers
    must still resolve it through existing policy and lifecycle checks.
    """

    owner: OwnerScope
    staff_id: str
    task_id: str
    generation: int
    workspace: Path
    provider: Provider = Provider.PYTHONAI
    model: str | None = None
    allowed_tools: frozenset[str] = frozenset()

    def __post_init__(self):
        if not isinstance(self.owner, OwnerScope):
            raise ValueError("owner must be an OwnerScope")
        _identifier(self.staff_id, "staff_id")
        _identifier(self.task_id, "task_id")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("generation must be a positive integer")
        if not isinstance(self.workspace, Path) or not self.workspace.is_absolute():
            raise ValueError("workspace must be an absolute Path")
        if not isinstance(self.provider, Provider):
            raise ValueError("provider must be a Provider")
        if self.model is not None:
            _text(self.model, "model")
        if not isinstance(self.allowed_tools, frozenset):
            raise ValueError("allowed_tools must be an immutable frozenset")
        for tool in self.allowed_tools:
            _identifier(tool, "tool")


@dataclass(frozen=True)
class TurnReceipt:
    """An accepted send, not a completion or successful verification."""

    turn_id: str

    def __post_init__(self):
        _identifier(self.turn_id, "turn_id")


@dataclass(frozen=True)
class ToolDecision:
    """A resolved application decision for one exact provider tool request.

    This DTO is not a grant. The office approval service must authenticate and
    consume its exact-action grant before forwarding an allow to the adapter.
    It cannot rewrite the tool arguments after approval.
    """

    session_id: str
    generation: int
    turn_id: str
    tool_call_id: str
    allow: bool
    reason: str = ""

    def __post_init__(self):
        for name in ("session_id", "turn_id", "tool_call_id"):
            _identifier(getattr(self, name), name)
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("generation must be a positive integer")
        if type(self.allow) is not bool:
            raise ValueError("allow must be a boolean")
        if not isinstance(self.reason, str) or "\x00" in self.reason:
            raise ValueError("reason must be a string without NUL")
        if not self.allow:
            _text(self.reason, "denial reason")
