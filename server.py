"""Small HTTP bridge for chatting with the ASCS Ollama endpoint."""

from __future__ import annotations

import os
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from exceptions import TaskSuspendedException

from openai import APIConnectionError, APIStatusError, APITimeoutError, AuthenticationError
from openai import OpenAI
from agent_execution.budgets import budget_client, active_budget, BudgetExhausted

from agent_runtime import AgentRuntime, format_guard_notice, run_agent_turn, should_skip_laya_for_user_request
from base import TOOLS_SCHEMAS, load_dotenv, run_skill, run_tool_with_context
from hooks import HookContext, HookDecision, HookLifecycle, HookManager, default_hook_manager
from laya_guard import GuardDecision, LayaGuard
from memory_classifier import LayaMemoryClassifier, MemoryCandidateDecision
from procedure_similarity import LLMProcedureSimilarityMatcher, LexicalProcedureSimilarityMatcher, ProcedureCandidate
from semantic_extractor import LLMSemanticExtractor
from memory import MemoryStore
from memory_worker import BackgroundWorkerLease, MemoryReviewWorker, QueueOnlyMemoryWorker
from scheduler import SchedulerWorker
from skills import SkillMatcher, SkillRegistry
from vector_search import OpenAICompatibleEmbeddingProvider, VectorMemorySearcher
from task_evaluator import EvaluationResult, TaskEvaluator


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
        max_retries=0,
        timeout=60,
    )


client: OpenAI | None = None


def get_client() -> OpenAI:
    global client
    if client is None:
        client = create_ollama_client()
    return budget_client(client)


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


SYSTEM_PROMPT = """You are an autonomous AI assistant integrated into a local environment. You have access to tools that allow you to read, write, and execute commands on the user's local computer. ALWAYS use these tools when asked to interact with files or the system. NEVER refuse by claiming you are in a cloud environment.

### ReAct & Planning Guidelines (CRITICAL):
1. **Thought & Plan**: Before taking action or calling tools, always think step-by-step about what needs to be done. For complex tasks, formulate a clear plan.
2. **Execute**: Call the necessary tools based on your plan.
3. **Observe & Reflect**: After tools return results, observe them. If a tool fails or returns an error, REFLECT on why it failed and TRY an alternative approach. Do NOT blindly repeat the same failed tool call.
4. **Iterate**: Continue thinking, executing, and observing until the goal is fully achieved.
5. **Final Output**: Once the task is complete, summarize the final results clearly to the user.
6. **Progress**: Reuse successful reads. If a tool fails, correct the cause or change tools instead of repeating it. Batch directory creation with create_directory(paths=[...]); write_file creates parent directories. For a knowledge-base/documentation structure, create the requested directories and documents directly rather than introducing unrelated executable scaffolds. A GitHub repository fetch provides its README; fetch specific source files only when needed to support the requested explanation.
"""
AGENTS_INSTRUCTIONS_PATH = Path("AGENTS.md")


def load_agents_instructions(path: str | Path = AGENTS_INSTRUCTIONS_PATH) -> str:
    agents_path = Path(path)
    if not agents_path.exists():
        return ""
    content = agents_path.read_text(encoding="utf-8").strip()
    return content


