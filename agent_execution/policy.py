from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import wraps
import inspect
import json
from pathlib import Path
from typing import Any, Callable

from permissions import check_tool_permission, current_role


class PolicyDenied(PermissionError):
    """An action denied before invoking the tool."""


@dataclass(frozen=True)
class ExecutionContext:
    source: str = "local"
    identity: str = "local"
    memory: Any = None
    episode_id: int | None = None
    trusted_assets: tuple[Path, ...] = ()


_context: ContextVar[ExecutionContext] = ContextVar("execution_context", default=ExecutionContext())
PATH_KEYS = {"path", "file_path", "image_path", "project_path", "project_name", "cwd",
             "env_file", "test_target", "directory", "directory_path", "output_path"}


@contextmanager
def execution_scope(**changes):
    token = _context.set(replace(_context.get(), **changes))
    try:
        yield _context.get()
    finally:
        _context.reset(token)


def resolve_path(path: str | Path) -> Path:
    # Import lazily: base imports Skills which in turn uses this policy.
    from base import resolve_workspace_path
    return resolve_workspace_path(path)


def current_context() -> ExecutionContext:
    return _context.get()


def git_workspace() -> Path:
    """Reject Git's implicit traversal into a repository outside the workspace."""
    import subprocess
    root = resolve_path(".")
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                            capture_output=True, text=True, encoding="utf-8", check=True)
    return resolve_path(result.stdout.strip())


def _validate_paths(arguments: dict[str, Any], trusted_assets: tuple[Path, ...]) -> None:
    from base import resolve_workspace_or_trusted_path, validate_command_paths
    for key, value in arguments.items():
        if key in {"memory", "episode_id"}:
            continue
        if key == "trusted_asset_roots":
            roots = tuple(Path(item).resolve() for item in value or [])
            if any(root not in trusted_assets for root in roots):
                raise PolicyDenied("Untrusted caller cannot grant trusted_asset_roots")
        elif key in PATH_KEYS or key.endswith(("_path", "_dir")):
            if value not in (None, ""):
                # Only run_command may read/use trusted executable assets outside workspace.
                resolve_workspace_or_trusted_path(value, list(trusted_assets))
        elif key == "command" and isinstance(value, list):
            validate_command_paths(value, list(trusted_assets))
        elif isinstance(value, dict):
            _validate_paths(value, ())
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _validate_paths(item, ())


def _audit_denial(name: str, reason: str) -> None:
    context = _context.get()
    metadata = {"tool": name, "role": current_role(), "source": context.source,
                "identity": context.identity, "reason": reason}
    print(f"AUDIT: Permission denied: {json.dumps(metadata, ensure_ascii=False)}")
    if context.memory is not None and context.episode_id is not None:
        context.memory.add_event(context.episode_id, "permission_denied", content=reason,
                                 metadata=metadata)


def execute_tool(name: str, arguments: dict[str, Any], function: Callable[..., Any]) -> Any:
    try:
        if not check_tool_permission(name):
            raise PolicyDenied(f"Role '{current_role()}' cannot execute {name}")
        assets = _context.get().trusted_assets if name == "run_command" else ()
        _validate_paths(arguments, assets)
    except PermissionError as error:
        _audit_denial(name, str(error))
        raise PolicyDenied(str(error)) from error
    return function(**arguments)


def policy_tool(name: str):
    """Wrap registered tools and direct public calls without changing their signature."""
    def decorate(function):
        if getattr(function, "_policy_tool_name", None) == name:
            return function
        signature = inspect.signature(function)

        @wraps(function)
        def wrapped(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            arguments = dict(bound.arguments)
            for parameter in signature.parameters.values():
                if parameter.kind == inspect.Parameter.VAR_KEYWORD:
                    arguments.update(arguments.pop(parameter.name, {}))
            return execute_tool(name, arguments, lambda **_values: function(*args, **kwargs))

        wrapped._policy_tool_name = name
        return wrapped
    return decorate
