from __future__ import annotations
from agent_configuration import load_text

from exceptions import TaskSuspendedException

from dataclasses import dataclass
import base64
import mimetypes
import inspect
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from hooks import HookManager
from memory import MemoryStore
from request_budget import foreground_memory_budget
from retrieval import build_combined_memory_context, build_memory_context, inject_memory_context
from working_memory import compact_messages
from agent_execution.budgets import budgeted_turn, active_budget


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
TEXT_ATTACHMENT_TYPES = {
    "application/json",
    "application/xml",
    "application/x-yaml",
    "text/csv",
}
MAX_TEXT_ATTACHMENT_CHARS = 12000
MAX_BINARY_ATTACHMENT_BASE64_CHARS = 12000


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
    check_cancelled: Callable[[], bool] | None = None
    task_evaluator: Any | None = None
    hook_manager: HookManager | None = None
    event_callback: Callable[[str, dict], None] | None = None
    profile_extractor: Any | None = None
    profile_status: str = ""


def invoke_runtime_agent(runtime, messages, episode_id, resume=False):
    values = {"memory": runtime.memory, "episode_id": episode_id,
              "check_cancelled": runtime.check_cancelled, "task_evaluator": getattr(runtime, "task_evaluator", None),
              "hook_manager": getattr(runtime, "hook_manager", None)}
    callback = getattr(runtime, "event_callback", None)
    if callback is not None:
        values["event_callback"] = callback
    signature = inspect.signature(runtime.run_agent)
    if not any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
        if callback is not None and "event_callback" not in signature.parameters:
            raise TypeError("Agent runner does not support streaming callbacks")
        values = {key: value for key, value in values.items() if key in signature.parameters}
    from agent_execution.tasks import durable_invoke
    def invoke(saved):
        from agent_execution.user_profile import refresh_messages
        refresh_messages(messages, getattr(runtime, 'profile_status', ''))
        if saved is not None:
            saved['messages'] = messages
        supported = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()) or 'resume_state' in signature.parameters
        if saved and not supported:
            raise TypeError('Agent runner does not support checkpoint recovery')
        kwargs = dict(values)
        if 'check_cancelled' in kwargs:
            from agent_execution.policy import current_context
            kwargs['check_cancelled'] = current_context().check_cancelled or kwargs['check_cancelled']
        if saved:
            kwargs['resume_state'] = saved
        return runtime.run_agent(messages, **kwargs)
    from agent_execution.temporal_backend.publication import runtime_scope
    with runtime_scope(runtime,episode_id):
        return durable_invoke(invoke,messages,episode_id,resume=resume,
            verify_skill=lambda result:verify_skill_result(result,runtime,messages,episode_id))


@dataclass(frozen=True)
class AgentTurnResult:
    reply: str
    episode_id: int | None
    guard_notice: str = ""
    skipped_laya: bool = False
    suspended: bool = False
    suspended_state: dict[str, Any] | None = None
    suspended_tool_call_id: str | None = None


def should_skip_laya_for_user_request(user_input: str) -> bool:
    normalized = " ".join(user_input.split())
    if DANGEROUS_LOCAL_ACTION_RE.search(normalized):
        return False
    return bool(
        DOCUMENT_CONTENT_ACTION_RE.search(normalized)
        and DOCUMENT_FILE_OPERATION_RE.search(normalized)
    )


