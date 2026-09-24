from __future__ import annotations

import json
import re
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
    subject, predicate, object_value = extract_semantic_triple(source["content"] or "")
    memory_id = store.add_semantic_memory(
        subject=subject,
        predicate=predicate,
        object_value=object_value,
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
    task_type = tool_calls[0]["metadata"].get("name", "tool_workflow")
    context_pattern = "; ".join(step.split(" ", 1)[0] for step in steps)
    existing = store.find_procedure(task_type, context_pattern)
    if existing:
        store.record_procedure_result(existing["id"], succeeded=True)
        procedure_id = existing["id"]
    else:
        procedure_id = store.add_procedure(
            task_type=task_type,
            context_pattern=context_pattern,
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


def extract_semantic_triple(text: str) -> tuple[str, str, str]:
    normalized = text.strip()
    patterns = [
        (r"(?i)\bI prefer ([\w .+-]+)", "user", "prefers", 1),
        (r"(?i)\bI like ([\w .+-]+)", "user", "likes", 1),
        (r"(?i)\bI live in ([\w .+-]+)", "user", "lives_in", 1),
        (r"(?i)\bmy preferred ([\w_ -]+) is ([\w .+-]+)", "user", "preferred_{0}", 2),
    ]
    for pattern, subject, predicate, group in patterns:
        match = re.search(pattern, normalized)
        if not match:
            continue
        if "{0}" in predicate:
            return subject, predicate.format(match.group(1).strip().lower().replace(" ", "_")), match.group(group).strip()
        return subject, predicate, match.group(group).strip()
    return "episode", "user_statement", normalized
