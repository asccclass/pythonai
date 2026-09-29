from __future__ import annotations

import argparse
import json
from pathlib import Path
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


def review_candidates(store: MemoryStore, limit: int = 50) -> dict[str, Any]:
    return {"limit": limit, "events": store.review_candidates(limit=limit)}


def procedures(store: MemoryStore, task_type: str | None = None) -> dict[str, Any]:
    return {
        "task_type": task_type,
        "active_procedures": store.active_procedures(task_type=task_type),
        "archived_procedures": store.archived_procedures() if task_type is None else [],
    }


def active_facts(
    store: MemoryStore,
    memory_type: str | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    return {
        "memory_type": memory_type,
        "scope": scope,
        "memories": store.active_semantic_memories(memory_type=memory_type, scope=scope),
    }


def low_confidence_facts(store: MemoryStore, threshold: float = 0.35) -> dict[str, Any]:
    memories = [
        memory
        for memory in store.active_semantic_memories()
        if float(memory["confidence"]) <= threshold
    ]
    return {"threshold": threshold, "memories": memories}


def semantic_conflicts(store: MemoryStore) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for memory in store.active_semantic_memories():
        grouped.setdefault((memory["subject"], memory["predicate"]), []).append(memory)
    conflicts = [
        {"subject": subject, "predicate": predicate, "memories": memories}
        for (subject, predicate), memories in grouped.items()
        if len({memory["object"] for memory in memories}) > 1
    ]
    return {"conflicts": conflicts}


def archive_fact(store: MemoryStore, memory_id: int, reason: str = "manual_archive") -> dict[str, Any]:
    store.archive_semantic_memory(memory_id, reason=reason)
    return {"archived_memory_id": memory_id, "reason": reason}


def confirm_fact(store: MemoryStore, memory_id: int) -> dict[str, Any]:
    episode_id = store.start_episode()
    event_id = store.add_event(
        episode_id,
        "semantic_memory_confirmation",
        metadata={"semantic_memory_id": memory_id},
    )
    store.finish_episode(episode_id, summary=f"Confirmed semantic memory {memory_id}")
    return {"event_id": event_id, "episode_id": episode_id, "semantic_memory_id": memory_id}


def contradict_fact(store: MemoryStore, memory_id: int) -> dict[str, Any]:
    episode_id = store.start_episode()
    event_id = store.add_event(
        episode_id,
        "semantic_memory_contradiction",
        metadata={"semantic_memory_id": memory_id},
    )
    store.finish_episode(episode_id, summary=f"Contradicted semantic memory {memory_id}")
    return {"event_id": event_id, "episode_id": episode_id, "semantic_memory_id": memory_id}


def supersede_fact(
    store: MemoryStore,
    old_memory_id: int,
    subject: str,
    predicate: str,
    object_value: str,
    confidence: float = 0.8,
    memory_type: str = "fact",
    scope: str = "global",
    reason: str = "manual_supersede",
) -> dict[str, Any]:
    episode_id = store.start_episode()
    source_event_id = store.add_event(
        episode_id,
        "manual_memory_supersession",
        metadata={
            "old_memory_id": old_memory_id,
            "subject": subject,
            "predicate": predicate,
            "object": object_value,
            "reason": reason,
        },
    )
    new_memory_id = store.supersede_semantic_memory(
        old_memory_id,
        subject,
        predicate,
        object_value,
        source_event_id=source_event_id,
        confidence=confidence,
        reason=reason,
        memory_type=memory_type,
        scope=scope,
    )
    store.finish_episode(episode_id, summary=f"Superseded semantic memory {old_memory_id}")
    return {
        "episode_id": episode_id,
        "old_memory_id": old_memory_id,
        "new_memory_id": new_memory_id,
        "reason": reason,
    }


def export_memory(store: MemoryStore) -> dict[str, Any]:
    return {
        "active_semantic_memories": store.active_semantic_memories(),
        "archived_semantic_memories": store.archived_semantic_memories(),
        "active_procedures": store.active_procedures(),
        "archived_procedures": store.archived_procedures(),
    }


def import_memory(store: MemoryStore, payload: dict[str, Any]) -> dict[str, Any]:
    episode_id = store.start_episode()
    source_event_id = store.add_event(
        episode_id,
        "memory_import",
        metadata={
            "semantic_count": len(payload.get("active_semantic_memories", [])),
            "procedure_count": len(payload.get("active_procedures", [])),
        },
    )
    imported_semantic_ids = []
    for memory in payload.get("active_semantic_memories", []):
        imported_semantic_ids.append(
            store.add_semantic_memory(
                subject=str(memory["subject"]),
                predicate=str(memory["predicate"]),
                object_value=str(memory["object"]),
                source_event_id=source_event_id,
                confidence=float(memory.get("confidence", 0.5)),
                expires_at=memory.get("expires_at"),
                embedding=memory.get("embedding"),
                memory_type=str(memory.get("memory_type", "fact")),
                scope=str(memory.get("scope", "global")),
            )
        )
    imported_procedure_ids = []
    for procedure in payload.get("active_procedures", []):
        imported_procedure_ids.append(
            store.add_procedure(
                task_type=str(procedure["task_type"]),
                context_pattern=str(procedure["context_pattern"]),
                steps=[str(step) for step in procedure.get("steps", [])],
                source_episode_id=episode_id,
                confidence=float(procedure.get("confidence", 0.5)),
                success_count=int(procedure.get("success_count", 1)),
                failure_count=int(procedure.get("failure_count", 0)),
            )
        )
    store.finish_episode(episode_id, summary="Imported memory export")
    return {
        "episode_id": episode_id,
        "source_event_id": source_event_id,
        "imported_semantic_memory_ids": imported_semantic_ids,
        "imported_procedure_ids": imported_procedure_ids,
    }


def import_memory_file(store: MemoryStore, path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return import_memory(store, payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect bot memory.")
    parser.add_argument(
        "command",
        choices=[
            "overview",
            "episode",
            "messages",
            "skills",
            "skill",
            "skill-runs",
            "review-candidates",
            "procedures",
            "facts",
            "low-confidence",
            "conflicts",
            "archive-fact",
            "confirm-fact",
            "contradict-fact",
            "supersede-fact",
            "export",
            "import",
        ],
    )
    parser.add_argument("--episode-id", type=int)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--name")
    parser.add_argument("--memory-id", type=int)
    parser.add_argument("--reason", default="manual_archive")
    parser.add_argument("--memory-type")
    parser.add_argument("--scope")
    parser.add_argument("--threshold", type=float, default=0.35)
    parser.add_argument("--subject")
    parser.add_argument("--predicate")
    parser.add_argument("--object")
    parser.add_argument("--confidence", type=float, default=0.8)
    parser.add_argument("--path")
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
    elif args.command == "skill-runs":
        payload = skill_runs(store, limit=args.limit)
    elif args.command == "review-candidates":
        payload = review_candidates(store, limit=args.limit)
    elif args.command == "procedures":
        payload = procedures(store, task_type=args.name)
    elif args.command == "facts":
        payload = active_facts(store, memory_type=args.memory_type, scope=args.scope)
    elif args.command == "low-confidence":
        payload = low_confidence_facts(store, threshold=args.threshold)
    elif args.command == "conflicts":
        payload = semantic_conflicts(store)
    elif args.command == "archive-fact":
        if args.memory_id is None:
            parser.error("--memory-id is required for archive-fact")
        payload = archive_fact(store, args.memory_id, reason=args.reason)
    elif args.command == "confirm-fact":
        if args.memory_id is None:
            parser.error("--memory-id is required for confirm-fact")
        payload = confirm_fact(store, args.memory_id)
    elif args.command == "contradict-fact":
        if args.memory_id is None:
            parser.error("--memory-id is required for contradict-fact")
        payload = contradict_fact(store, args.memory_id)
    elif args.command == "supersede-fact":
        if args.memory_id is None:
            parser.error("--memory-id is required for supersede-fact")
        if args.subject is None or args.predicate is None or args.object is None:
            parser.error("--subject, --predicate, and --object are required for supersede-fact")
        payload = supersede_fact(
            store,
            args.memory_id,
            args.subject,
            args.predicate,
            args.object,
            confidence=args.confidence,
            memory_type=args.memory_type or "fact",
            scope=args.scope or "global",
            reason=args.reason,
        )
    elif args.command == "export":
        payload = export_memory(store)
    else:
        if args.path is None:
            parser.error("--path is required for import")
        payload = import_memory_file(store, args.path)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
