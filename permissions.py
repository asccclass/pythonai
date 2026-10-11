import os
import json
from contextvars import ContextVar
from contextlib import contextmanager

ROLE_VIEWER = "viewer"
ROLE_OPERATOR = "operator"
ROLE_ADMIN = "admin"

_current_user_role: ContextVar[str] = ContextVar("current_user_role", default=ROLE_ADMIN)

def get_user_role(sender_id: str) -> str:
    config_path = os.environ.get("ROLES_CONFIG", "")
    if config_path:
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                roles = json.load(f)
                role = roles.get(sender_id, ROLE_VIEWER)
                return role if role in {ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN} else ROLE_VIEWER
        except Exception:
            return ROLE_VIEWER
    # An explicit roles file remains authoritative, including absent identities.
    # Without one, honor the communication admin list advertised in .env.example.
    platform, separator, identity = sender_id.partition(":")
    if separator and identity and platform in {"telegram", "discord", "line"}:
        admins = {entry.strip() for entry in os.environ.get("COMM_ADMIN_SENDERS", "").split(",")
                  if entry.strip()}
        if sender_id in admins:
            return ROLE_ADMIN
    # This lookup is for remote identities. Local execution keeps its ContextVar default.
    return ROLE_VIEWER

@contextmanager
def set_current_role(role: str):
    token = _current_user_role.set(role)
    try:
        yield
    finally:
        _current_user_role.reset(token)

def current_role() -> str:
    return _current_user_role.get()

def check_tool_permission(tool_name: str) -> bool:
    role = current_role()
    if role == ROLE_ADMIN:
        return True
    
    if role == ROLE_VIEWER:
        # viewers cannot use any write/exec tools, maybe allow read? 
        # For simplicity, no tools or just read_file.
        return tool_name in ("read_file", "ask_user")
        
    if role == ROLE_OPERATOR:
        allowed = {"read_file", "list_files", "fetch_url", "run_skill", "ask_user"}
        return tool_name in allowed
        
    return False
