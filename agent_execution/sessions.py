"""Independent conversation state and physical tenant memory databases."""
import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import threading


def tenant_memory_path(identity, project="default", conversation=""):
    storage = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
    root = Path(os.environ.get("AGENT_TENANT_ROOT") or storage / "PythonAI" / "tenants")
    key = hashlib.sha256(json.dumps([identity, project, conversation], ensure_ascii=False).encode()).hexdigest()
    return root / key / "memory.db"


def project_workspace(project):
    configured = json.loads(os.environ.get("AGENT_PROJECT_WORKSPACES", "{}"))
    if not isinstance(configured, dict):
        raise PermissionError("AGENT_PROJECT_WORKSPACES must be a JSON object")
    if project in configured:
        path = configured[project]
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise PermissionError("Project workspace must be an absolute path")
        return Path(path).resolve()
    if configured or project != "default":
        raise PermissionError("Project has no authorized workspace mapping")
    from base import workspace_root
    return workspace_root()


def remote_memory_path():
    from .policy import current_context
    context = current_context()
    if context.identity == "local" and context.source not in {"api", "communication", "websocket"}:
        return None
    conversation = context.session if context.source == "communication" else ""
    return tenant_memory_path(context.identity, context.project, conversation)


class RuntimeSessions:
    def __init__(self, template):
        self.template = template
        self.states = {}
        self.lock = threading.Lock()
        self.system_messages = copy.deepcopy([message for message in getattr(template, "messages", []) if message.get("role") == "system"])

    def get(self, key):
        with self.lock:
            if key not in self.states:
                from memory import MemoryStore
                memory = MemoryStore(tenant_memory_path(key[0], key[2], key[1])) if self.template.memory is not None else None
                searcher = copy.copy(self.template.memory_searcher)
                if searcher is not None and hasattr(searcher, "store"):
                    searcher.store = memory
                    searcher.last_stats = {}
                runtime = replace(self.template, messages=copy.deepcopy(self.system_messages), memory=memory,
                                  memory_searcher=searcher, check_cancelled=None)
                self.states[key] = (runtime, threading.Lock())
            return self.states[key]

    def clear(self, key):
        with self.lock:
            self.states.pop(key, None)
