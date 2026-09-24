from __future__ import annotations

from typing import Any


DEFAULT_MAX_MESSAGES = 12
DEFAULT_KEEP_RECENT = 8


def compact_messages(
    messages: list[dict[str, Any]],
    max_messages: int = DEFAULT_MAX_MESSAGES,
    keep_recent: int = DEFAULT_KEEP_RECENT,
) -> tuple[list[dict[str, Any]], str | None]:
    if len(messages) <= max_messages:
        return messages, None

    system_messages = [message for message in messages if message.get("role") == "system"]
    non_system_messages = [message for message in messages if message.get("role") != "system"]
    older_messages = non_system_messages[:-keep_recent]
    recent_messages = non_system_messages[-keep_recent:]
    summary = summarize_messages(older_messages)

    compacted = [
        *system_messages[:1],
        {"role": "system", "content": f"Earlier conversation summary:\n{summary}"},
        *recent_messages,
    ]
    return compacted, summary


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
