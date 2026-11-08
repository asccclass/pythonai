"""Explicit, uncached office settings; no environment changes or startup I/O."""

from collections.abc import Mapping
from dataclasses import dataclass
import os


@dataclass(frozen=True)
class OfficeSettings:
    """Admission limits for explicit dispatch, not a request to start a worker."""

    enabled: bool = False
    owner_concurrency: int = 1
    global_concurrency: int = 4
    queue_limit: int = 100
    approval_ttl_seconds: int = 300

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("Office enabled must be a boolean")
        for name, maximum in (
            ("owner_concurrency", 64), ("global_concurrency", 64),
            ("queue_limit", 10000), ("approval_ttl_seconds", 604800),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"Office {name} must be an integer in 1..{maximum}")
        if self.owner_concurrency > self.global_concurrency:
            raise ValueError("Office owner concurrency exceeds global concurrency")

    @classmethod
    def load(cls, environ: Mapping[str, str] | None = None) -> "OfficeSettings":
        """Only 0/1 enables the flag; malformed configuration fails closed.

        Configuration is trusted operator input. This flag is not yet connected
        to a server startup hook; enabled=True cannot bypass later admission.
        """
        values = os.environ if environ is None else environ
        enabled = values.get("AGENT_OFFICE_ENABLED", "0")
        if not isinstance(enabled, str) or enabled not in {"0", "1"}:
            raise ValueError("AGENT_OFFICE_ENABLED must be 0 or 1")
        options = {}
        for name in ("owner_concurrency", "global_concurrency", "queue_limit", "approval_ttl_seconds"):
            key = "AGENT_OFFICE_" + name.upper()
            raw = values.get(key)
            if raw is not None:
                if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal():
                    raise ValueError(f"{key} must contain an unsigned decimal integer")
                try:
                    options[name] = int(raw)
                except ValueError as error:
                    raise ValueError(f"{key} is too large") from error
        return cls(enabled=enabled == "1", **options)
