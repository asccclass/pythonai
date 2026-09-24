from __future__ import annotations

from memory import MemoryStore


def run_forgetting_policy(
    store: MemoryStore,
    min_semantic_confidence: float = 0.2,
    max_procedure_failures: int = 3,
) -> dict[str, int]:
    archived_semantic = 0
    for memory in store.active_semantic_memories():
        if float(memory["confidence"]) < min_semantic_confidence:
            store.archive_semantic_memory(memory["id"], reason="low_confidence")
            archived_semantic += 1

    archived_procedures = 0
    for procedure in store.active_procedures():
        if int(procedure["failure_count"]) >= max_procedure_failures and int(procedure["success_count"]) == 0:
            store.archive_procedure(procedure["id"], reason="too_many_failures")
            archived_procedures += 1

    return {
        "archived_semantic_memories": archived_semantic,
        "archived_procedures": archived_procedures,
    }
