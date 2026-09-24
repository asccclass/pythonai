from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from memory import MemoryStore


@dataclass(frozen=True)
class ForgettingPolicyConfig:
    min_semantic_confidence: float = 0.2
    semantic_decay: float = 0.02
    semantic_access_boost: float = 0.03
    semantic_confirmation_boost: float = 0.08
    semantic_conflict_margin: float = 0.05
    max_procedure_failures: int = 3
    procedure_failure_ratio: float = 0.75
    min_procedure_attempts_for_ratio: int = 4
    procedure_low_confidence: float = 0.2


@dataclass(frozen=True)
class LifecycleSignals:
    referenced_semantic_ids: set[int]
    confirmed_semantic_ids: set[int]
    contradicted_semantic_ids: set[int]
    failed_procedure_ids: set[int]
    successful_procedure_ids: set[int]


def run_forgetting_policy(
    store: MemoryStore,
    min_semantic_confidence: float = 0.2,
    max_procedure_failures: int = 3,
    confidence_decay: float = 0.02,
    config: ForgettingPolicyConfig | None = None,
) -> dict[str, int]:
    policy = config or ForgettingPolicyConfig(
        min_semantic_confidence=min_semantic_confidence,
        max_procedure_failures=max_procedure_failures,
        semantic_decay=confidence_decay,
    )
    signals = collect_lifecycle_signals(store)
    result = {
        "decayed_semantic_memories": 0,
        "reinforced_semantic_memories": 0,
        "archived_semantic_memories": 0,
        "archived_expired_semantic_memories": 0,
        "archived_conflicting_semantic_memories": 0,
        "archived_contradicted_semantic_memories": 0,
        "archived_procedures": 0,
    }

    result["archived_expired_semantic_memories"] = archive_expired_semantic_memories(store)
    result["archived_conflicting_semantic_memories"] = resolve_conflicts(store, policy)
    result["archived_contradicted_semantic_memories"] = archive_contradicted_semantic_memories(store, signals)
    semantic_result = update_semantic_lifecycle(store, policy, signals)
    result.update({key: result.get(key, 0) + value for key, value in semantic_result.items()})
    result["archived_procedures"] = update_procedure_lifecycle(store, policy)
    return result


def collect_lifecycle_signals(store: MemoryStore, limit: int = 200) -> LifecycleSignals:
    referenced_semantic_ids: set[int] = set()
    confirmed_semantic_ids: set[int] = set()
    contradicted_semantic_ids: set[int] = set()
    failed_procedure_ids: set[int] = set()
    successful_procedure_ids: set[int] = set()

    for event in store.recent_events(limit):
        metadata = event.get("metadata") or {}
        event_type = event.get("event_type")
        referenced_semantic_ids.update(_int_ids(metadata.get("semantic_memory_ids")))
        referenced_semantic_ids.update(_int_ids(metadata.get("semantic_memory_id")))

        if event_type in {"memory_confirmation", "semantic_memory_confirmation"}:
            confirmed_semantic_ids.update(_int_ids(metadata.get("semantic_memory_ids")))
            confirmed_semantic_ids.update(_int_ids(metadata.get("semantic_memory_id")))
        if event_type in {"memory_contradiction", "semantic_memory_contradiction"}:
            contradicted_semantic_ids.update(_int_ids(metadata.get("semantic_memory_ids")))
            contradicted_semantic_ids.update(_int_ids(metadata.get("semantic_memory_id")))

        if event_type == "procedure_result":
            if metadata.get("succeeded") is True:
                successful_procedure_ids.update(_int_ids(metadata.get("procedure_id")))
            elif metadata.get("succeeded") is False:
                failed_procedure_ids.update(_int_ids(metadata.get("procedure_id")))

    return LifecycleSignals(
        referenced_semantic_ids=referenced_semantic_ids,
        confirmed_semantic_ids=confirmed_semantic_ids,
        contradicted_semantic_ids=contradicted_semantic_ids,
        failed_procedure_ids=failed_procedure_ids,
        successful_procedure_ids=successful_procedure_ids,
    )


def archive_expired_semantic_memories(store: MemoryStore) -> int:
    archived = 0
    now = datetime.now(timezone.utc)
    expired_memories = (
        store.expired_semantic_memories()
        if hasattr(store, "expired_semantic_memories")
        else store.active_semantic_memories()
    )
    for memory in expired_memories:
        expires_at = memory.get("expires_at")
        if not expires_at:
            continue
        expires = _parse_sqlite_datetime(expires_at)
        if expires is not None and expires <= now:
            store.archive_semantic_memory(memory["id"], reason="expired")
            archived += 1
    return archived


