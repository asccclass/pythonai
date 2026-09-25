from __future__ import annotations

import argparse
import json
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


def memory_messages(store: MemoryStore, limit: int = 50, episode_id: int | None = None) -> dict[str, Any]:
    return {
        "episode_id": episode_id,
        "limit": limit,
        "messages": store.message_events(limit=limit, episode_id=episode_id),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect bot memory.")
    parser.add_argument("command", choices=["overview", "episode", "messages"])
    parser.add_argument("--episode-id", type=int)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()

    store = MemoryStore()
    if args.command == "overview":
        payload = memory_overview(store)
    elif args.command == "episode":
        if args.episode_id is None:
            parser.error("--episode-id is required for episode")
        payload = inspect_episode(store, args.episode_id)
    else:
        payload = memory_messages(store, limit=args.limit, episode_id=args.episode_id)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
