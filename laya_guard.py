from __future__ import annotations

from dataclasses import dataclass
from typing import Any


GUARD_QUESTIONS = {
    "intent": {
        "type": "choice",
        "instructions": "What does the user want the local assistant to do?",
        "criteria": {
            "chat": "general conversation or question answering",
            "read_file": "read, inspect, summarize, or search local files",
            "write_file": "create, edit, overwrite, or delete local files",
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


@dataclass(frozen=True)
class GuardDecision:
    intent: str = "chat"
    risk: float = 0.0
    needs_confirmation: bool = False
    available: bool = True
    reason: str = ""


class LayaGuard:
    def __init__(self, preload: bool = False) -> None:
        self._router: Any | None = None
        self._load_error: str = ""
        try:
            from laya import Router

            self._router = Router(preload=preload)
        except Exception as error:
            self._load_error = str(error)

    @property
    def available(self) -> bool:
        return self._router is not None

    def assess(self, user_input: str) -> GuardDecision:
        if self._router is None:
            return GuardDecision(
                available=False,
                reason=f"Laya unavailable: {self._load_error}" if self._load_error else "Laya unavailable",
            )

        try:
            result = self._router.predict({"message": user_input}, GUARD_QUESTIONS)
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
