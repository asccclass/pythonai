"""A per-turn bridge; model tools never execute arbitrary desktop commands."""
from agent_configuration import load_text, load_tool_text
from contextlib import contextmanager
from contextvars import ContextVar
import json

_bridge = ContextVar("desktop_bridge", default=None)
ACTIONS = {"system_info", "open_url", "open_path", "clipboard_write"}
SCHEMA = {"type": "function", "function": {"name": "desktop_tool",
    "description": load_tool_text('desktop_tool/description'),
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": sorted(ACTIONS)},
        "arguments": {"type": "object", "properties": {"url": {"type": "string"},
            "path": {"type": "string"}, "text": {"type": "string"}}, "additionalProperties": False}},
        "required": ["action"], "additionalProperties": False}}}


@contextmanager
def desktop_bridge(callback):
    token = _bridge.set(callback)
    try:
        yield
    finally:
        _bridge.reset(token)


def desktop_tool(action, arguments=None):
    from permissions import current_role
    arguments = {} if arguments is None else arguments
    if not isinstance(action, str) or action not in ACTIONS or not isinstance(arguments, dict):
        raise ValueError("Unknown desktop action or arguments")
    expected = {"system_info": set(), "open_url": {"url"}, "open_path": {"path"}, "clipboard_write": {"text"}}[action]
    if set(arguments) != expected or any(not isinstance(value, str) or not value or len(value.encode()) > 8192 for value in arguments.values()):
        raise ValueError("Invalid desktop tool arguments")
    if current_role() == "viewer" and action != "system_info":
        raise PermissionError("Viewer can only query system_info")
    if current_role() == "operator" and action == "clipboard_write":
        raise PermissionError("Clipboard writes require admin role")
    callback = _bridge.get()
    if callback is None:
        return {"success": False, "error": "No connected desktop in this execution context"}
    return callback(action, arguments)


def human_desktop_command(text):
    if not text.startswith("/desktop "):
        return None
    parts = text.split(maxsplit=2)
    action = parts[1] if len(parts) > 1 else ""
    value = parts[2] if len(parts) == 3 else ""
    argument = {"open_url": "url", "open_path": "path", "clipboard_write": "text"}.get(action)
    result = desktop_tool(action, {argument: value} if argument else {})
    return json.dumps(result, ensure_ascii=False)
