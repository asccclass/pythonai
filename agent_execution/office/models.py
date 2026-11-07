"""Office directory inputs and public projections; no credentials or paths."""

from dataclasses import dataclass
from enum import StrEnum
import re
import unicodedata

from .contracts import Provider, _identifier, _text


def display_name(value: str) -> str:
    _text(value, "name")
    value = unicodedata.normalize("NFC", value.strip())
    if not value.isprintable() or len(value.encode("utf-8")) > 512:
        raise ValueError("Name must be printable and at most 512 UTF-8 bytes")
    return value


def name_key(value: str) -> str:
    return unicodedata.normalize("NFKC", display_name(value)).casefold()


@dataclass(frozen=True)
class StaffConfig:
    name: str
    provider: Provider = Provider.PYTHONAI
    model: str | None = None
    allowed_tools: frozenset[str] = frozenset()
    enabled: bool = False

    def __post_init__(self):
        object.__setattr__(self, "name", display_name(self.name))
        if not isinstance(self.provider, Provider):
            raise ValueError("provider must be a Provider")
        if self.model is not None and (
            not isinstance(self.model, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,127}", self.model) is None
        ):
            raise ValueError("Invalid model identifier")
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        if not isinstance(self.allowed_tools, frozenset) or len(self.allowed_tools) > 64:
            raise ValueError("allowed_tools must be a frozenset of at most 64 tool names")
        for tool in self.allowed_tools:
            _identifier(tool, "tool")


@dataclass(frozen=True)
class TeamConfig:
    name: str
    enabled: bool = False

    def __post_init__(self):
        object.__setattr__(self, "name", display_name(self.name))
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be a boolean")


class TeamRole(StrEnum):
    MEMBER = "member"
    LEAD = "lead"


@dataclass(frozen=True)
class Membership:
    staff_id: str
    role: TeamRole

    def to_dict(self) -> dict:
        return {"staff_id": self.staff_id, "role": self.role.value}


@dataclass(frozen=True)
class StaffRecord:
    id: str
    config: StaffConfig
    workspace_project: str
    revision: int

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.config.name, "provider": self.config.provider.value,
            "model": self.config.model, "allowed_tools": sorted(self.config.allowed_tools),
            "enabled": self.config.enabled, "workspace_project": self.workspace_project,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class TeamRecord:
    id: str
    config: TeamConfig
    revision: int
    members: tuple[Membership, ...]

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.config.name, "enabled": self.config.enabled,
            "revision": self.revision, "members": [member.to_dict() for member in self.members],
        }
