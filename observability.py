from __future__ import annotations

from typing import Any

from memory import MemoryStore


def memory_overview(store: MemoryStore) -> dict[str, Any]:
    return {
        "recent_events": store.recent_events(limit=20),
        "active_semantic_memories": store.active_semantic_memories(),
        "archived_semantic_memories": store.archived_semantic_memories(),
        "active_procedures": store.active_procedures(),
        "archived_procedures": store.archived_procedures(),
        "review_candidates": store.review_candidates(),
    }


def inspect_episode(store: MemoryStore, episode_id: int) -> dict[str, Any]:
    return {
        "episode_id": episode_id,
        "events": store.episode_events(episode_id),
    }
