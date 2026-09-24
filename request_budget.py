from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RemoteRequestBudget:
    limits: dict[str, int]
    used: dict[str, int] = field(default_factory=dict)

    def try_acquire(self, operation: str) -> bool:
        limit = self.limits.get(operation)
        if limit is None:
            return True
        current = self.used.get(operation, 0)
        if current >= limit:
            return False
        self.used[operation] = current + 1
        return True

    def remaining(self, operation: str) -> int | None:
        limit = self.limits.get(operation)
        if limit is None:
            return None
        return max(0, limit - self.used.get(operation, 0))


def foreground_memory_budget() -> RemoteRequestBudget:
    return RemoteRequestBudget(
        {
            "memory_query_embedding": 1,
            "memory_embedding_backfill": 0,
            "semantic_extraction": 0,
            "procedure_similarity": 0,
        }
    )


def background_memory_budget() -> RemoteRequestBudget:
    return RemoteRequestBudget(
        {
            "memory_query_embedding": 0,
            "memory_embedding_backfill": 0,
            "semantic_extraction": 1,
            "procedure_similarity": 1,
        }
    )
