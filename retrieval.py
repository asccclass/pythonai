from __future__ import annotations

import re
from typing import Any

from memory import MemoryStore


def build_memory_context(store: MemoryStore, query: str = "", limit: int = 12, ranker: Any | None = None) -> str:
    memories = rank_memories(store.active_semantic_memories(), query)
    if ranker is not None:
        memories = ranker.rank(query, memories)
    memories = memories[:limit]
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