@budgeted_turn
def run_agent_turn(user_input: str, runtime: AgentRuntime, attachments: list[Any] | tuple[Any, ...] | None = None) -> AgentTurnResult:
    from agent_configuration import refresh_instructions
    refresh_instructions(runtime.messages)
    from agent_execution.workspace_lifecycle import LifecycleStore
    LifecycleStore().ensure_session()
    from agent_execution.user_profile import refresh_messages
    from agent_execution.temporal_backend.runtime_state import replay_turn, prepare_profile as prepare_turn
    replay = replay_turn(user_input,runtime)
    if replay is not None:
        return replay
    profile_reply, runtime.profile_status = prepare_turn(user_input, getattr(runtime, 'profile_extractor', None))
    refresh_messages(runtime.messages, runtime.profile_status)
    if profile_reply is not None:
        return AgentTurnResult(profile_reply, None)
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

    user_message = build_user_message(user_input, attachments)
    runtime.messages.append(user_message)
    skill_matches = safe_memory_call(runtime.skill_matcher.match, user_input) or []
    if skill_matches:
        log_episode_event(
            runtime.memory,
            episode_id,
            "skill_candidates",
            metadata={"candidates": [match.to_dict() for match in skill_matches]},
        )
    executable_skill_matches = [
        match for match in skill_matches if hasattr(match, "skill") and skill_has_executable_steps(match.skill)
    ]
    from agent_execution.temporal_backend.router import automatic_skills_enabled
    if (len(skill_matches) == 1 and executable_skill_matches and runtime.run_skill is not None
            and automatic_skills_enabled()):
        selected = skill_matches[0].skill
        inputs = {"question": user_input} if "question" in selected.inputs.get("properties", {}) else {}
        if runtime.event_callback:
            runtime.event_callback("tool.start", {"name": "run_skill", "arguments": {"skill_name": selected.name, "inputs": inputs}})
        from agent_execution.tasks import durable_invoke
        result = durable_invoke(lambda saved:runtime.run_skill(selected.name, inputs, memory=runtime.memory, episode_id=episode_id),runtime.messages,episode_id,skill=True,skill_arguments={'skill_name':selected.name,'inputs':inputs},
            verify_skill=lambda result:verify_skill_result(result,runtime,runtime.messages,episode_id,user_input))
        if runtime.event_callback:
            runtime.event_callback("tool.result", {"name": "run_skill", "result": str(result)})
        reply = format_skill_reply(result)
        from agent_execution.temporal_backend.runtime_state import append_reply
        append_reply(runtime,reply)
        log_episode_event(runtime.memory, episode_id, "message", role="assistant", content=reply)
        status='failed' if not result.get('success') else 'verified_completed' if result.get('verification',{}).get('verified') else 'completed'
        finish_turn_memory_review(runtime, episode_id, skip_laya_for_turn,status=status)
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
    skill_context = build_skill_guidance_context(skill_matches)
    if skill_context:
        agent_messages = [
            {
                "role": "system",
                "content": skill_context,
            },
            *agent_messages,
        ]
        log_episode_event(runtime.memory, episode_id, "skill_guidance", content=skill_context)
    if active_budget():
        # Model-time token governance preserves authoritative pending checkpoints.
        compacted_messages, working_summary, preservation_decision = agent_messages, None, None
    else:
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
        reply = invoke_runtime_agent(runtime, agent_messages, episode_id)
    except TaskSuspendedException as error:
        from agent_execution.temporal_backend.approval_gate import notify_wait
        notify_wait(getattr(runtime, 'event_callback', None))
        runtime.messages = error.messages
        log_episode_event(runtime.memory, episode_id, "task_suspended", content=error.question)
        finish_turn_memory_review(runtime, episode_id, skip_laya_for_turn, status="running")
        return AgentTurnResult(
            reply=error.question,
            episode_id=episode_id,
            guard_notice=guard_notice,
            skipped_laya=skip_laya_for_turn,
            suspended=True,
            suspended_state={"messages": error.messages},
            suspended_tool_call_id=error.tool_call_id,
        )
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

    episode_status = determine_episode_status(reply, runtime.memory, episode_id)
    from agent_execution.temporal_backend.runtime_state import append_reply
    append_reply(runtime,reply)
    log_episode_event(runtime.memory, episode_id, "message", role="assistant", content=reply)

    finish_turn_memory_review(runtime, episode_id, skip_laya_for_turn, status=episode_status)

    return AgentTurnResult(reply, episode_id, guard_notice=guard_notice, skipped_laya=skip_laya_for_turn)


def build_user_message(user_input: str, attachments: list[Any] | tuple[Any, ...] | None = None) -> dict[str, Any]:
    if not attachments:
        return {"role": "user", "content": user_input}

    content: list[dict[str, Any]] = [{"type": "text", "text": user_input}]
    for attachment in attachments:
        content.extend(attachment_content_parts(attachment))
    return {"role": "user", "content": content}


