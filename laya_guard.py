from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any


GUARD_QUESTIONS = {
    "intent": {
        "type": "choice",
        "instructions": "What does the user want the local assistant to do?",
        "criteria": {
            "chat": "general conversation or question answering",
            "read_file": "read, inspect, summarize, or search local files",
            "write_file": "create, edit, overwrite local files",
            "delete_file": "delete local files",
            "run_command": "run terminal, shell, package manager, or system commands",
        },
    },
    "risk": {
        "type": "score",
        "instructions": "How risky is this request for the local machine or project state?",
        "criteria": ["safe", "needs caution", "dangerous"],
    },
    "needs_confirmation": {
        "type": "noul",
        "instructions": "Should this request require explicit user confirmation before execution?",
    },
}

DEFAULT_MODEL_DIR = Path(__file__).resolve().parent / "models" / "laya-multilingual"


@dataclass(frozen=True)
class GuardDecision:
    intent: str = "chat"
    risk: float = 0.0
    needs_confirmation: bool = False
    available: bool = True
    reason: str = ""


class LayaGuard:
    def __init__(self, model_dir: str | Path | None = None) -> None:
        self._agent: Any | None = None
        self._load_error: str = ""
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

    def assess(self, user_input: str) -> GuardDecision:
        if self._agent is None:
            return GuardDecision(
                available=False,
                reason=f"Laya unavailable: {self._load_error}" if self._load_error else "Laya unavailable",
            )

        try:
            result = self._agent.predict({"message": user_input}, GUARD_QUESTIONS)
        except Exception as error:
            return GuardDecision(available=False, reason=f"Laya prediction failed: {error}")

        answers = result.get("answers", {})
        intent_answer = answers.get("intent", {})
        risk_answer = answers.get("risk", {})
        confirmation_answer = answers.get("needs_confirmation", {})

        return GuardDecision(
            intent=str(intent_answer.get("choice", "chat")),
            risk=float(risk_answer.get("score", 0.0)),
            needs_confirmation=bool(confirmation_answer.get("noul", False)),
        )
