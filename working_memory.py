from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEFAULT_MAX_MESSAGES = 12
DEFAULT_KEEP_RECENT = 8


PRESERVATION_QUESTIONS = {
    "should_preserve": {
        "type": "noul",
        "instructions": "Does this older context contain durable facts, reusable procedures, or important preferences?",
    },
    "preservation_kind": {
        "type": "choice",
        "instructions": "What should be preserved from this older context?",
        "criteria": {
            "none": "nothing durable",
            "semantic": "facts, preferences, entities, or relationships",
            "procedure": "reusable workflow or task method",
            "both": "both semantic facts and reusable procedure",
        },
    },
}


@dataclass(frozen=True)
class PreservationDecision:
    should_preserve: bool = False
    preservation_kind: str = "none"
    available: bool = True
    reason: str = ""


def compact_messages(
    messages: list[dict[str, Any]],
    max_messages: int = DEFAULT_MAX_MESSAGES,
    keep_recent: int = DEFAULT_KEEP_RECENT,
    preservation_classifier: Any | None = None,
) -> tuple[list[dict[str, Any]], str | None, PreservationDecision | None]:
    if len(messages) <= max_messages:
        return messages, None, None

    system_messages = [message for message in messages if message.get("role") == "system"]
    non_system_messages = [message for message in messages if message.get("role") != "system"]
    older_messages = non_system_messages[:-keep_recent]
    recent_messages = non_system_messages[-keep_recent:]
    summary = summarize_messages(older_messages)
    preservation_decision = assess_preservation(summary, preservation_classifier)

    compacted = [
        *system_messages,
        {"role": "system", "content": f"Earlier conversation summary:\n{summary}"},
        *recent_messages,
    ]
    return compacted, summary, preservation_decision


def assess_preservation(summary: str, preservation_classifier: Any | None = None) -> PreservationDecision:
    if preservation_classifier is None:
        return PreservationDecision()
    try:
        result = preservation_classifier.predict({"context": summary}, PRESERVATION_QUESTIONS)
        answers = result.get("answers", {})
        preserve_answer = answers.get("should_preserve", {})
        kind_answer = answers.get("preservation_kind", {})
        return PreservationDecision(
            should_preserve=bool(preserve_answer.get("noul", False)),
            preservation_kind=str(kind_answer.get("choice", "none")),
        )
    except Exception as error:
        return PreservationDecision(available=False, reason=str(error))


def summarize_messages(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return "No earlier conversation."

    lines = []
    for index, message in enumerate(messages, start=1):
        role = str(message.get("role", "unknown"))
        content = normalize_content(message.get("content", ""))
        lines.append(f"{index}. {role}: {content}")
    return "\n".join(lines)


def normalize_content(content: Any, max_length: int = 240) -> str:
    text = str(content).replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."