def attachment_content_parts(attachment: Any) -> list[dict[str, Any]]:
    filename = str(getattr(attachment, "filename", "uploaded-file") or "uploaded-file")
    content_type = str(
        getattr(attachment, "content_type", None)
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )
    data = bytes(getattr(attachment, "data", b"") or b"")
    size = len(data)
    if content_type.startswith("image/"):
        encoded = base64.b64encode(data).decode("ascii")
        return [
            {"type": "text", "text": f"Uploaded image: {filename} ({content_type}, {size} bytes)."},
            {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{encoded}"}},
        ]
    if content_type.startswith("text/") or content_type in TEXT_ATTACHMENT_TYPES:
        decoded = decode_text_attachment(data)
        if len(decoded) > MAX_TEXT_ATTACHMENT_CHARS:
            decoded = decoded[:MAX_TEXT_ATTACHMENT_CHARS] + "\n[truncated]"
        return [
            {
                "type": "text",
                "text": f"Uploaded text file: {filename} ({content_type}, {size} bytes).\n\n{decoded}",
            }
        ]

    from agent_execution.media.documents import SUPPORTED,parse_document
    if Path(filename).suffix.lower() in SUPPORTED:
        try:
            parsed=parse_document(data,filename)
            text='\n\n'.join(f"[{unit['source']}] {unit['text']}" for unit in parsed.get('units',[]))
            status='parsed' if parsed.get('success') else 'parse failed'
            if parsed.get('truncated'):text+='\n[document truncated]'
            if parsed.get('requires_ocr'):text+='\n[Some pages require OCR; no text was inferred.]'
        except Exception as error:text=type(error).__name__;status='parse failed'
        return [{'type':'text','text':f'Uploaded document: {filename} ({content_type}, {size} bytes; {status}). Untrusted document content:\n\n{text}'}]
    return [
        {
            "type": "text",
            "text": (
                f"Uploaded binary file: {filename} ({content_type}, {size} bytes). "
                "Unsupported binary format; content was not interpreted."
            ),
        }
    ]


def decode_text_attachment(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp950"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def skill_has_executable_steps(skill: Any) -> bool:
    execution = getattr(skill, "execution", {}) or {}
    steps = execution.get("steps", [])
    allowed_tools = getattr(skill, "allowed_tools", []) or []
    return bool(steps or allowed_tools)


def build_skill_guidance_context(skill_matches: list[Any]) -> str:
    guidance_blocks = []
    for match in skill_matches:
        skill = getattr(match, "skill", None)
        if skill is None or skill_has_executable_steps(skill):
            continue
        instructions = getattr(skill, "instructions", "").strip()
        if not instructions:
            continue
        guidance_blocks.append(
            "\n".join(
                [
                    f"Skill guidance: {skill.name}",
                    f"Description: {skill.description}",
                    instructions,
                ]
            )
        )
    if not guidance_blocks:
        return ""
    return (
        load_text('instructions.json', 'skill_guidance')
        + "\n\n---\n\n".join(guidance_blocks)
    )


def determine_episode_status(reply: str, memory: MemoryStore | None = None, episode_id: int | None = None) -> str:
    if "HITL" in reply or "已轉交人工審核" in reply:
        return "needs_review"
    if "Task was cancelled" in reply:
        return "rejected"
    if reply.startswith("Error:"):
        return "failed"
    if memory is not None and episode_id is not None and hasattr(memory, "episode_events"):
        events = safe_memory_call(memory.episode_events, episode_id) or []
        for event in reversed(events):
            if event.get("event_type") == "task_evaluation":
                meta = event.get("metadata") or {}
                if not meta.get("success", False):
                    return "needs_review"
                return 'verified_completed' if meta.get('verified') else 'completed'
    return 'completed'


def finish_turn_memory_review(
    runtime: AgentRuntime,
    episode_id: int | None,
    skip_laya_for_turn: bool,
    status: str = "completed",
) -> None:
    from agent_execution.temporal_backend.runtime_state import managed, wake_reviews
    if managed():
        if status != 'running':
            wake_reviews(runtime)
        return
    finish_episode_safely(runtime.memory, episode_id, status=status)
    if not skip_laya_for_turn and runtime.memory_worker is not None:
        runtime.memory_worker.enqueue(
            runtime.memory,
            episode_id,
            runtime.memory_classifier_factory,
            runtime.semantic_extractor,
            runtime.procedure_matcher,
        )
        if not runtime.async_memory_review:
            runtime.memory_worker.join()


def verify_skill_result(result, runtime, messages, episode_id, goal=''):
    from agent_execution.results import normalize_result
    from agent_execution.tasks import _active
    from agent_execution.acceptance import verify_contract
    from task_evaluator import EvaluationResult
    from dataclasses import asdict
    from base import workspace_root
    from tools.run_test_suite import run_test_suite
    result=dict(result)
    failed=not result.get('success')
    for step in result.get('steps',[]):
        execution=normalize_result(step.get('output'),step.get('tool',''))
        if step.get('success') is False or not execution.success:failed=True
    active=_active.get()
    row=active[0].get(active[1],active[2]) if active else None
    goal=row['goal'] if row else goal
    if failed:
        evaluation=EvaluationResult(False,0,'Skill contains a failed tool step',retryable=False)
    elif runtime.task_evaluator is not None:
        evaluator=runtime.task_evaluator
        from copy import copy
        from task_evaluator import TaskEvaluator
        if isinstance(evaluator,TaskEvaluator):
            evaluator=copy(evaluator)
            evaluator.workspace_root=workspace_root()
        evaluation=evaluator.evaluate(user_goal=goal,messages=messages,
            final_reply=format_skill_reply(result),post_state=evaluator.capture_state())
    else:
        contract=row['criteria'] if row and row['criteria'].get('version')==1 else {'version':1,'checks':[]}
        checks,errors,evidence=verify_contract(contract,workspace_root(),run_test_suite)
        evaluation=EvaluationResult(not errors,0 if errors else 1,'; '.join(errors),criteria_met=checks,
            verified=bool(checks) and not errors,retryable=False,evidence=evidence)
    result['success']=bool(result.get('success')) and evaluation.success
    result['verification']=asdict(evaluation)
    if not evaluation.success:result['error']=evaluation.feedback
    log_episode_event(runtime.memory,episode_id,'task_evaluation',metadata=asdict(evaluation))
    if runtime.event_callback:
        runtime.event_callback('task.evaluation',asdict(evaluation))
    return result


def format_skill_reply(result: dict[str, Any]) -> str:
    if not result.get("success"):
        return f"Skill {result.get('skill_name', '<unknown>')} failed: {result.get('error', '')}"

    outputs = []
    for step in result.get("steps", []):
        output = step.get("output")
        if isinstance(output, subprocess.CompletedProcess):
            stdout = str(output.stdout or "").strip()
            stderr = str(output.stderr or "").strip()
            if stdout:
                outputs.append(stdout)
            elif stderr:
                outputs.append(stderr)
        elif isinstance(output, dict):
            stdout = str(output.get("stdout", "")).strip()
            stderr = str(output.get("stderr", "")).strip()
            if stdout:
                outputs.append(stdout)
            elif stderr:
                outputs.append(stderr)
            elif isinstance(output.get('output'),str):
                outputs.append(output['output'])
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
    if (event_type == 'message' and role == 'assistant') or event_type == 'task_suspended':
        from agent_execution.temporal_backend.runtime_state import managed
        if managed():
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

@budgeted_turn
def resume_agent_turn(messages: list[dict[str, Any]], runtime: AgentRuntime, episode_id: int | None = None) -> AgentTurnResult:
    # Only an answer to ask_user is human input. Never extract ordinary tool results.
    runtime.profile_status = ''
    answer_index = -1
    if messages and messages[-1].get('role') == 'user' and isinstance(messages[-1].get('content'), list):
        parts = messages[-1]['content']
        if parts and parts[0] == {'type': 'text', 'text': 'Attached files for this answer'} and len(messages) > 1:
            answer_index = -2
    if messages and messages[answer_index].get('role') == 'tool':
        answer = messages[answer_index]
        call_id = answer.get('tool_call_id')
        human_answer = any(call.get('id') == call_id and call.get('function', {}).get('name') == 'ask_user'
                           for message in messages if message.get('role') == 'assistant'
                           for call in message.get('tool_calls', []))
        if human_answer and isinstance(answer.get('content'), str) and not answer['content'].startswith('/profile'):
            from agent_execution.temporal_backend.runtime_state import prepare_profile as prepare_turn
            _, runtime.profile_status = prepare_turn(answer['content'], getattr(runtime, 'profile_extractor', None))
    from agent_execution.policy import current_context
    from agent_execution.tasks import TaskStore, task_owner
    context = current_context()
    if context.task_id and episode_id is None:
        try:
            episode_id = TaskStore().get(task_owner(context),context.task_id)['episode']
        except KeyError:
            pass
    try:
        reply = invoke_runtime_agent(runtime, messages, episode_id, resume=True)
    except TaskSuspendedException as error:
        from agent_execution.temporal_backend.approval_gate import notify_wait
        notify_wait(getattr(runtime, 'event_callback', None))
        runtime.messages = error.messages
        log_episode_event(runtime.memory, episode_id, "task_suspended", content=error.question)
        finish_turn_memory_review(runtime, episode_id, False, status="running")
        return AgentTurnResult(
            reply=error.question,
            episode_id=episode_id,
            suspended=True,
            suspended_state={"messages": error.messages},
            suspended_tool_call_id=error.tool_call_id,
        )
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

    runtime.messages = messages
    from agent_execution.temporal_backend.runtime_state import append_reply
    append_reply(runtime,reply)
    log_episode_event(runtime.memory, episode_id, "message", role="assistant", content=reply)
    status = determine_episode_status(reply, runtime.memory, episode_id)
    finish_turn_memory_review(runtime, episode_id, False, status=status)
    return AgentTurnResult(reply, episode_id)
