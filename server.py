"""Small HTTP bridge for chatting with the ASCS Ollama endpoint."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable

from openai import APIConnectionError, APIStatusError, APITimeoutError, AuthenticationError
from openai import OpenAI

from agent_runtime import AgentRuntime, format_guard_notice, run_agent_turn, should_skip_laya_for_user_request
from base import TOOLS_SCHEMAS, load_dotenv, run_skill, run_tool_with_context
from laya_guard import GuardDecision, LayaGuard
from memory_classifier import LayaMemoryClassifier, MemoryCandidateDecision
from procedure_similarity import LLMProcedureSimilarityMatcher, LexicalProcedureSimilarityMatcher, ProcedureCandidate
from semantic_extractor import LLMSemanticExtractor
from memory import MemoryStore
from memory_worker import BackgroundWorkerLease, MemoryReviewWorker, QueueOnlyMemoryWorker
from scheduler import SchedulerWorker
from skills import SkillMatcher, SkillRegistry
from vector_search import OpenAICompatibleEmbeddingProvider, VectorMemorySearcher


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
AGENTS_INSTRUCTIONS_PATH = Path("AGENTS.md")


def load_agents_instructions(path: str | Path = AGENTS_INSTRUCTIONS_PATH) -> str:
    agents_path = Path(path)
    if not agents_path.exists():
        return ""
    content = agents_path.read_text(encoding="utf-8").strip()
    return content


def build_system_prompt(base_prompt: str = SYSTEM_PROMPT, agents_path: str | Path = AGENTS_INSTRUCTIONS_PATH) -> str:
    agents_instructions = load_agents_instructions(agents_path)
    if not agents_instructions:
        return base_prompt
    parts = [part for part in [base_prompt.strip(), f"AGENTS.md instructions:\n{agents_instructions}"] if part]
    return "\n\n".join(parts)

message = [
    {"role": "user", "content": "You are a helpful assistant."}
]


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


import random


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
        messages.append(assistant_message_to_dict(assistant_message))

        if assistant_message.tool_calls:
            for tool_call in assistant_message.tool_calls:
                matched_procedure = find_matching_procedure_for_tool_call(memory, tool_call)
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
                result = run_tool_with_context(tool_call, memory=memory, episode_id=episode_id)
                log_episode_event(
                    memory,
                    episode_id,
                    "tool_result",
                    content=str(result),
                    metadata={"tool_call_id": tool_call.id},
                )
                record_tool_procedure_result(memory, episode_id, matched_procedure, result)
                messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": str(result)})
            continue

        return assistant_message.content or ""


def find_matching_procedure_for_tool_call(
    memory: MemoryStore | None,
    tool_call: Any,
) -> dict[str, Any] | None:
    if memory is None or not hasattr(memory, "active_procedures"):
        return None
    tool_name = tool_call.function.name
    procedures = safe_memory_call(memory.active_procedures, tool_name) or []
    if not procedures:
        return None
    candidate = ProcedureCandidate(
        task_type=tool_name,
        context_pattern=tool_name,
        steps=[f"{tool_name} {tool_call.function.arguments}"],
    )
    match = LexicalProcedureSimilarityMatcher().find_match(candidate, procedures, threshold=0.2)
    if match is None:
        return None
    return next((procedure for procedure in procedures if procedure["id"] == match.procedure_id), None)


def record_tool_procedure_result(
    memory: MemoryStore | None,
    episode_id: int | None,
    procedure: dict[str, Any] | None,
    result: Any,
) -> None:
    if memory is None or episode_id is None or procedure is None:
        return
    succeeded = tool_result_succeeded(result)
    safe_memory_call(memory.record_procedure_result, int(procedure["id"]), succeeded=succeeded)
    log_episode_event(
        memory,
        episode_id,
        "procedure_result",
        metadata={
            "procedure_id": procedure["id"],
            "succeeded": succeeded,
            "task_type": procedure["task_type"],
        },
    )


def tool_result_succeeded(result: Any) -> bool:
    return not str(result).lstrip().lower().startswith("error:")


def assistant_message_to_dict(assistant_message: Any) -> dict[str, Any]:
    if hasattr(assistant_message, "model_dump"):
        return assistant_message.model_dump(exclude_none=True)

    message = {"role": "assistant", "content": getattr(assistant_message, "content", None)}
    tool_calls = getattr(assistant_message, "tool_calls", None)
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def read_user_input(prompt: str = "\nYou: ") -> str:
    try:
        return input(prompt)
    except KeyboardInterrupt:
        print()
        return "exit"


def main(async_memory_review: bool = True, drain_memory_on_exit: bool = False):
    messages = [{"role": "system", "content": build_system_prompt()}]
    guard = LayaGuard()
    memory_classifier_factory = LayaMemoryClassifier
    memory = safe_memory_call(MemoryStore)
    memory_searcher = VectorMemorySearcher(OpenAICompatibleEmbeddingProvider(get_client, OLLAMA_EMBEDDING_MODEL), store=memory)
    skill_matcher = SkillMatcher(SkillRegistry())
    semantic_extractor = LLMSemanticExtractor(get_client, OLLAMA_MODEL)
    procedure_matcher = LLMProcedureSimilarityMatcher(get_client, OLLAMA_MODEL)
    scheduler_worker = SchedulerWorker()
    scheduler_worker.start()

    worker_lease = BackgroundWorkerLease.acquire(memory)
    if worker_lease.acquired:
        worker = MemoryReviewWorker(
            async_mode=async_memory_review,
            embedding_provider=memory_searcher.embedding_provider,
            lease=worker_lease,
        )
        worker.enqueue_pending_reviews(memory, memory_classifier_factory, semantic_extractor, procedure_matcher)
        worker.enqueue_pending_embedding_backfills(memory)
    else:
        worker = QueueOnlyMemoryWorker()
        print("Memory background worker already active in another process; this process will only enqueue review jobs.")
    runtime = AgentRuntime(
        messages=messages,
        guard=guard,
        memory=memory,
        memory_searcher=memory_searcher,
        skill_matcher=skill_matcher,
        memory_worker=worker,
        memory_classifier_factory=memory_classifier_factory,
        semantic_extractor=semantic_extractor,
        procedure_matcher=procedure_matcher,
        run_agent=run_agent,
        run_skill=run_skill,
        async_memory_review=async_memory_review,
    )
    print("Mini agent ready. Type 'exit' to quit.")

    try:
        while True:
            user_input = read_user_input()
            if user_input.lower() in ("exit", "quit"):
                break

            try:
                result = run_agent_turn(user_input, runtime)
            except ValueError as e:
                print(f"\nConfiguration error: {e}")
                break
            except AuthenticationError as e:
                print(f"\n{format_authentication_error(e)}")
                break
            except APIStatusError as e:
                print(f"\n{format_api_status_error(e)}")
                if e.status_code in (401, 403):
                    break
                continue
            except APITimeoutError as e:
                print(f"\n{format_api_connection_error(e)}")
                continue
            except APIConnectionError as e:
                print(f"\n{format_api_connection_error(e)}")
                continue
            if result.guard_notice:
                print(f"\n{result.guard_notice}")
            print(f"\nMiniAgent: {result.reply}")
    finally:
        if drain_memory_on_exit:
            worker.join()
        worker.stop()
        scheduler_worker.stop()

if __name__ == "__main__":
    main()
    
