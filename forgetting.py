from __future__ import annotations

from memory import MemoryStore


def run_forgetting_policy(
    store: MemoryStore,
    min_semantic_confidence: float = 0.2,
    max_procedure_failures: int = 3,
    confidence_decay: float = 0.02,
) -> dict[str, int]:
    decayed = 0
    resolve_conflicts(store)
    archived_semantic = 0
    for memory in store.active_semantic_memories():
        new_confidence = max(0.0, float(memory["confidence"]) - confidence_decay)
        if new_confidence != float(memory["confidence"]):
            store.update_semantic_confidence(memory["id"], new_confidence)
            decayed += 1
        if new_confidence < min_semantic_confidence:
            store.archive_semantic_memory(memory["id"], reason="low_confidence")
            archived_semantic += 1

    archived_procedures = 0
    for procedure in store.active_procedures():
        if int(procedure["failure_count"]) >= max_procedure_failures and int(procedure["success_count"]) == 0:
            store.archive_procedure(procedure["id"], reason="too_many_failures")
            archived_procedures += 1

    return {
        "decayed_semantic_memories": decayed,
        "archived_semantic_memories": archived_semantic,
        "archived_procedures": archived_procedures,
    }


def resolve_conflicts(store: MemoryStore) -> int:
    grouped = {}
    for memory in store.active_semantic_memories():
        grouped.setdefault((memory["subject"], memory["predicate"]), []).append(memory)

    archived = 0
    for memories in grouped.values():
        if len(memories) < 2:
            continue
        winner = sorted(memories, key=lambda item: (float(item["confidence"]), item["updated_at"], item["id"]), reverse=True)[0]
        for memory in memories:
            if memory["id"] != winner["id"]:
                store.archive_semantic_memory(memory["id"], reason=f"conflicts_with:{winner['id']}")
                archived += 1
    return archived
