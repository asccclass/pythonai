from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable

from memory import MemoryStore
from request_budget import foreground_memory_budget
from retrieval import build_combined_memory_context, build_memory_context, inject_memory_context
from working_memory import compact_messages


DOCUMENT_CONTENT_ACTION_RE = re.compile(
    r"(翻譯|翻译|摘要|總結|总结|summari[sz]e|translate|轉成|转换|convert)",
    re.IGNORECASE,
)
DOCUMENT_FILE_OPERATION_RE = re.compile(
    r"(讀取|读取|寫入|写入|存成|保存|read|write|save).{0,80}(\.md|\.txt|\.json|\.csv|\.html|\.docx?|\.pdf|\bfile\b)",
    re.IGNORECASE,
)
DANGEROUS_LOCAL_ACTION_RE = re.compile(
    r"(刪除|删除|delete|remove|rm\s+-|執行|执行|run\s+command|shell|powershell|cmd\.exe)",
    re.IGNORECASE,
)


@dataclass
class AgentRuntime:
    messages: list[dict[str, Any]]
    guard: Any
    memory: MemoryStore | None
    memory_searcher: Any
    skill_matcher: Any
    memory_worker: Any | None
    memory_classifier_factory: Any
    semantic_extractor: Any
    procedure_matcher: Any
    run_agent: Callable[..., str]
    run_skill: Callable[..., dict[str, Any]] | None = None
    async_memory_review: bool = True


@dataclass(frozen=True)
class AgentTurnResult:
    reply: str
    episode_id: int | None
    guard_notice: str = ""
    skipped_laya: bool = False


def should_skip_laya_for_user_request(user_input: str) -> bool:
    normalized = " ".join(user_input.split())
    if DANGEROUS_LOCAL_ACTION_RE.search(normalized):
        return False
    return bool(
        DOCUMENT_CONTENT_ACTION_RE.search(normalized)
        and DOCUMENT_FILE_OPERATION_RE.search(normalized)
    )


