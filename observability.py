from __future__ import annotations

import argparse
import json
from typing import Any

from memory import MemoryStore
from skills import SkillRegistry


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


def list_skills(registry: SkillRegistry | None = None) -> dict[str, Any]:
    registry = registry or SkillRegistry()
    return {"skills": [skill.to_dict() for skill in registry.list()]}


def inspect_skill(name: str, registry: SkillRegistry | None = None) -> dict[str, Any]:
    registry = registry or SkillRegistry()
    return {"skill": registry.get(name).to_dict()}


def skill_runs(store: MemoryStore, limit: int = 50) -> dict[str, Any]:
    return {"limit": limit, "events": store.skill_run_events(limit=limit)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect bot memory.")
    parser.add_argument("command", choices=["overview", "episode", "messages", "skills", "skill", "skill-runs"])
    parser.add_argument("--episode-id", type=int)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--name")
    args = parser.parse_args()

    store = MemoryStore()
    if args.command == "overview":
        payload = memory_overview(store)
    elif args.command == "episode":
        if args.episode_id is None:
            parser.error("--episode-id is required for episode")
        payload = inspect_episode(store, args.episode_id)
    elif args.command == "messages":
        payload = memory_messages(store, limit=args.limit, episode_id=args.episode_id)
    elif args.command == "skills":
        payload = list_skills()
    elif args.command == "skill":
        if args.name is None:
            parser.error("--name is required for skill")
        payload = inspect_skill(args.name)
    else:
        payload = skill_runs(store, limit=args.limit)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
