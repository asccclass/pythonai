from __future__ import annotations

import re
from typing import Any

from memory import MemoryStore
from vector_search import VectorMemorySearcher

DEFAULT_VECTOR_CANDIDATE_LIMIT = 24
DEFAULT_PROCEDURE_LIMIT = 4


def build_combined_memory_context(
    store: MemoryStore,
    query: str = "",
    semantic_context: str | None = None,
    procedure_limit: int = DEFAULT_PROCEDURE_LIMIT,
) -> str:
    sections = []
    if semantic_context:
        sections.append(semantic_context)
    procedure_context = build_procedure_context(store, query=query, limit=procedure_limit)
    if procedure_context:
        sections.append(procedure_context)
    return "\n\n".join(sections)


def build_memory_context(
    store: MemoryStore,
    query: str = "",
    limit: int = 12,
    vector_searcher: VectorMemorySearcher | None = None,
    allow_query_embedding: bool = True,
    max_missing_embeddings: int = 0,
    vector_candidate_limit: int = DEFAULT_VECTOR_CANDIDATE_LIMIT,
) -> str:
    searcher = vector_searcher or VectorMemorySearcher()
    active_memories = store.active_semantic_memories()
    entity_memories = linked_entity_memories(store, active_memories, query)
    candidates = candidate_memories(active_memories, query, vector_candidate_limit)
    candidates = merge_candidate_memories(entity_memories, candidates, vector_candidate_limit)
    if hasattr(searcher, "search_with_budget"):
        memories = searcher.search_with_budget(
            query,
            candidates,
            limit,
            allow_query_embedding=allow_query_embedding,
            max_missing_embeddings=max_missing_embeddings,
        )
    else:
        memories = searcher.search(query, candidates, limit)
    if not memories and entity_memories:
        memories = entity_memories[:limit]
    if not memories:
        return ""

    lines = ["Relevant long-term memory:"]
    for section, section_memories in group_memories_by_type(memories):
        lines.append(f"{section}:")
        for memory in section_memories:
            scope = memory.get("scope", "global")
            lines.append(
                "- "
                f"[{memory['id']}] {memory['subject']} {memory['predicate']} {memory['object']} "
                f"(type={memory.get('memory_type', 'fact')}, scope={scope}, "
                f"confidence={memory['confidence']:.2f}, source_event_id={memory['source_event_id']})"
            )
    return "\n".join(lines)


def candidate_memories(memories: list[dict[str, Any]], query: str = "", limit: int = DEFAULT_VECTOR_CANDIDATE_LIMIT) -> list[dict[str, Any]]:
    if not query:
        return rank_memories(memories)[:limit]

    lexical = rank_memories(memories, query)
    selected: dict[int, dict[str, Any]] = {int(memory["id"]): memory for memory in lexical[:limit]}

    for memory in rank_memories(memories):
        if len(selected) >= limit:
            break
        selected.setdefault(int(memory["id"]), memory)
    return list(selected.values())


def build_procedure_context(store: MemoryStore, query: str = "", limit: int = DEFAULT_PROCEDURE_LIMIT) -> str:
    procedures = rank_procedures(store.active_procedures(), query)[:limit]
    if not procedures:
        return ""
    lines = ["Relevant reusable workflows:"]
    for procedure in procedures:
        steps = "; ".join(procedure["steps"])
        lines.append(
            "- "
            f"{procedure['task_type']} [{procedure['context_pattern']}] "
            f"steps={steps} "
            f"(confidence={procedure['confidence']:.2f}, successes={procedure['success_count']}, "
            f"failures={procedure['failure_count']}, source_episode_id={procedure['source_episode_id']})"
        )
    return "\n".join(lines)


def rank_procedures(procedures: list[dict[str, Any]], query: str = "") -> list[dict[str, Any]]:
    if not query:
        return sorted(procedures, key=procedure_base_score, reverse=True)
    query_terms = tokenize(query)
    scored = []
    for procedure in procedures:
        procedure_terms = tokenize(
            " ".join([procedure["task_type"], procedure["context_pattern"], " ".join(procedure["steps"])])
        )
        overlap = len(query_terms & procedure_terms)
        if overlap <= 0:
            continue
        scored.append((overlap, *procedure_base_score(procedure), procedure))
    scored.sort(reverse=True)
    return [procedure for *_, procedure in scored]


def procedure_base_score(procedure: dict[str, Any]) -> tuple[float, int, str, int]:
    return (
        procedure_success_rate(procedure),
        int(procedure["success_count"]),
        str(procedure.get("last_success_at") or procedure.get("updated_at") or ""),
        int(procedure["id"]),
    )


def procedure_success_rate(procedure: dict[str, Any]) -> float:
    successes = int(procedure["success_count"])
    failures = int(procedure["failure_count"])
    attempts = successes + failures
    if attempts == 0:
        return 0.0
    return successes / attempts


def linked_entity_memories(store: MemoryStore, memories: list[dict[str, Any]], query: str = "") -> list[dict[str, Any]]:
    if not query or not hasattr(store, "find_entity"):
        return []
    entity = store.find_entity(query)
    if entity is None:
        return []
    entity_id = entity["id"]
    return [
        memory
        for memory in rank_memories(memories)
        if memory.get("subject_entity_id") == entity_id or memory.get("object_entity_id") == entity_id
    ]


def merge_candidate_memories(
    preferred: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    selected: dict[int, dict[str, Any]] = {}
    for memory in [*preferred, *candidates]:
        if len(selected) >= limit:
            break
        selected.setdefault(int(memory["id"]), memory)
    return list(selected.values())


def rank_memories(memories: list[dict[str, Any]], query: str = "") -> list[dict[str, Any]]:
    if not query:
        return sorted(
            memories,
            key=lambda memory: (
                memory_type_priority(memory),
                memory["confidence"],
                memory["updated_at"],
                memory["id"],
            ),
            reverse=True,
        )

    query_terms = tokenize(query)
    scored = []
    for memory in memories:
        memory_terms = tokenize(f"{memory['subject']} {memory['predicate']} {memory['object']}")
        overlap = len(query_terms & memory_terms)
        if overlap <= 0:
            continue
        scored.append((overlap, memory_type_priority(memory), memory["confidence"], memory["updated_at"], memory["id"], memory))
    scored.sort(reverse=True)
    return [memory for _, _, _, _, _, memory in scored]


def group_memories_by_type(memories: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for memory in memories:
        grouped.setdefault(memory_section(memory), []).append(memory)
    return [(section, grouped[section]) for section in MEMORY_SECTION_ORDER if section in grouped]


MEMORY_SECTION_ORDER = [
    "User profile",
    "Project facts",
    "Agent persona",
    "Entity facts",
    "Task facts",
    "Facts",
]


def memory_section(memory: dict[str, Any]) -> str:
    memory_type = memory.get("memory_type", "fact")
    return {
        "user_profile": "User profile",
        "project_fact": "Project facts",
        "agent_persona": "Agent persona",
        "entity_fact": "Entity facts",
        "task_fact": "Task facts",
    }.get(memory_type, "Facts")


def memory_type_priority(memory: dict[str, Any]) -> int:
    return {
        "user_profile": 50,
        "project_fact": 40,
        "agent_persona": 35,
        "entity_fact": 30,
        "task_fact": 20,
    }.get(memory.get("memory_type", "fact"), 10)


def tokenize(text: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[\w]+", text) if len(token) >= 2}


def inject_memory_context(messages: list[dict[str, Any]], context: str) -> list[dict[str, Any]]:
    if not context:
        return messages
    return [*messages, {"role": "system", "content": context}]
