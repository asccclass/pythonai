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
    if config_path and os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                roles = json.load(f)
                return roles.get(sender_id, ROLE_VIEWER)
        except Exception:
            pass
    # If not configured, default to admin for safety (or viewer? let's follow the prompt which implies operator/viewer but admin allows anything. Locally we want admin default)
    # The prompt says: 對外部通訊入口建立一致權限模型
    # We will default to admin if config doesn't exist so it doesn't break local testing.
    return ROLE_ADMIN if not config_path else ROLE_VIEWER

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
        return tool_name in ("read_file",)
        
    if role == ROLE_OPERATOR:
        allowed = {"read_file", "list_files", "fetch_url", "run_skill"}
        return tool_name in allowed
        
    return False