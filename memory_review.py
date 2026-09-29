from __future__ import annotations

import json
from typing import Any

from memory import MemoryStore
from procedure_similarity import ProcedureCandidate, ProcedureSimilarityMatcher
from semantic_extractor import SemanticExtractor, extract_semantic_triple, fallback_semantic_triples


def process_memory_review_candidates(
    store: MemoryStore,
    episode_id: int,
    semantic_extractor: SemanticExtractor | None = None,
    procedure_matcher: ProcedureSimilarityMatcher | None = None,
) -> list[dict[str, Any]]:
    events = store.episode_events(episode_id)
    candidates = review_candidate_events(events)
    processed = []
    for candidate in candidates:
        for kind in candidate_memory_kinds(candidate):
            if kind == "semantic":
                processed.append(process_semantic_candidate(store, episode_id, events, candidate, semantic_extractor))
            elif kind == "procedure":
                processed.append(process_procedure_candidate(store, episode_id, events, candidate, procedure_matcher))
            elif kind == "forgetting":
                processed.append(process_forgetting_candidate(store, episode_id, candidate))
    return [item for item in processed if item]


def review_candidate_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        event
        for event in events
        if event["event_type"] in {"memory_review_candidate", "working_memory_preservation_candidate"}
    ]


def candidate_memory_kinds(candidate: dict[str, Any]) -> list[str]:
    if candidate["event_type"] == "memory_review_candidate":
        kind = candidate["metadata"].get("memory_kind", "none")
    else:
        preservation = candidate["metadata"].get("preservation") or {}
        if not preservation.get("should_preserve", False):
            return []
        kind = preservation.get("preservation_kind", "none")
    if kind == "both":
        return ["semantic", "procedure"]
    if kind in {"semantic", "procedure", "forgetting"}:
        return [kind]
    return []


def process_semantic_candidate(
    store: MemoryStore,
    episode_id: int,
    events: list[dict[str, Any]],
    candidate: dict[str, Any],
    semantic_extractor: SemanticExtractor | None = None,
) -> dict[str, Any] | None:
    source = semantic_source_event(events, candidate)
    if source is None:
        return None
    candidate_confidence = float(candidate["metadata"].get("confidence", 0.5))
    extractor = semantic_extractor or _FallbackSemanticExtractor()
    triples = extractor.extract(source["content"] or "")
    memory_ids = []
    deduplicated_memory_ids = []
    existing_episode_memories = semantic_memories_for_episode(store, events)
    for triple in triples:
        existing_memory = existing_episode_memories.get(semantic_memory_key(triple.subject, triple.predicate, triple.object_value))
        if existing_memory is not None:
            deduplicated_memory_ids.append(existing_memory["id"])
            continue
        memory_id = store.add_semantic_memory(
            subject=triple.subject,
            predicate=triple.predicate,
            object_value=triple.object_value,
            source_event_id=source["id"],
            confidence=min(candidate_confidence, triple.confidence),
            memory_type=triple.memory_type,
            scope=triple.scope,
        )
        memory_ids.append(memory_id)
        existing_episode_memories[semantic_memory_key(triple.subject, triple.predicate, triple.object_value)] = {
            "id": memory_id
        }
    if not memory_ids and not deduplicated_memory_ids:
        return None
    all_memory_ids = [*memory_ids, *deduplicated_memory_ids]
    metadata = {"memory_kind": "semantic", "semantic_memory_ids": all_memory_ids}
    if memory_ids:
        metadata["created_semantic_memory_ids"] = memory_ids
    if deduplicated_memory_ids:
        metadata["deduplicated_semantic_memory_ids"] = deduplicated_memory_ids
    if len(all_memory_ids) == 1:
        metadata["semantic_memory_id"] = all_memory_ids[0]
    store.add_event(
        episode_id,
        "memory_review_result",
        metadata=metadata,
    )
    return metadata


def semantic_source_event(events: list[dict[str, Any]], candidate: dict[str, Any]) -> dict[str, Any] | None:
    if candidate["event_type"] == "working_memory_preservation_candidate":
        summary_event = latest_event_before(events, candidate["id"], "working_memory_summary")
        if summary_event is not None:
            return summary_event
        if candidate.get("content"):
            return candidate
    return next((event for event in events if event["event_type"] == "message" and event["role"] == "user"), None)


def semantic_memories_for_episode(store: MemoryStore, events: list[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, Any]]:
    event_ids = {event["id"] for event in events}
    memories = {}
    for memory in store.active_semantic_memories():
        if memory["source_event_id"] not in event_ids:
            continue
        memories[semantic_memory_key(memory["subject"], memory["predicate"], memory["object"])] = memory
    return memories


def semantic_memory_key(subject: str, predicate: str, object_value: str) -> tuple[str, str, str]:
    return (normalize_memory_part(subject), normalize_memory_part(predicate), normalize_memory_part(object_value))


def normalize_memory_part(value: str) -> str:
    return " ".join(str(value).strip().casefold().split())


def latest_event_before(events: list[dict[str, Any]], event_id: int, event_type: str) -> dict[str, Any] | None:
    matches = [event for event in events if event["event_type"] == event_type and event["id"] < event_id]
    if not matches:
        return None
    return sorted(matches, key=lambda event: event["id"], reverse=True)[0]


def process_procedure_candidate(
    store: MemoryStore,
    episode_id: int,
    events: list[dict[str, Any]],
    candidate: dict[str, Any],
    procedure_matcher: ProcedureSimilarityMatcher | None = None,
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
    procedure_candidate = ProcedureCandidate(task_type, context_pattern, steps)
    existing_procedures = store.active_procedures(task_type)
    match = (procedure_matcher or _FallbackProcedureMatcher()).find_match(procedure_candidate, existing_procedures)
    if match:
        store.record_procedure_result(match.procedure_id, succeeded=True)
        procedure_id = match.procedure_id
        action = "merged"
    else:
        procedure_id = store.add_procedure(
            task_type=task_type,
            context_pattern=context_pattern,
            steps=steps,
            source_episode_id=episode_id,
            confidence=float(candidate["metadata"].get("confidence", 0.5)),
        )
        action = "created"
    store.add_event(
        episode_id,
        "memory_review_result",
        metadata={"memory_kind": "procedure", "procedure_id": procedure_id, "action": action},
    )
    return {"memory_kind": "procedure", "procedure_id": procedure_id, "action": action}


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


class _FallbackSemanticExtractor:
    def extract(self, text: str):
        return fallback_semantic_triples(text)


class _FallbackProcedureMatcher:
    def find_match(self, candidate: ProcedureCandidate, procedures: list[dict[str, Any]], threshold: float = 0.72):
        for procedure in procedures:
            if procedure["context_pattern"] == candidate.context_pattern:
                from procedure_similarity import ProcedureMatch

                return ProcedureMatch(procedure["id"], 1.0, "exact_context_pattern")
        return None
