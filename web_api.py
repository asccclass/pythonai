from __future__ import annotations

from contextlib import closing
import json
import sqlite3
from typing import Any
from urllib.parse import parse_qs, urlparse

from communication_store import communication_db_path
from memory import MemoryStore, memory_db_path


def get_dashboard_stats() -> dict[str, int]:
    stats: dict[str, int] = {}
    with closing(sqlite3.connect(memory_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        stats["semantic_memory_count"] = conn.execute(
            "SELECT COUNT(*) as c FROM semantic_memories WHERE archived_at IS NULL"
        ).fetchone()["c"]
        stats["low_confidence_memory_count"] = conn.execute(
            "SELECT COUNT(*) as c FROM semantic_memories WHERE archived_at IS NULL AND confidence < 0.3"
        ).fetchone()["c"]
        stats["procedure_count"] = conn.execute("SELECT COUNT(*) as c FROM procedures").fetchone()["c"]

    with closing(sqlite3.connect(communication_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        stats["pending_tasks"] = conn.execute(
            "SELECT COUNT(*) as c FROM agent_command_jobs WHERE status = 'pending'"
        ).fetchone()["c"]
        stats["running_tasks"] = conn.execute(
            "SELECT COUNT(*) as c FROM agent_command_jobs WHERE status = 'running'"
        ).fetchone()["c"]
        stats["failed_tasks"] = conn.execute(
            "SELECT COUNT(*) as c FROM agent_command_jobs WHERE status = 'failed'"
        ).fetchone()["c"]

    return stats


def get_recent_memories(limit: int = 50, include_archived: bool = False) -> list[dict[str, Any]]:
    store = MemoryStore()
    memories = store.active_semantic_memories()
    if include_archived:
        memories.extend(store.archived_semantic_memories())
        memories.sort(key=lambda memory: (str(memory.get("updated_at") or ""), int(memory.get("id") or 0)), reverse=True)
    return memories[:limit]


def get_recent_tasks(limit: int = 50) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(communication_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, text, status, created_at, last_error
            FROM agent_command_jobs
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def handle_api_request(
    path: str,
    method: str = "GET",
    body: bytes | str | None = None,
) -> tuple[int, dict[str, Any] | list[dict[str, Any]]]:
    parsed = urlparse(path)
    route = parsed.path.rstrip("/") or "/"
    query = parse_qs(parsed.query)
    method = method.upper()

    try:
        if route == "/api/stats" and method == "GET":
            return 200, get_dashboard_stats()
        if route == "/api/tasks" and method == "GET":
            return 200, get_recent_tasks(limit=query_int(query, "limit", 50))
        if route == "/api/memories":
            if method == "GET":
                return 200, get_recent_memories(
                    limit=query_int(query, "limit", 50),
                    include_archived=query_bool(query, "include_archived", False),
                )
            if method == "POST":
                return create_memory(parse_json_body(body))

        memory_id, action = parse_memory_route(route)
        if memory_id is not None:
            if method == "GET" and action is None:
                return read_memory(memory_id)
            if method == "PUT" and action is None:
                return update_memory(memory_id, parse_json_body(body))
            if method == "DELETE" and action is None:
                return archive_memory(memory_id, parse_json_body(body, allow_empty=True))
            if method == "POST" and action == "restore":
                return restore_memory(memory_id)
        return 404, {"error": "not_found"}
    except ValueError as error:
        return 400, {"error": "bad_request", "message": str(error)}


def create_memory(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    data = normalized_memory_payload(payload, require_all=True)
    store = MemoryStore()
    episode_id, event_id = create_management_event(store, "semantic_memory_created", data)
    memory_id = store.add_semantic_memory(
        data["subject"],
        data["predicate"],
        data["object"],
        source_event_id=event_id,
        confidence=data["confidence"],
        expires_at=data.get("expires_at"),
        memory_type=data["memory_type"],
        scope=data["scope"],
    )
    store.finish_episode(episode_id, status="verified_completed", summary=f"Created semantic memory {memory_id}")
    memory = store.semantic_memory(memory_id)
    return 201, {"memory": memory}


def read_memory(memory_id: int) -> tuple[int, dict[str, Any]]:
    memory = MemoryStore().semantic_memory(memory_id)
    if memory is None:
        return 404, {"error": "not_found"}
    return 200, {"memory": memory}


def update_memory(memory_id: int, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    store = MemoryStore()
    existing = store.semantic_memory(memory_id)
    if existing is None:
        return 404, {"error": "not_found"}
    merged = {
        "subject": existing["subject"],
        "predicate": existing["predicate"],
        "object": existing["object"],
        "confidence": existing["confidence"],
        "memory_type": existing["memory_type"],
        "scope": existing["scope"],
        "expires_at": existing["expires_at"],
        **payload,
    }
    data = normalized_memory_payload(merged, require_all=True)
    updated = store.update_semantic_memory(
        memory_id,
        subject=data["subject"],
        predicate=data["predicate"],
        object_value=data["object"],
        confidence=data["confidence"],
        memory_type=data["memory_type"],
        scope=data["scope"],
        expires_at=data.get("expires_at"),
    )
    if not updated:
        return 404, {"error": "not_found"}
    episode_id, _event_id = create_management_event(store, "semantic_memory_updated", {"id": memory_id, **data})
    store.finish_episode(episode_id, status="verified_completed", summary=f"Updated semantic memory {memory_id}")
    return 200, {"memory": store.semantic_memory(memory_id)}


def archive_memory(memory_id: int, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    store = MemoryStore()
    if store.semantic_memory(memory_id) is None:
        return 404, {"error": "not_found"}
    reason = str(payload.get("reason") or "web_management_delete").strip() or "web_management_delete"
    store.archive_semantic_memory(memory_id, reason=reason)
    episode_id, _event_id = create_management_event(
        store,
        "semantic_memory_archived",
        {"id": memory_id, "reason": reason},
    )
    store.finish_episode(episode_id, status="verified_completed", summary=f"Archived semantic memory {memory_id}")
    return 200, {"memory": store.semantic_memory(memory_id)}


def restore_memory(memory_id: int) -> tuple[int, dict[str, Any]]:
    store = MemoryStore()
    if store.semantic_memory(memory_id) is None:
        return 404, {"error": "not_found"}
    restored = store.restore_semantic_memory(memory_id)
    if not restored:
        return 404, {"error": "not_found"}
    episode_id, _event_id = create_management_event(store, "semantic_memory_restored", {"id": memory_id})
    store.finish_episode(episode_id, status="verified_completed", summary=f"Restored semantic memory {memory_id}")
    return 200, {"memory": store.semantic_memory(memory_id)}


def create_management_event(store: MemoryStore, event_type: str, payload: dict[str, Any]) -> tuple[int, int]:
    episode_id = store.start_episode()
    event_id = store.add_event(
        episode_id,
        event_type,
        role="operator",
        content=json.dumps(payload, ensure_ascii=False),
        metadata={"source": "web_management", "payload": payload},
    )
    return episode_id, event_id


def parse_memory_route(route: str) -> tuple[int | None, str | None]:
    parts = [part for part in route.split("/") if part]
    if len(parts) not in (3, 4) or parts[:2] != ["api", "memories"]:
        return None, None
    try:
        memory_id = int(parts[2])
    except ValueError as error:
        raise ValueError("memory id must be an integer") from error
    action = parts[3] if len(parts) == 4 else None
    return memory_id, action


def parse_json_body(body: bytes | str | None, allow_empty: bool = False) -> dict[str, Any]:
    if body in (None, b"", ""):
        if allow_empty:
            return {}
        raise ValueError("request body must be JSON")
    text = body.decode("utf-8") if isinstance(body, bytes) else body
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError("request body must be valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    return payload


def normalized_memory_payload(payload: dict[str, Any], require_all: bool = False) -> dict[str, Any]:
    required = ("subject", "predicate", "object")
    if require_all:
        missing = [field for field in required if not str(payload.get(field) or "").strip()]
        if missing:
            raise ValueError(f"missing required field(s): {', '.join(missing)}")
    confidence = float(payload.get("confidence", 0.5))
    if confidence < 0 or confidence > 1:
        raise ValueError("confidence must be between 0 and 1")
    data = {
        "subject": str(payload.get("subject", "")).strip(),
        "predicate": str(payload.get("predicate", "")).strip(),
        "object": str(payload.get("object", "")).strip(),
        "confidence": confidence,
        "memory_type": str(payload.get("memory_type") or "fact").strip() or "fact",
        "scope": str(payload.get("scope") or "global").strip() or "global",
        "expires_at": payload.get("expires_at") or None,
    }
    return data


def query_int(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = query.get(key, [str(default)])[0]
    try:
        return max(1, min(500, int(raw)))
    except (TypeError, ValueError):
        return default


def query_bool(query: dict[str, list[str]], key: str, default: bool) -> bool:
    raw = query.get(key, [str(default)])[0]
    return str(raw).lower() in ("1", "true", "yes", "on")