def archive_contradicted_semantic_memories(store: MemoryStore, signals: LifecycleSignals) -> int:
    archived = 0
    for memory in store.active_semantic_memories():
        if memory["id"] in signals.contradicted_semantic_ids:
            store.archive_semantic_memory(memory["id"], reason="contradicted")
            archived += 1
    return archived


def update_semantic_lifecycle(
    store: MemoryStore,
    policy: ForgettingPolicyConfig,
    signals: LifecycleSignals,
) -> dict[str, int]:
    decayed = 0
    reinforced = 0
    archived = 0
    for memory in store.active_semantic_memories():
        confidence = float(memory["confidence"])
        new_confidence = confidence
        if memory["id"] in signals.confirmed_semantic_ids:
            new_confidence += policy.semantic_confirmation_boost
        elif memory["id"] in signals.referenced_semantic_ids:
            new_confidence += policy.semantic_access_boost
        else:
            new_confidence -= policy.semantic_decay
        new_confidence = _clamp(new_confidence)

        if new_confidence != confidence:
            store.update_semantic_confidence(memory["id"], new_confidence)
            if new_confidence > confidence:
                reinforced += 1
            else:
                decayed += 1
        if new_confidence < policy.min_semantic_confidence:
            store.archive_semantic_memory(memory["id"], reason="low_confidence")
            archived += 1

    return {
        "decayed_semantic_memories": decayed,
        "reinforced_semantic_memories": reinforced,
        "archived_semantic_memories": archived,
    }


def update_procedure_lifecycle(store: MemoryStore, policy: ForgettingPolicyConfig) -> int:
    archived = 0
    for procedure in store.active_procedures():
        successes = int(procedure["success_count"])
        failures = int(procedure["failure_count"])
        attempts = successes + failures
        confidence = float(procedure["confidence"])
        failure_ratio = failures / attempts if attempts else 0.0

        should_archive = (
            failures >= policy.max_procedure_failures
            and successes == 0
            or attempts >= policy.min_procedure_attempts_for_ratio
            and failure_ratio >= policy.procedure_failure_ratio
            or confidence < policy.procedure_low_confidence
            and failures > successes
        )
        if should_archive:
            store.archive_procedure(procedure["id"], reason=_procedure_archive_reason(procedure, failure_ratio))
            archived += 1
    return archived


def resolve_conflicts(store: MemoryStore, config: ForgettingPolicyConfig | None = None) -> int:
    policy = config or ForgettingPolicyConfig()
    grouped = {}
    for memory in store.active_semantic_memories():
        grouped.setdefault((memory["subject"], memory["predicate"]), []).append(memory)

    archived = 0
    for memories in grouped.values():
        if len(memories) < 2:
            continue
        winner = choose_conflict_winner(memories)
        winner_confidence = float(winner["confidence"])
        for memory in memories:
            if memory["id"] == winner["id"]:
                continue
            confidence_gap = winner_confidence - float(memory["confidence"])
            if confidence_gap >= policy.semantic_conflict_margin or memory["object"] != winner["object"]:
                store.archive_semantic_memory(memory["id"], reason=f"conflicts_with:{winner['id']}")
                archived += 1
    return archived


def choose_conflict_winner(memories: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(
        memories,
        key=lambda item: (
            float(item["confidence"]),
            _parse_sqlite_datetime(item.get("updated_at")) or datetime.min.replace(tzinfo=timezone.utc),
            int(item["id"]),
        ),
        reverse=True,
    )[0]


def _procedure_archive_reason(procedure: dict[str, Any], failure_ratio: float) -> str:
    successes = int(procedure["success_count"])
    failures = int(procedure["failure_count"])
    if failures and successes == 0:
        return "too_many_failures"
    if failure_ratio:
        return f"high_failure_ratio:{failure_ratio:.2f}"
    return "low_confidence"


def _int_ids(value: Any) -> set[int]:
    if value is None:
        return set()
    if isinstance(value, int):
        return {value}
    if isinstance(value, str):
        try:
            return {int(value)}
        except ValueError:
            return set()
    if isinstance(value, list | tuple | set):
        ids: set[int] = set()
        for item in value:
            ids.update(_int_ids(item))
        return ids
    return set()


def _parse_sqlite_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))
