from __future__ import annotations

import re
from typing import Any

from memory import MemoryStore
from vector_search import VectorMemorySearcher

DEFAULT_VECTOR_CANDIDATE_LIMIT = 24


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
    candidates = candidate_memories(store.active_semantic_memories(), query, vector_candidate_limit)
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
    if not memories:
        return ""

    lines = ["Relevant long-term memory:"]
    for memory in memories:
        lines.append(
            "- "
            f"{memory['subject']} {memory['predicate']} {memory['object']} "
            f"(confidence={memory['confidence']:.2f}, source_event_id={memory['source_event_id']})"
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


def rank_memories(memories: list[dict[str, Any]], query: str = "") -> list[dict[str, Any]]:
    if not query:
        return sorted(memories, key=lambda memory: (memory["confidence"], memory["updated_at"], memory["id"]), reverse=True)

    query_terms = tokenize(query)
    scored = []
    for memory in memories:
        memory_terms = tokenize(f"{memory['subject']} {memory['predicate']} {memory['object']}")
        overlap = len(query_terms & memory_terms)
        if overlap <= 0:
            continue
        scored.append((overlap, memory["confidence"], memory["updated_at"], memory["id"], memory))
    scored.sort(reverse=True)
    return [memory for _, _, _, _, memory in scored]


def tokenize(text: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[\w]+", text) if len(token) >= 2}


def inject_memory_context(messages: list[dict[str, Any]], context: str) -> list[dict[str, Any]]:
    if not context:
        return messages
    return [*messages, {"role": "system", "content": context}]
