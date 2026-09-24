"""Small HTTP bridge for chatting with the ASCS Ollama endpoint."""

from __future__ import annotations

import os
from typing import Any, Callable

from openai import APIStatusError, AuthenticationError
from openai import OpenAI

from base import TOOLS_SCHEMAS, load_dotenv, run_tool
from laya_guard import GuardDecision, LayaGuard
from memory_classifier import LayaMemoryClassifier, MemoryCandidateDecision
from memory_review import process_memory_review_candidates
from forgetting import run_forgetting_policy
from retrieval_ranker import LayaMemoryRanker
from memory import MemoryStore
from retrieval import build_memory_context, inject_memory_context
from working_memory import compact_messages


load_dotenv()

OLLAMA_BASE_URL = os.environ["OLLAMA_BASE_URL"]
OLLAMA_MODEL = os.environ["OLLAMA_MODEL"]
OLLAMA_API_KEY = os.environ["OLLAMA_API_KEY"]

def validate_ollama_api_key(api_key: str) -> None:
    if not api_key.startswith("sk-"):
        raise ValueError("OLLAMA_API_KEY must be a LiteLLM virtual key that starts with 'sk-'.")


def create_ollama_client() -> OpenAI:
    validate_ollama_api_key(OLLAMA_API_KEY)
    return OpenAI(
        base_url=OLLAMA_BASE_URL,
        api_key=OLLAMA_API_KEY,
    )


client: OpenAI | None = None


def get_client() -> OpenAI:
    global client
    if client is None:
        client = create_ollama_client()
    return client


def format_authentication_error(error: AuthenticationError) -> str:
    return f"Authentication failed for {OLLAMA_BASE_URL}. Check OLLAMA_API_KEY in .env. {error}"


def format_api_status_error(error: APIStatusError) -> str:
    if error.status_code in (401, 403):
        return (
            f"Request was rejected by {OLLAMA_BASE_URL} with HTTP {error.status_code}. "
            "Check OLLAMA_BASE_URL, OLLAMA_API_KEY, and whether the key can access "
            f"OLLAMA_MODEL={OLLAMA_MODEL!r}. {error}"
        )
    return f"Request to {OLLAMA_BASE_URL} failed with HTTP {error.status_code}. {error}"


SYSTEM_PROMPT = ""    

message = [
    {"role": "user", "content": "You are a helpful assistant."}
]


def format_guard_notice(decision: GuardDecision) -> str:
    if not decision.available:
        return f"Laya guard unavailable; continuing without guard. {decision.reason}"
    if decision.needs_confirmation or decision.risk >= 1.5:
        return (
            "Laya guard: "
            f"intent={decision.intent}, risk={decision.risk:.2f}, "
            f"needs_confirmation={decision.needs_confirmation}"
        )
    return ""


