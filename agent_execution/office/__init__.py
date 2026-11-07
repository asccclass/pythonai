"""Office domain boundary; importing this package starts no office services.

These contracts do not authenticate callers or authorize execution. Transport
handlers must establish the existing execution_scope before using this package.
"""

from .config import OfficeSettings
from .contracts import OwnerScope, Provider, RuntimeCapabilities, ResumeMode

__all__ = [
    "OfficeSettings", "OwnerScope", "Provider", "RuntimeCapabilities", "ResumeMode",
]