def build_system_prompt(base_prompt: str = SYSTEM_PROMPT, agents_path: str | Path = AGENTS_INSTRUCTIONS_PATH) -> str:
    agents_instructions = load_agents_instructions(agents_path)
    parts = [base_prompt.strip()] if base_prompt.strip() else []
    if agents_instructions:
        parts.append(f"AGENTS.md instructions:\n{agents_instructions}")
    from agent_execution.turn_control.environment import environment_context
    parts.append(environment_context())

    # Identity-specific profile is refreshed at execution time, never in a shared template.
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
    max_iterations: int | None = None,
    check_cancelled: Callable[[], bool] | None = None,
    task_evaluator: TaskEvaluator | None = None,
    max_eval_retries: int = 2,
    hook_manager: HookManager | None = None,
    event_callback: Callable[[str, dict], None] | None = None,
    resume_state: dict | None = None,
):
    from agent_execution.tasks import save_checkpoint
    from agent_execution.results import normalize_result
    from agent_execution.turn_control.steps import model_step, tool_step, evaluation_step
    from openai.types.chat import ChatCompletionMessage
    if isinstance(task_evaluator,TaskEvaluator):
        from copy import copy
        from base import workspace_root
        task_evaluator=copy(task_evaluator)
        task_evaluator.workspace_root=workspace_root()
    if hook_manager is None:hook_manager=default_hook_manager()
    resume_state = resume_state or {}
    from agent_execution.turn_control.environment import refresh_environment
    inserted = refresh_environment(messages)
    if inserted and resume_state.get('evaluation_start') is not None:
        resume_state = {**resume_state, 'evaluation_start': resume_state['evaluation_start'] + 1}
    from agent_execution.turn_control import TurnProgress, iteration_limit
    max_iterations = iteration_limit(max_iterations, resume_state.get('iteration_limit'))
    progress = TurnProgress(resume_state.get('progress'))
    evaluation_start = resume_state.get('evaluation_start')
    if evaluation_start is None:
        evaluation_start = next((index for index in range(len(messages)-1, -1, -1)
            if messages[index].get('role') == 'user' and not
            str(messages[index].get('content') or '').startswith('【任務驗收未通過')), 0)
    tool_results = dict(resume_state.get('tool_results',{}))
    iteration = resume_state.get('iteration', 0)
    if resume_state.get('phase') == 'model_pending':
        iteration = max(0,iteration-1)
    eval_retries = resume_state.get('eval_retries', 0)
    pre_state = resume_state.get('pre_state')
    if pre_state is None and task_evaluator is not None:
        pre_state = task_evaluator.capture_state()
    def checkpoint(phase, **metadata):
        save_checkpoint(phase,messages,iteration=iteration,iteration_limit=max_iterations,eval_retries=eval_retries,pre_state=pre_state,tool_results=tool_results,evaluation_start=evaluation_start,progress=progress.state,**metadata)
    def pending_assistant():
        for index in range(len(messages)-1,-1,-1):
            message = messages[index]
            if message.get('role') == 'assistant':
                calls = message.get('tool_calls') or []
                finished = {m.get('tool_call_id') for m in messages[index+1:] if m.get('role')=='tool'}
                if any(call['id'] not in finished for call in calls):
                    return ChatCompletionMessage(**message), finished
                break
        return None, set()

    while iteration < max_iterations:
        if check_cancelled and check_cancelled():
            return "Task was cancelled by the user."
            
        assistant_message, resolved_calls = pending_assistant() if resume_state else (None,set())
        if resume_state.get('phase') in {'evaluation_pending','evaluation_done'} or (resume_state.get('phase')=='model_received' and not messages[-1].get('tool_calls')):
            assistant_message = ChatCompletionMessage(**next(m for m in reversed(messages) if m.get('role')=='assistant'))
        if assistant_message is None:
            iteration += 1
            concluding = iteration >= max_iterations or progress.state['blocked'] >= 2
            if concluding:
                messages.append({'role':'user','content':'Execution budget / repeated-operation limit reached. Conclude now using existing evidence. Do not call tools. Explain completed work, unverified work, actual errors and next steps; never claim unfinished work is complete.'})
            elif iteration == max_iterations - 2:
                messages.append({'role':'user','content':'Only two execution rounds and a final answer remain. Batch independent work, run required checks, and prepare an evidence-based final response.'})
            from agent_execution.policy import current_context
            allowed=current_context().allowed_tools
            from permissions import check_tool_permission
            schemas=[schema for schema in TOOLS_SCHEMAS
                     if check_tool_permission(schema.get('function',{}).get('name'))
                     and (allowed is None or schema.get('function',{}).get('name') in allowed)]
            request={'model':OLLAMA_MODEL,'tools':schemas}
            if hook_manager is not None:
                hook_context = HookContext(
                    lifecycle=HookLifecycle.PRE_MODEL_CALL,
                    messages=messages,
                    model_request=request,
                    memory=memory,
                    episode_id=episode_id,
                    metadata={"iteration": iteration},
                )
                hook_result = hook_manager.dispatch(HookLifecycle.PRE_MODEL_CALL, hook_context)
                request=hook_context.model_request
                if hook_result.decision in {HookDecision.RETURN_EARLY, HookDecision.BLOCK}:
                    if hook_result.decision==HookDecision.BLOCK:return 'Error: '+hook_result.message
                    return str(hook_result.return_value if hook_result.return_value is not None else hook_result.message)
            if concluding:
                request['tools'] = []
            checkpoint('model_pending')
            attempts = 0
            while True:
                try:
                    assistant_message = model_step(
                        get_client(), request, messages, iteration=iteration, attempt=attempts+1,
                        event_callback=event_callback, check_cancelled=check_cancelled)
                    break
                except BudgetExhausted as error:
                    if event_callback:
                        event_callback('error',{'code':'budget_exhausted','message':str(error),'retryable':False})
                    return f'Error: {error}'
                except InterruptedError:
                    return "Task was cancelled by the user."
                except (APIStatusError, APIConnectionError, APITimeoutError) as error:
                    if check_cancelled and check_cancelled():
                        return "Task was cancelled by the user."
                    is_transient = True
                    status_code = getattr(error, 'status_code', None)
                    if isinstance(error, APIStatusError) and status_code not in TRANSIENT_STATUS_CODES:
                        is_transient = False
                    
                    if not is_transient or attempts >= max_retries:
                        err_msg = f"⚠️ 遠端 LLM 服務發生錯誤 (連線失敗或伺服器過載)。已達最大重試次數，請稍後再試。\n詳細錯誤: {error}"
                        print(err_msg)
                        if event_callback:
                            event_callback("error", {"code": "provider_error", "message": "Model provider request failed", "retryable": is_transient})
                            return "Error: Model provider request failed"
                        return err_msg
                    
                    attempts += 1
                    base_delay = retry_delay_seconds(error) if isinstance(error, APIStatusError) else 5.0
                    jitter = random.uniform(0.1, 1.0) if base_delay > 0 else 0.0
                    delay = base_delay * (2 ** (attempts - 1)) + jitter
                
                    status_str = f"HTTP {status_code}" if status_code else type(error).__name__
                    print(f"\nRemote service returned {status_str}; retrying in {delay:g} seconds.")
                    if (event_callback or active_budget()) and check_cancelled:
                        end = time.monotonic() + delay
                        while time.monotonic() < end:
                            if check_cancelled():
                                return "Task was cancelled by the user."
                            sleep(min(.1, max(0, end - time.monotonic())))
                    else:
                        sleep(delay)
            messages.append(assistant_message_to_dict(assistant_message))
            if hook_manager is not None:
                hook_context = HookContext(
                    lifecycle=HookLifecycle.POST_MODEL_CALL,
                    messages=messages,
                    model_response=assistant_message,
                    memory=memory,
                    episode_id=episode_id,
                    metadata={"iteration": iteration},
                )
                hook_result = hook_manager.dispatch(HookLifecycle.POST_MODEL_CALL, hook_context)
                if hook_result.decision in {HookDecision.RETURN_EARLY, HookDecision.BLOCK}:
                    if hook_result.decision==HookDecision.BLOCK:return 'Error: '+hook_result.message
                    return str(hook_result.return_value if hook_result.return_value is not None else hook_result.message)
                if hook_result.decision == HookDecision.APPEND_FEEDBACK:
                    messages.append({"role": "user", "content": hook_result.feedback})
                    continue

            checkpoint('model_received')
        if assistant_message.tool_calls:
            if iteration >= max_iterations or progress.state['blocked'] >= 2:
                return 'Error: Agent could not conclude within its execution budget. Repeated tool operations or remaining work require a new strategy. No additional tools were executed; inspect prior tool errors and saved progress before continuing.'
            suspend_request = None
            for tool_call in assistant_message.tool_calls:
                if tool_call.id in resolved_calls:
                    continue
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
                
                tool_name = tool_call.function.name
                tool_args = parse_tool_arguments(tool_call.function.arguments)
                if hook_manager is not None:
                    hook_context = HookContext(
                        lifecycle=HookLifecycle.PRE_TOOL_EXECUTE,
                        messages=messages,
                        tool_call=tool_call,
                        tool_name=tool_name,
                        tool_arguments=tool_args,
                        memory=memory,
                        episode_id=episode_id,
                        metadata={"iteration": iteration},
                    )
                    hook_result = hook_manager.dispatch(HookLifecycle.PRE_TOOL_EXECUTE, hook_context)
                    tool_name = hook_context.tool_name or tool_name
                    tool_args = hook_context.tool_arguments
                    if hook_result.decision in {HookDecision.BLOCK, HookDecision.RETURN_TOOL_ERROR}:
                        result = hook_result.tool_error or hook_result.message or f"Error: {tool_name} blocked by hook"
                        repeated = progress.before(tool_name, tool_args)
                        if repeated:
                            result = str(result) + '\n' + repeated
                        tool_results[tool_call.id]=normalize_result('Error: '+str(result),tool_name).to_dict()
                        feedback = progress.observe(tool_name, tool_args, normalize_result('Error: '+str(result), tool_name))
                        messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": str(result)})
                        if feedback:
                            messages[-1]['content'] += '\n' + feedback
                        resolved_calls.add(tool_call.id)
                        checkpoint('tool_done',step={'id':tool_call.id,'name':tool_name,'arguments':tool_args,
                            'state':'failed','result':json.dumps(tool_results[tool_call.id],ensure_ascii=False)})
                        log_episode_event(
                            memory,
                            episode_id,
                            "tool_result",
                            content=str(result),
                            metadata={"tool_call_id": tool_call.id, "blocked_by_hook": True},
                        )
                        continue
                    if hook_result.decision == HookDecision.RETURN_EARLY:
                        return str(hook_result.return_value if hook_result.return_value is not None else hook_result.message)
                    tool_call = mutable_tool_call(tool_call, tool_name, tool_args)

                from agent_execution import execution_scope
                if check_cancelled and check_cancelled():
                    return "Task was cancelled by the user."
                if event_callback:
                    event_callback("tool.start", {"tool_call_id": tool_call.id, "name": tool_name, "arguments": tool_args})
                if tool_name == 'ask_user':
                    question = tool_step(tool_call, invoke=run_tool_with_context,
                        memory=memory, episode_id=episode_id, check_cancelled=check_cancelled)
                    context=HookContext(HookLifecycle.POST_TOOL_EXECUTE,messages=messages,tool_call=tool_call,
                        tool_name=tool_name,tool_arguments=tool_args,tool_result=question,memory=memory,episode_id=episode_id,
                        metadata={'iteration':iteration})
                    result_hook=hook_manager.dispatch(HookLifecycle.POST_TOOL_EXECUTE,context)
                    question=context.tool_result
                    if result_hook.decision in {HookDecision.BLOCK,HookDecision.RETURN_TOOL_ERROR}:
                        question='Error: '+(result_hook.tool_error or result_hook.message)
                    elif result_hook.decision==HookDecision.RETURN_EARLY:
                        return str(result_hook.return_value if result_hook.return_value is not None else result_hook.message)
                    elif result_hook.decision==HookDecision.APPEND_FEEDBACK:
                        messages.append({'role':'user','content':result_hook.feedback})
                    tool_results[tool_call.id]=normalize_result(question,tool_name).to_dict()
                    if str(question).startswith('Error:'):
                        messages.append({'role':'tool','tool_call_id':tool_call.id,'content':str(question)})
                        resolved_calls.add(tool_call.id)
                        checkpoint('tool_done',step={'id':tool_call.id,'name':tool_name,'arguments':tool_args,
                            'state':'failed','result':json.dumps(tool_results[tool_call.id],ensure_ascii=False)})
                    elif suspend_request is None:
                        suspend_request = (tool_call.id,question)
                    continue
                step = {'id':tool_call.id,'name':tool_name,'arguments':tool_args,
                        'dependencies':sorted(resolved_calls),'state':'running'}
                checkpoint('tool_started',step=step)
                source_id = tool_call.id
                with execution_scope(check_cancelled=check_cancelled,tool_hook_managed=tool_name):
                    result = progress.before(tool_name, tool_args)
                    if result is None:
                        result = tool_step(tool_call, invoke=run_tool_with_context,
                            memory=memory, episode_id=episode_id, check_cancelled=check_cancelled)
                    else:
                        from agent_execution.results import ToolResult
                        previous_id = progress.state['calls'].get(progress.key(tool_name, tool_args), {}).get('source_id')
                        previous = tool_results.get(previous_id, {})
                        if tool_name in {'fetch_url', 'read_file', 'list_files'} and previous.get('success') is True:
                            source_id = previous_id
                            result = ToolResult(True, 'Reused earlier read; no new network/filesystem read was performed.\n' + str(previous.get('output', '')),
                                                evidence={'kind':'reuse_prior_read','source_tool_call':source_id})
                        else:
                            result = ToolResult(False, error=str(result), side_effects={'status':'none'}, evidence={'kind':'repeat_prevented'})
                raw_result=normalize_result(result,tool_name)
                progress_feedback = progress.observe(tool_name, tool_args, raw_result)
                progress.state['calls'][progress.key(tool_name, tool_args)]['source_id'] = source_id
                result=raw_result
                tool_results[tool_call.id]=raw_result.to_dict()
                save_checkpoint('tool_done',messages+[{'role':'tool','tool_call_id':tool_call.id,'content':str(result)}],
                    iteration=iteration,iteration_limit=max_iterations,eval_retries=eval_retries,pre_state=pre_state,tool_results=tool_results,evaluation_start=evaluation_start,progress=progress.state,
                    step={**step,'state':'completed' if raw_result.success else 'failed','result':json.dumps(raw_result.to_dict(),ensure_ascii=False)})
                if hook_manager is not None:
                    hook_context = HookContext(
                        lifecycle=HookLifecycle.POST_TOOL_EXECUTE,
                        messages=messages,
                        tool_call=tool_call,
                        tool_name=tool_name,
                        tool_arguments=tool_args,
                        tool_result=result,
                        memory=memory,
                        episode_id=episode_id,
                        metadata={"iteration": iteration},
                    )
                    hook_result = hook_manager.dispatch(HookLifecycle.POST_TOOL_EXECUTE, hook_context)
                    result = hook_context.tool_result
                    if hook_result.decision == HookDecision.APPEND_FEEDBACK:
                        messages.append({"role": "user", "content": hook_result.feedback})
                    elif hook_result.decision in {HookDecision.BLOCK, HookDecision.RETURN_TOOL_ERROR}:
                        result = hook_result.tool_error or hook_result.message or f"Error: {tool_name} rejected by hook"
                    elif hook_result.decision == HookDecision.RETURN_EARLY:
                        return str(hook_result.return_value if hook_result.return_value is not None else hook_result.message)
                structured=raw_result if result is raw_result else normalize_result(result,tool_name)
                if result is not raw_result:
                    from dataclasses import replace
                    structured=replace(raw_result,success=raw_result.success and structured.success,
                        output=str(result),error=structured.error if raw_result.success else raw_result.error)
                # Presentation hooks cannot erase an observed process failure.
                if not raw_result.success:structured=raw_result
                tool_results[tool_call.id]=structured.to_dict()
                if event_callback:
                    event_callback("tool.result", {"tool_call_id": tool_call.id, "name": tool_name, "result": str(result), 'execution':structured.to_dict()})
                    if "approval_required:" in str(result):
                        import re
                        match = re.search(r"approval_required: ([a-f0-9]{32})", str(result))
                        if match:
                            event_callback("approval.required", {"approval_id": match.group(1), "name": tool_name, "arguments": tool_args})
                log_episode_event(
                    memory,
                    episode_id,
                    "tool_result",
                    content=str(result),
                    metadata={"tool_call_id": tool_call.id,'execution':structured.to_dict()},
                )
                record_tool_procedure_result(memory, episode_id, matched_procedure, result)
                messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": str(result)})
                if progress_feedback:
                    messages[-1]['content'] += '\n' + progress_feedback
                resolved_calls.add(tool_call.id)
                checkpoint('tool_done',step={**step,'state':'completed' if structured.success else 'failed','result':json.dumps(structured.to_dict(),ensure_ascii=False)})

            if suspend_request:
                tool_call_id, question = suspend_request
                raise TaskSuspendedException(question, tool_call_id, messages)
            resume_state = {}
            continue

        if task_evaluator is not None:
            user_goal = ""
            for msg in messages[evaluation_start:]:
                if msg.get("role") == "user":
                    content = str(msg.get("content") or "")
                    if not content.startswith("【任務驗收未通過"):
                        user_goal = content
                        break
            from agent_execution.tasks import _active
            active=_active.get()
            if active:
                user_goal=active[0].get(active[1],active[2])['goal']

            if resume_state.get('phase') == 'evaluation_done':
                from task_evaluator import EvaluationResult
                eval_res = EvaluationResult(**resume_state['evaluation'])
            else:
                checkpoint('evaluation_pending')
                eval_res = evaluation_step(task_evaluator,
                    user_goal=user_goal,
                    messages=messages, evaluation_start=evaluation_start,
                    final_reply=assistant_message.content or "",
                    pre_state=pre_state,
                )

            from dataclasses import asdict
            checkpoint('evaluation_done',evaluation=asdict(eval_res))
            resume_state = {}

            log_episode_event(
                memory,
                episode_id,
                "task_evaluation",
                metadata={
                    "success": eval_res.success,
                    "score": eval_res.score,
                    "feedback": eval_res.feedback,
                    "criteria_met": eval_res.criteria_met,
                    "side_effects": eval_res.side_effects,
                    'verified':getattr(eval_res,'verified',False),
                    'evidence':getattr(eval_res,'evidence',[]),
                    "eval_retry": eval_retries,
                },
            )
            if event_callback:
                event_callback("task.evaluation", {"success": eval_res.success, "score": eval_res.score, "feedback": eval_res.feedback,'verified':getattr(eval_res,'verified',False)})

            if not eval_res.success:
                if eval_retries < max_eval_retries and getattr(eval_res,'retryable',True) and iteration < max_iterations and progress.state['blocked'] < 2:
                    eval_retries += 1
                    critic_feedback = (
                        f"【任務驗收未通過 (Critic Feedback - 第 {eval_retries}/{max_eval_retries} 次修正)】\n"
                        f"評估結果: 未達成目標 (得分: {eval_res.score})\n"
                        f"問題反饋:\n{eval_res.feedback}\n"
                        f"請針對原始任務目標與上述反饋修正，僅使用已授權工具；權限不足時請說明需要的設定。"
                    )
                    messages.append({"role": "user", "content": critic_feedback})
                    checkpoint('model_pending')
                    log_episode_event(
                        memory,
                        episode_id,
                        "critic_correction",
                        content=critic_feedback,
                        metadata={"eval_retry": eval_retries, "score": eval_res.score},
                    )
                    continue
                else:
                    log_episode_event(
                        memory,
                        episode_id,
                        "hitl_handover",
                        metadata={
                            "max_eval_retries": max_eval_retries,
                            "feedback": eval_res.feedback,
                        },
                    )
                    reason = ("已用完執行輪數或重複操作額度" if iteration >= max_iterations or progress.state['blocked'] >= 2 else f"已達最大修正次數 {max_eval_retries}" if eval_retries >= max_eval_retries
                              else "此失敗無法自動修正，已停止重試")
                    return (
                        f"⛔ 任務驗收未通過 ({reason})，已轉交人工審核 (HITL)。\n"
                        f"未通過原因:\n{eval_res.feedback}\n\n"
                        f"Agent 最終回覆:\n{assistant_message.content or ''}"
                    )

        return assistant_message.content or ""
        
    from agent_execution.operations.redaction import redact
    failed = [value for value in tool_results.values() if not value.get('success')]
    last_error = redact(str(failed[-1].get('error') or failed[-1].get('output') or ''))[:400] if failed else 'No failing tool result was recorded; inspect remaining work and response hooks.'
    return (f"Error: Agent execution limit ({max_iterations}) reached. Successful tool steps: {len(tool_results)-len(failed)}; failed: {len(failed)}. "
            f"Last tool error: {last_error}. Progress is saved; correct this cause or split the remaining work before resuming.")


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
    from agent_execution.results import normalize_result
    return normalize_result(result).success


