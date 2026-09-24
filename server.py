"""Small HTTP bridge for chatting with the ASCS Ollama endpoint."""

from __future__ import annotations

import os
import time
from typing import Any, Callable

from openai import APIConnectionError, APIStatusError, APITimeoutError, AuthenticationError
from openai import OpenAI

from base import TOOLS_SCHEMAS, load_dotenv, run_tool
from laya_guard import GuardDecision, LayaGuard
from memory_classifier import LayaMemoryClassifier, MemoryCandidateDecision
from memory_review import process_memory_review_candidates
from procedure_similarity import LLMProcedureSimilarityMatcher
from request_budget import background_memory_budget, foreground_memory_budget
from semantic_extractor import LLMSemanticExtractor
from forgetting import run_forgetting_policy
from memory import MemoryStore
from retrieval import build_memory_context, inject_memory_context
from vector_search import OpenAICompatibleEmbeddingProvider, VectorMemorySearcher
from working_memory import compact_messages


load_dotenv()

OLLAMA_BASE_URL = os.environ["OLLAMA_BASE_URL"]
OLLAMA_MODEL = os.environ["OLLAMA_MODEL"]
OLLAMA_EMBEDDING_MODEL = os.environ.get("OLLAMA_EMBEDDING_MODEL", OLLAMA_MODEL)
OLLAMA_API_KEY = os.environ["OLLAMA_API_KEY"]
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}

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


def format_api_connection_error(error: APIConnectionError) -> str:
    return (
        f"Could not reach {OLLAMA_BASE_URL}. "
        "Check your network connection, OLLAMA_BASE_URL, and whether the remote service is available. "
        f"{error}"
    )


def retry_delay_seconds(error: APIStatusError, default: float = 5.0) -> float:
    response = getattr(error, "response", None)
    if response is None:
        return default
    retry_after = response.headers.get("retry-after")
    if retry_after is None:
        return default
    try:
        return max(0.0, float(retry_after))
    except ValueError:
        return default


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


import queue
import random
import threading


class MemoryReviewWorker:
    def __init__(self, async_mode: bool = True) -> None:
        self.queue: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self.async_mode = async_mode
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        if self.async_mode:
            self._thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._thread.start()

    def enqueue(
        self,
        memory: MemoryStore | None,
        episode_id: int | None,
        memory_classifier: Any,
        semantic_extractor: Any,
        procedure_matcher: Any,
    ) -> None:
        if memory is None or episode_id is None or not hasattr(memory, "episode_events"):
            return
        item = (memory, episode_id, memory_classifier, semantic_extractor, procedure_matcher)
        if self.async_mode:
            self.queue.put(item)
        else:
            self._process_item(item)

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._process_item(item)
            finally:
                self.queue.task_done()

    def _process_item(self, item: tuple[Any, ...]) -> None:
        memory, episode_id, memory_classifier, semantic_extractor, procedure_matcher = item
        if callable(memory_classifier):
            memory_classifier = memory_classifier()
        review_budget = background_memory_budget()
        if hasattr(semantic_extractor, "allow_remote"):
            semantic_extractor.allow_remote = review_budget.try_acquire
        if hasattr(procedure_matcher, "allow_remote"):
            procedure_matcher.allow_remote = review_budget.try_acquire
        episode_events = episode_events_safely(memory, episode_id)
        memory_candidate = classify_memory_safely(memory_classifier, episode_events or [])
        log_episode_event(
            memory,
            episode_id,
            "memory_candidate_decision",
            metadata={"candidate": memory_candidate},
        )
        queue_memory_review_candidate(memory, episode_id, memory_candidate)
        safe_memory_call(
            process_memory_review_candidates,
            memory,
            episode_id,
            semantic_extractor=semantic_extractor,
            procedure_matcher=procedure_matcher,
        ) if memory is not None and episode_id is not None else None
        safe_memory_call(run_forgetting_policy, memory) if memory is not None else None
        finish_episode_safely(memory, episode_id)

    def join(self) -> None:
        if self.async_mode and self._thread is not None and self._thread.is_alive():
            self.queue.join()

    def stop(self) -> None:
        self._stop_event.set()
        if self.async_mode and self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)


def run_agent(
    messages,
    memory: MemoryStore | None = None,
    episode_id: int | None = None,
    max_retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
):
    while True:
        attempts = 0
        while True:
            try:
                response = get_client().chat.completions.create(
                    model = OLLAMA_MODEL,
                    messages = messages,
                    tools = TOOLS_SCHEMAS
                )
                break
            except APIStatusError as error:
                if error.status_code not in TRANSIENT_STATUS_CODES or attempts >= max_retries:
                    raise
                attempts += 1
                base_delay = retry_delay_seconds(error)
                jitter = random.uniform(0.1, 1.0) if base_delay > 0 else 0.0
                delay = base_delay * (2 ** (attempts - 1)) + jitter
                print(f"\nRemote service returned HTTP {error.status_code}; retrying in {delay:g} seconds.")
                sleep(delay)
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


def main(async_memory_review: bool = True, drain_memory_on_exit: bool = False):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    guard = LayaGuard()
    memory_classifier_factory = LayaMemoryClassifier
    memory = safe_memory_call(MemoryStore)
    memory_searcher = VectorMemorySearcher(OpenAICompatibleEmbeddingProvider(get_client, OLLAMA_EMBEDDING_MODEL), store=memory)
    semantic_extractor = LLMSemanticExtractor(get_client, OLLAMA_MODEL)
    procedure_matcher = LLMProcedureSimilarityMatcher(get_client, OLLAMA_MODEL)
    worker = MemoryReviewWorker(async_mode=async_memory_review)
    print("Mini agent ready. Type 'exit' to quit.")

    try:
        while True:
            user_input = read_user_input()
            if user_input.lower() in ("exit", "quit"):
                break

            turn_budget = foreground_memory_budget()
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
            memory_context = (
                safe_memory_call(
                    build_memory_context,
                    memory,
                    query=user_input,
                    vector_searcher=memory_searcher,
                    allow_query_embedding=turn_budget.try_acquire("memory_query_embedding"),
                    max_missing_embeddings=turn_budget.remaining("memory_embedding_backfill") or 0,
                )
                if memory is not None
                else ""
            )
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
            except APITimeoutError as e:
                log_episode_event(memory, episode_id, "error", content=str(e), metadata={"error_type": "APITimeoutError"})
                finish_episode_safely(memory, episode_id, status="failed")
                print(f"\n{format_api_connection_error(e)}")
                break
            except APIConnectionError as e:
                log_episode_event(memory, episode_id, "error", content=str(e), metadata={"error_type": "APIConnectionError"})
                finish_episode_safely(memory, episode_id, status="failed")
                print(f"\n{format_api_connection_error(e)}")
                break
            messages.append({"role": "assistant", "content": reply})
            log_episode_event(memory, episode_id, "message", role="assistant", content=reply)
            print(f"\nMiniAgent: {reply}")
            worker.enqueue(memory, episode_id, memory_classifier_factory, semantic_extractor, procedure_matcher)
            if not async_memory_review:
                worker.join()
    finally:
        if drain_memory_on_exit:
            worker.join()
        worker.stop()

if __name__ == "__main__":
    main()
    