def safe_memory_call(operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return operation(*args, **kwargs)
    except Exception as error:
        print(f"\nMemory warning: {error}")
        return None


def log_episode_event(
    memory: MemoryStore | None,
    episode_id: int | None,
    event_type: str,
    role: str | None = None,
    content: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    if memory is None or episode_id is None:
        return
    safe_memory_call(
        memory.add_event,
        episode_id,
        event_type,
        role=role,
        content=content,
        metadata=metadata,
    )


def finish_episode_safely(
    memory: MemoryStore | None,
    episode_id: int | None,
    status: str = "completed",
    summary: str | None = None,
) -> None:
    if memory is None or episode_id is None:
        return
    safe_memory_call(memory.finish_episode, episode_id, status=status, summary=summary)


def episode_events_safely(memory: MemoryStore | None, episode_id: int | None) -> list[dict[str, Any]]:
    if memory is None or episode_id is None or not hasattr(memory, "episode_events"):
        return []
    return safe_memory_call(memory.episode_events, episode_id) or []


def classify_memory_safely(
    memory_classifier: LayaMemoryClassifier,
    events: list[dict[str, Any]],
) -> MemoryCandidateDecision:
    result = safe_memory_call(memory_classifier.assess_episode, events)
    if isinstance(result, MemoryCandidateDecision):
        return result
    return MemoryCandidateDecision(available=False, reason="Memory classifier failed")


def queue_memory_review_candidate(
    memory: MemoryStore | None,
    episode_id: int | None,
    candidate: MemoryCandidateDecision,
) -> None:
    if not candidate.should_extract:
        return
    log_episode_event(
        memory,
        episode_id,
        "memory_review_candidate",
        metadata={
            "memory_kind": candidate.memory_kind,
            "confidence": candidate.confidence,
            "reason": candidate.reason,
        },
    )


def run_agent(messages, memory: MemoryStore | None = None, episode_id: int | None = None):
    while True:
        response = get_client().chat.completions.create(
            model = OLLAMA_MODEL,
            messages = messages,
            tools = TOOLS_SCHEMAS
        )
        assistant_message = response.choices[0].message
        messages.append(assistant_message)

        if assistant_message.tool_calls:
            for tool_call in assistant_message.tool_calls:
                log_episode_event(
                    memory,
                    episode_id,
                    "tool_call",
                    metadata={
                        "tool_call_id": tool_call.id,
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                )
                result = run_tool(tool_call)
                log_episode_event(
                    memory,
                    episode_id,
                    "tool_result",
                    content=str(result),
                    metadata={"tool_call_id": tool_call.id},
                )
                messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": str(result)})
            continue

        return assistant_message.content or ""


def read_user_input(prompt: str = "\nYou: ") -> str:
    try:
        return input(prompt)
    except KeyboardInterrupt:
        print()
        return "exit"


def main():
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    guard = LayaGuard()
    memory_classifier = LayaMemoryClassifier()
    memory_ranker = LayaMemoryRanker()
    memory = safe_memory_call(MemoryStore)
    print("Mini agent ready. Type 'exit' to quit.")

    while True:
        user_input = read_user_input()
        if user_input.lower() in ("exit", "quit"):
            break

        episode_id = safe_memory_call(memory.start_episode) if memory is not None else None
        log_episode_event(memory, episode_id, "message", role="user", content=user_input)

        guard_decision = guard.assess(user_input)
        guard_notice = format_guard_notice(guard_decision)
        log_episode_event(
            memory,
            episode_id,
            "guard_decision",
            metadata={"guard": guard_decision},
        )
        if guard_notice:
            print(f"\n{guard_notice}")

        messages.append({"role": "user", "content": user_input})
        memory_context = safe_memory_call(build_memory_context, memory, query=user_input, ranker=memory_ranker) if memory is not None else ""
        if memory_context:
            log_episode_event(memory, episode_id, "retrieval_context", content=memory_context)
        agent_messages = inject_memory_context(messages, memory_context)
        compacted_messages, working_summary, preservation_decision = compact_messages(
            agent_messages,
            preservation_classifier=getattr(guard, "_agent", None),
        )
        if working_summary is not None:
            agent_messages = compacted_messages
            log_episode_event(memory, episode_id, "working_memory_summary", content=working_summary)
            if preservation_decision is not None and preservation_decision.should_preserve:
                log_episode_event(
                    memory,
                    episode_id,
                    "working_memory_preservation_candidate",
                    metadata={"preservation": preservation_decision},
                )
        try:
            reply = run_agent(agent_messages, memory=memory, episode_id=episode_id)
        except ValueError as e:
            log_episode_event(memory, episode_id, "error", content=str(e), metadata={"error_type": "ValueError"})
            finish_episode_safely(memory, episode_id, status="failed")
            print(f"\nConfiguration error: {e}")
            break
        except AuthenticationError as e:
            log_episode_event(memory, episode_id, "error", content=str(e), metadata={"error_type": "AuthenticationError"})
            finish_episode_safely(memory, episode_id, status="failed")
            print(f"\n{format_authentication_error(e)}")
            break
        except APIStatusError as e:
            log_episode_event(memory, episode_id, "error", content=str(e), metadata={"error_type": "APIStatusError"})
            finish_episode_safely(memory, episode_id, status="failed")
            print(f"\n{format_api_status_error(e)}")
            break
        messages.append({"role": "assistant", "content": reply})
        log_episode_event(memory, episode_id, "message", role="assistant", content=reply)
        episode_events = episode_events_safely(memory, episode_id)
        memory_candidate = classify_memory_safely(memory_classifier, episode_events or [])
        log_episode_event(
            memory,
            episode_id,
            "memory_candidate_decision",
            metadata={"candidate": memory_candidate},
        )
        queue_memory_review_candidate(memory, episode_id, memory_candidate)
        safe_memory_call(process_memory_review_candidates, memory, episode_id) if memory is not None and episode_id is not None else None
        safe_memory_call(run_forgetting_policy, memory) if memory is not None else None
        finish_episode_safely(memory, episode_id)
        print(f"\nMiniAgent: {reply}")

if __name__ == "__main__":
    main()
    
