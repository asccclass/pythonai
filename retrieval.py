from __future__ import annotations

from typing import Any

from memory import MemoryStore


def build_memory_context(store: MemoryStore, limit: int = 12) -> str:
    memories = store.active_semantic_memories()[:limit]
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


def inject_memory_context(messages: list[dict[str, Any]], context: str) -> list[dict[str, Any]]:
    if not context:
        return messages
    return [*messages, {"role": "system", "content": context}]