def run_agent_turn(user_input: str, runtime: AgentRuntime) -> AgentTurnResult:
    turn_budget = foreground_memory_budget()
    episode_id = safe_memory_call(runtime.memory.start_episode) if runtime.memory is not None else None
    log_episode_event(runtime.memory, episode_id, "message", role="user", content=user_input)

    skip_laya_for_turn = should_skip_laya_for_user_request(user_input)
    guard_notice = ""
    if skip_laya_for_turn:
        log_episode_event(
            runtime.memory,
            episode_id,
            "guard_decision",
            metadata={
                "skipped": True,
                "reason": "document_content_transform",
            },
        )
    else:
        guard_decision = runtime.guard.assess(user_input)
        guard_notice = format_guard_notice(guard_decision)
        log_episode_event(
            runtime.memory,
            episode_id,
            "guard_decision",
            metadata={"guard": guard_decision},
        )

    runtime.messages.append({"role": "user", "content": user_input})
    skill_matches = safe_memory_call(runtime.skill_matcher.match, user_input) or []
    if skill_matches:
        log_episode_event(
            runtime.memory,
            episode_id,
            "skill_candidates",
            metadata={"candidates": [match.to_dict() for match in skill_matches]},
        )
    if len(skill_matches) == 1 and runtime.run_skill is not None and hasattr(skill_matches[0], "skill"):
        selected = skill_matches[0].skill
        inputs = {"question": user_input} if "question" in selected.inputs.get("properties", {}) else {}
        result = runtime.run_skill(selected.name, inputs, memory=runtime.memory, episode_id=episode_id)
        reply = format_skill_reply(result)
        runtime.messages.append({"role": "assistant", "content": reply})
        log_episode_event(runtime.memory, episode_id, "message", role="assistant", content=reply)
        finish_turn_memory_review(runtime, episode_id, skip_laya_for_turn)
        return AgentTurnResult(reply, episode_id, guard_notice=guard_notice, skipped_laya=skip_laya_for_turn)

    memory_context = (
        safe_memory_call(
            build_memory_context,
            runtime.memory,
            query=user_input,
            vector_searcher=runtime.memory_searcher,
            allow_query_embedding=turn_budget.try_acquire("memory_query_embedding"),
            max_missing_embeddings=turn_budget.remaining("memory_embedding_backfill") or 0,
        )
        if runtime.memory is not None
        else ""
    )
    if runtime.memory is not None:
        memory_context = safe_memory_call(
            build_combined_memory_context,
            runtime.memory,
            query=user_input,
            semantic_context=memory_context,
        ) or memory_context
    log_retrieval_stats(runtime.memory, episode_id, runtime.memory_searcher)
    log_memory_budget(runtime.memory, episode_id, "foreground_retrieval", turn_budget)
    if memory_context:
        log_episode_event(runtime.memory, episode_id, "retrieval_context", content=memory_context)

    agent_messages = inject_memory_context(runtime.messages, memory_context)
    compacted_messages, working_summary, preservation_decision = compact_messages(
        agent_messages,
        preservation_classifier=None if skip_laya_for_turn else getattr(runtime.guard, "_agent", None),
    )
    if working_summary is not None:
        agent_messages = compacted_messages
        log_episode_event(runtime.memory, episode_id, "working_memory_summary", content=working_summary)
        if preservation_decision is not None and preservation_decision.should_preserve:
            log_episode_event(
                runtime.memory,
                episode_id,
                "working_memory_preservation_candidate",
                metadata={"preservation": preservation_decision},
            )

    try:
        reply = runtime.run_agent(agent_messages, memory=runtime.memory, episode_id=episode_id)
    except Exception as error:
        log_episode_event(
            runtime.memory,
            episode_id,
            "error",
            content=str(error),
            metadata={"error_type": type(error).__name__},
        )
        finish_episode_safely(runtime.memory, episode_id, status="failed")
        raise

    runtime.messages.append({"role": "assistant", "content": reply})
    log_episode_event(runtime.memory, episode_id, "message", role="assistant", content=reply)

    finish_turn_memory_review(runtime, episode_id, skip_laya_for_turn)

    return AgentTurnResult(reply, episode_id, guard_notice=guard_notice, skipped_laya=skip_laya_for_turn)


def finish_turn_memory_review(
    runtime: AgentRuntime,
    episode_id: int | None,
    skip_laya_for_turn: bool,
) -> None:
    if skip_laya_for_turn:
        finish_episode_safely(runtime.memory, episode_id)
    elif runtime.memory_worker is not None:
        runtime.memory_worker.enqueue(
            runtime.memory,
            episode_id,
            runtime.memory_classifier_factory,
            runtime.semantic_extractor,
            runtime.procedure_matcher,
        )
        if not runtime.async_memory_review:
            runtime.memory_worker.join()


def format_skill_reply(result: dict[str, Any]) -> str:
    if not result.get("success"):
        return f"Skill {result.get('skill_name', '<unknown>')} failed: {result.get('error', '')}"

    outputs = []
    for step in result.get("steps", []):
        output = step.get("output")
        if isinstance(output, dict):
            stdout = str(output.get("stdout", "")).strip()
            stderr = str(output.get("stderr", "")).strip()
            if stdout:
                outputs.append(stdout)
            elif stderr:
                outputs.append(stderr)
        elif output not in (None, ""):
            outputs.append(str(output))
    return "\n\n".join(outputs).strip() or f"Skill {result.get('skill_name', '<unknown>')} completed."


def format_guard_notice(decision: Any) -> str:
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


def log_memory_budget(
    memory: MemoryStore | None,
    episode_id: int | None,
    phase: str,
    budget: Any,
) -> None:
    snapshot = budget.snapshot() if hasattr(budget, "snapshot") else {}
    log_episode_event(memory, episode_id, "memory_budget", metadata={"phase": phase, "budget": snapshot})


def log_retrieval_stats(
    memory: MemoryStore | None,
    episode_id: int | None,
    searcher: Any,
) -> None:
    stats = getattr(searcher, "last_stats", None)
    if not stats:
        return
    log_episode_event(memory, episode_id, "memory_retrieval_stats", metadata={"stats": stats})