def parse_tool_arguments(arguments: str) -> dict[str, Any]:
    try:
        parsed = json.loads(arguments or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def mutable_tool_call(tool_call: Any, tool_name: str, tool_arguments: dict[str, Any]) -> Any:
    return SimpleNamespace(
        id=tool_call.id,
        function=SimpleNamespace(
            name=tool_name,
            arguments=json.dumps(tool_arguments, ensure_ascii=False),
        ),
    )


def assistant_message_to_dict(assistant_message: Any) -> dict[str, Any]:
    if hasattr(assistant_message, "model_dump"):
        return assistant_message.model_dump(exclude_none=True)

    message = {"role": "assistant", "content": getattr(assistant_message, "content", None)}
    tool_calls = getattr(assistant_message, "tool_calls", None)
    if tool_calls:
        message["tool_calls"] = [call if isinstance(call,dict) else {
            'id':call.id,'type':'function','function':{'name':call.function.name,'arguments':call.function.arguments}}
            for call in tool_calls]
    return message


def read_user_input(prompt: str = "\nYou: ") -> str:
    try:
        return input(prompt)
    except KeyboardInterrupt:
        print()
        return "exit"


def main(async_memory_review: bool = True, drain_memory_on_exit: bool = False):
    from agent_execution.turn_control.environment import host_environment
    host_environment()
    from agent_execution.coordinator import resume_coordinator
    coordinator_service=resume_coordinator()
    hook_manager=default_hook_manager(diagnose=True)
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
    from base import workspace_root
    task_evaluator = TaskEvaluator(
        workspace_root=workspace_root(),
        client=get_client(),
        model=OLLAMA_MODEL,
    )
    from agent_execution.user_profile.extractor import ProfileExtractor
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
        task_evaluator=task_evaluator,
        hook_manager=hook_manager,
        profile_extractor=ProfileExtractor(get_client, OLLAMA_MODEL),
    )
    print("Mini agent ready. Type 'exit' to quit.")
    import uuid
    from agent_execution import execution_scope
    local_session = os.environ.get('AGENT_SESSION_ID') or 'local'

    try:
        while True:
            user_input = read_user_input()
            if user_input.lower() in ("exit", "quit"):
                break

            from agent_execution.approvals import human_command
            decision = human_command(user_input, "local", local_session)
            if decision is not None:
                print(decision)
                continue

            try:
                with execution_scope(session=local_session, task_id=uuid.uuid4().hex):
                    from agent_execution.tasks import dispatch_task_input
                    result = dispatch_task_input(user_input,runtime)
                    if result is None:
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
        from agent_execution.coordinator import close_coordinator
        close_coordinator()

if __name__ == "__main__":
    main()
    
