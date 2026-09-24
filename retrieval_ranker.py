from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from laya_guard import DEFAULT_MODEL_DIR


RELEVANCE_QUESTIONS = {
    "relevance": {
        "type": "score",
        "instructions": "How relevant is this memory to the current user request?",
        "criteria": ["irrelevant", "somewhat relevant", "highly relevant"],
    }
}


class LayaMemoryRanker:
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

    def rank(self, query: str, memories: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self._agent is None or not query or not memories:
            return memories

        scored = []
        try:
            for memory in memories:
                result = self._agent.predict(
                    {
                        "query": query,
                        "memory": format_memory_for_laya(memory),
                    },
                    RELEVANCE_QUESTIONS,
                )
                score = float(result.get("answers", {}).get("relevance", {}).get("score", 0.0))
                scored.append((score, memory["confidence"], memory["id"], memory))
        except Exception:
            return memories

        scored.sort(reverse=True)
        return [memory for _, _, _, memory in scored]


def format_memory_for_laya(memory: dict[str, Any]) -> str:
    return f"{memory['subject']} {memory['predicate']} {memory['object']}"
