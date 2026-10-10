"""Authenticated transport boundary shared by ASGI and the legacy HTTP server."""
import hmac
import json
import os
import re
from urllib.parse import urlparse

from permissions import get_user_role, set_current_role
from .policy import execution_scope
from .approvals import ApprovalStore


def authenticate(headers):
    authorization = headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        return None
    token = authorization[7:]
    try:
        tokens = json.loads(os.environ.get("AGENT_API_TOKENS", "{}"))
        if not isinstance(tokens, dict):
            return None
        for candidate, identity in tokens.items():
            if len(candidate) >= 32 and isinstance(identity, str) and identity and hmac.compare_digest(token.encode(), candidate.encode()):
                return "api:" + identity
    except (ValueError, TypeError):
        pass
    return None


def authenticated_api(path, method, body, headers):
    identity = authenticate(headers)
    if identity is None:
        return 401, {"error": "unauthorized"}
    role = get_user_role(identity)
    project = headers.get("X-Agent-Project", "default")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", project):
        return 400, {"error": "invalid_project"}
    session = headers.get("X-Agent-Session", "dashboard")
    if not session or len(session) > 128:
        return 400, {"error": "invalid_session"}
    if method != "GET" and role != "admin":
        return 403, {"error": "forbidden"}
    route = urlparse(path).path.rstrip("/")
    session = f"{project}:{session}"
    with set_current_role(role), execution_scope(source="api", identity=identity, session=session, project=project):
        if route.startswith('/api/ops/'):
            if method!='GET' or role!='admin':return 403,{'error':'forbidden'}
            if route=='/api/ops/readiness':
                from .operations.doctor import readiness
                report=readiness();return (200 if report['ready'] else 503),report
            if route=='/api/ops/traces':
                from .operations.tracing import TraceStore
                store=TraceStore();return 200,{'summary':store.summary(),'spans':store.list()}
            return 404,{'error':'not_found'}
        if route.startswith('/api/review/'):
            from .review import handle_review
            return handle_review(route, method, role)
        if route == "/api/approvals" and method == "GET":
            return 200, ApprovalStore().pending(identity, session)
        parts = route.split("/")
        if len(parts) == 5 and parts[1:3] == ["api", "approvals"] and method == "POST":
            if parts[4] not in {"approve", "deny"}:
                return 404, {"error": "not_found"}
            try:
                ApprovalStore().decide(parts[3], identity, session, parts[4] == "approve")
            except PermissionError as error:
                return 403, {"error": "approval_rejected", "message": str(error)}
            return 200, {"ok": True}
        # Mutations of existing memories require a human decision over exact request bytes.
        if route.startswith("/api/memories/") and method in {"PUT", "DELETE", "POST"}:
            try:
                content = (body or b"").decode("utf-8") if isinstance(body, bytes) else (body or "")
            except UnicodeDecodeError:
                return 400, {"error": "invalid_utf8"}
            arguments = {"route": route, "method": method,
                         "body": content, "project": project}
            store = ApprovalStore()
            approval_id = headers.get("X-Agent-Approval", "")
            if not approval_id:
                key = store.request(identity, session, f"memory_api:{project}", arguments)
                return 409, {"error": "approval_required", "approval_id": key,
                             "operation": arguments, "expires_in": 300}
            try:
                store.consume(approval_id, identity, session, f"memory_api:{project}", arguments)
            except PermissionError as error:
                return 403, {"error": "approval_rejected", "message": str(error)}
        from web_api import handle_api_request
        return handle_api_request(path, method, body)
