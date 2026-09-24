from __future__ import annotations

import json
from typing import Any

from memory import MemoryStore


def process_memory_review_candidates(store: MemoryStore, episode_id: int) -> list[dict[str, Any]]:
    events = store.episode_events(episode_id)
    candidates = [event for event in events if event["event_type"] == "memory_review_candidate"]
    processed = []
    for candidate in candidates:
        kind = candidate["metadata"].get("memory_kind", "none")
        if kind == "semantic":
            processed.append(process_semantic_candidate(store, episode_id, events, candidate))
        elif kind == "procedure":
            processed.append(process_procedure_candidate(store, episode_id, events, candidate))
        elif kind == "forgetting":
            processed.append(process_forgetting_candidate(store, episode_id, candidate))
    return [item for item in processed if item]


def process_semantic_candidate(
    store: MemoryStore,
    episode_id: int,
    events: list[dict[str, Any]],
    candidate: dict[str, Any],
) -> dict[str, Any] | None:
    source = next((event for event in events if event["event_type"] == "message" and event["role"] == "user"), None)
    if source is None:
        return None
    memory_id = store.add_semantic_memory(
        subject="episode",
        predicate="user_statement",
        object_value=source["content"] or "",
        source_event_id=source["id"],
        confidence=float(candidate["metadata"].get("confidence", 0.5)),
    )
    store.add_event(
        episode_id,
        "memory_review_result",
        metadata={"memory_kind": "semantic", "semantic_memory_id": memory_id},
    )
    return {"memory_kind": "semantic", "semantic_memory_id": memory_id}


def process_procedure_candidate(
    store: MemoryStore,
    episode_id: int,
    events: list[dict[str, Any]],
    candidate: dict[str, Any],
) -> dict[str, Any] | None:
    tool_calls = [event for event in events if event["event_type"] == "tool_call"]
    if not tool_calls:
        return None
    steps = []
    for event in tool_calls:
        metadata = event["metadata"]
        name = metadata.get("name", "tool")
        arguments = metadata.get("arguments", "{}")
        steps.append(f"{name} {arguments}")
    procedure_id = store.add_procedure(
        task_type=tool_calls[0]["metadata"].get("name", "tool_workflow"),
        context_pattern="; ".join(step.split(" ", 1)[0] for step in steps),
        steps=steps,
        source_episode_id=episode_id,
        confidence=float(candidate["metadata"].get("confidence", 0.5)),
    )
    store.add_event(
        episode_id,
        "memory_review_result",
        metadata={"memory_kind": "procedure", "procedure_id": procedure_id},
    )
    return {"memory_kind": "procedure", "procedure_id": procedure_id}


def process_forgetting_candidate(store: MemoryStore, episode_id: int, candidate: dict[str, Any]) -> dict[str, Any]:
    store.add_event(
        episode_id,
        "memory_review_result",
        metadata={
            "memory_kind": "forgetting",
            "status": "queued_for_policy",
            "confidence": candidate["metadata"].get("confidence", 0.0),
        },
    )
    return {"memory_kind": "forgetting", "status": "queued_for_policy"}


def tool_arguments(arguments: str) -> dict[str, Any]:
    try:
        return json.loads(arguments)
    except json.JSONDecodeError:
        return {}
