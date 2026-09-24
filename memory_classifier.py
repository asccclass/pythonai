from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

from laya_guard import DEFAULT_MODEL_DIR


MEMORY_QUESTIONS = {
    "memory_kind": {
        "type": "choice",
        "instructions": "What kind of long-term memory candidate does this episode contain?",
        "criteria": {
            "none": "no durable memory should be extracted",
            "semantic": "durable fact, user preference, entity, or relationship",
            "procedure": "reusable successful workflow or task method",
            "forgetting": "conflict, obsolete information, stale preference, or memory to archive",
        },
    },
    "should_extract": {
        "type": "noul",
        "instructions": "Should the memory service review this episode for extraction or archival?",
    },
    "confidence": {
        "type": "score",
        "instructions": "How confident are you that this episode contains a useful memory candidate?",
        "criteria": ["low", "medium", "high"],
    },
}


@dataclass(frozen=True)
class MemoryCandidateDecision:
    memory_kind: str = "none"
    should_extract: bool = False
    confidence: float = 0.0
    available: bool = True
    reason: str = ""


class LayaMemoryClassifier:
    def __init__(self, model_dir: str | Path | None = None) -> None:
        self._agent: Any | None = None
        self._load_error = ""
        self.model_dir = Path(model_dir or os.environ.get("LAYA_MODEL_DIR", DEFAULT_MODEL_DIR))
        try:
            import laya

            if not self.model_dir.exists():
                raise FileNotFoundError(f"Laya model directory not found: {self.model_dir}")
            self._agent = laya.load(str(self.model_dir))
        except Exception as error:
            self._load_error = str(error)

    @property
    def available(self) -> bool:
        return self._agent is not None

    def assess_episode(self, events: list[dict[str, Any]]) -> MemoryCandidateDecision:
        if self._agent is None:
            return MemoryCandidateDecision(
                available=False,
                reason=f"Laya memory classifier unavailable: {self._load_error}"
                if self._load_error
                else "Laya memory classifier unavailable",
            )

        try:
            result = self._agent.predict({"episode": format_episode_for_laya(events)}, MEMORY_QUESTIONS)
        except Exception as error:
            return MemoryCandidateDecision(available=False, reason=f"Laya memory classifier failed: {error}")

        answers = result.get("answers", {})
        kind_answer = answers.get("memory_kind", {})
        extract_answer = answers.get("should_extract", {})
        confidence_answer = answers.get("confidence", {})
        return MemoryCandidateDecision(
            memory_kind=str(kind_answer.get("choice", "none")),
            should_extract=bool(extract_answer.get("noul", False)),
            confidence=float(confidence_answer.get("score", 0.0)),
        )


def format_episode_for_laya(events: list[dict[str, Any]]) -> str:
    lines = []
    for event in events:
        event_type = event.get("event_type", "unknown")
        role = event.get("role") or "-"
        content = str(event.get("content") or "").replace("\r", " ").replace("\n", " ").strip()
        metadata = event.get("metadata") or {}
        metadata_text = f" metadata={metadata}" if metadata else ""
        lines.append(f"{event_type} role={role}: {content}{metadata_text}")
    return "\n".join(lines)
