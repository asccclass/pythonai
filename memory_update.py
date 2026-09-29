from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from semantic_extractor import SemanticTriple


LOW_CONFIDENCE_THRESHOLD = 0.2
REINFORCE_BOOST = 0.05


@dataclass(frozen=True)
class MemoryUpdateDecision:
    action: str
    reason: str
    existing_memory_id: int | None = None


def decide_semantic_update(
    triple: SemanticTriple,
    existing_memories: list[dict[str, Any]],
    candidate_confidence: float,
) -> MemoryUpdateDecision:
    effective_confidence = min(candidate_confidence, triple.confidence)
    if effective_confidence < LOW_CONFIDENCE_THRESHOLD:
        return MemoryUpdateDecision("ignore", "low_confidence")

    exact_match = find_exact_memory(triple, existing_memories)
    if exact_match is not None:
        return MemoryUpdateDecision("reinforce", "same_fact", int(exact_match["id"]))

    related_memory = find_related_memory(triple, existing_memories)
    if related_memory is not None:
        return MemoryUpdateDecision("supersede", "same_subject_predicate_new_object", int(related_memory["id"]))

    return MemoryUpdateDecision("add", "new_fact")


def find_exact_memory(triple: SemanticTriple, memories: list[dict[str, Any]]) -> dict[str, Any] | None:
    target = semantic_memory_key(triple.subject, triple.predicate, triple.object_value)
    for memory in memories:
        if semantic_memory_key(memory["subject"], memory["predicate"], memory["object"]) == target:
            return memory
    return None


def find_related_memory(triple: SemanticTriple, memories: list[dict[str, Any]]) -> dict[str, Any] | None:
    target_subject = normalize_memory_part(triple.subject)
    target_predicate = normalize_memory_part(triple.predicate)
    same_relation = [
        memory
        for memory in memories
        if normalize_memory_part(memory["subject"]) == target_subject
        and normalize_memory_part(memory["predicate"]) == target_predicate
    ]
    if not same_relation:
        return None
    return sorted(
        same_relation,
        key=lambda memory: (
            float(memory["confidence"]),
            str(memory.get("updated_at", "")),
            int(memory["id"]),
        ),
        reverse=True,
    )[0]


def reinforced_confidence(current: float, candidate: float, boost: float = REINFORCE_BOOST) -> float:
    return min(1.0, max(float(current), float(candidate)) + boost)


def semantic_memory_key(subject: str, predicate: str, object_value: str) -> tuple[str, str, str]:
    return (normalize_memory_part(subject), normalize_memory_part(predicate), normalize_memory_part(object_value))


def normalize_memory_part(value: str) -> str:
    return " ".join(str(value).strip().casefold().split())
