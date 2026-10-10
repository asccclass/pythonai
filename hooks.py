from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Any, Callable, Protocol


class HookLifecycle(str, Enum):
    PRE_MODEL_CALL = "PreModelCall"
    POST_MODEL_CALL = "PostModelCall"
    PRE_TOOL_EXECUTE = "PreToolExecute"
    POST_TOOL_EXECUTE = "PostToolExecute"


class HookDecision(str, Enum):
    CONTINUE = "continue"
    BLOCK = "block"
    RETURN_EARLY = "return_early"
    RETURN_TOOL_ERROR = "return_tool_error"
    APPEND_FEEDBACK = "append_feedback"


@dataclass
class HookContext:
    lifecycle: HookLifecycle
    messages: list[dict[str, Any]] = field(default_factory=list)
    model_request: dict[str, Any] = field(default_factory=dict)
    model_response: Any | None = None
    tool_call: Any | None = None
    tool_name: str | None = None
    tool_arguments: dict[str, Any] = field(default_factory=dict)
    tool_result: Any | None = None
    memory: Any | None = None
    episode_id: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    audit_metadata: dict[str, Any] = field(default_factory=dict)
    mutation_summaries: list[str] = field(default_factory=list)

    def record_mutation(self, summary: str) -> None:
        if summary:
            self.mutation_summaries.append(summary)


@dataclass
class HookResult:
    decision: HookDecision = HookDecision.CONTINUE
    message: str = ""
    return_value: Any | None = None
    tool_error: str = ""
    feedback: str = ""
    mutation_summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def continue_(cls, mutation_summary: str = "", **metadata: Any) -> "HookResult":
        return cls(decision=HookDecision.CONTINUE, mutation_summary=mutation_summary, metadata=metadata)

    @classmethod
    def return_early(cls, value: Any, message: str = "", **metadata: Any) -> "HookResult":
        return cls(
            decision=HookDecision.RETURN_EARLY,
            return_value=value,
            message=message,
            metadata=metadata,
        )

    @classmethod
    def block(cls, message: str, **metadata: Any) -> "HookResult":
        return cls(decision=HookDecision.BLOCK, message=message, metadata=metadata)

    @classmethod
    def return_tool_error(cls, message: str, **metadata: Any) -> "HookResult":
        return cls(
            decision=HookDecision.RETURN_TOOL_ERROR,
            tool_error=message,
            message=message,
            metadata=metadata,
        )

    @classmethod
    def append_feedback(cls, feedback: str, **metadata: Any) -> "HookResult":
        return cls(decision=HookDecision.APPEND_FEEDBACK, feedback=feedback, metadata=metadata)


class Hook(Protocol):
    name: str
    lifecycle: HookLifecycle
    priority: int
    enabled: bool
    fail_closed: bool

    def applies(self, context: HookContext) -> bool:
        ...

    def run(self, context: HookContext) -> HookResult:
        ...


@dataclass
class FunctionHook:
    name: str
    lifecycle: HookLifecycle
    func: Callable[[HookContext], HookResult | None]
    priority: int = 100
    enabled: bool = True
    fail_closed: bool = True
    tool_names: set[str] | None = None
    stages: set[str] | None = None
    task_types: set[str] | None = None

    def applies(self, context: HookContext) -> bool:
        if self.tool_names is not None and context.tool_name not in self.tool_names:
            return False
        stage = context.metadata.get("stage")
        if self.stages is not None and stage not in self.stages:
            return False
        task_type = context.metadata.get("task_type")
        if self.task_types is not None and task_type not in self.task_types:
            return False
        return True

    def run(self, context: HookContext) -> HookResult:
        result=self.func(context)
        return HookResult.continue_() if result is None else result


class HookManager:
    def __init__(self, hooks: list[Hook] | None = None):
        self._hooks: list[Hook] = []
        for hook in hooks or []:
            self.register(hook)

    def register(self, hook: Hook) -> None:
        if any(item.name==hook.name and item.lifecycle==hook.lifecycle for item in self._hooks):
            raise ValueError('Duplicate hook name and lifecycle')
        self._hooks.append(hook)
        self._hooks.sort(key=lambda item: (item.priority, item.name))

    def dispatch(self, lifecycle: HookLifecycle | str, context: HookContext) -> HookResult:
        lifecycle = HookLifecycle(lifecycle)
        context.lifecycle = lifecycle
        for hook in list(self._hooks):
            if not getattr(hook, "enabled", True) or hook.lifecycle != lifecycle:
                continue
            started = time.perf_counter()
            result: HookResult
            try:
                if not hook.applies(context):continue
                result = hook.run(context)
                if not isinstance(result,HookResult):raise TypeError('Hook must return HookResult')
            except Exception as error:
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                self._record_audit(
                    context,
                    hook,
                    HookResult.block(str(error), error_type=type(error).__name__),
                    elapsed_ms,
                    error=str(error),
                )
                if getattr(hook, "fail_closed", True):
                    return HookResult.block(str(error), hook=hook.name, error_type=type(error).__name__)
                continue
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            if result.mutation_summary:
                context.record_mutation(result.mutation_summary)
            self._record_audit(context, hook, result, elapsed_ms)
            if result.decision != HookDecision.CONTINUE:
                return result
        return HookResult.continue_()

    def diagnostics(self):
        return {'active':sum(bool(h.enabled) for h in self._hooks),'hooks':[
            {'name':h.name,'lifecycle':h.lifecycle.value,'priority':h.priority,
                'enabled':h.enabled,'fail_closed':h.fail_closed} for h in self._hooks]}

    def _record_audit(
        self,
        context: HookContext,
        hook: Hook,
        result: HookResult,
        elapsed_ms: int,
        error: str | None = None,
    ) -> None:
        audit = {
            "hook": hook.name,
            "lifecycle": context.lifecycle.value,
            "decision": result.decision.value,
            "mutation_summary": result.mutation_summary or "; ".join(context.mutation_summaries[-1:]),
            "elapsed_ms": elapsed_ms,
            "blocked_reason": result.message if result.decision in {HookDecision.BLOCK, HookDecision.RETURN_TOOL_ERROR} else "",
            "metadata": result.metadata,
        }
        from agent_execution.policy import current_context
        from permissions import current_role
        scope=current_context()
        audit.update(source=scope.source,identity=scope.identity,session=scope.session,
            project=scope.project,task_id=scope.task_id,role=current_role())
        if error is not None:
            audit["error"] = error
        from agent_execution.operations.redaction import redact
        from agent_execution.operations.tracing import _span
        current_span=_span.get()
        if current_span:audit.update(trace_id=current_span['trace_id'],span_id=current_span['id'])
        audit=redact(audit)
        context.audit_metadata.setdefault("hook_events", []).append(audit)
        memory = context.memory
        episode_id = context.episode_id
        if memory is None or episode_id is None or not hasattr(memory, "add_event"):
            return
        try:
            memory.add_event(episode_id, "hook_execution", metadata=audit)
        except Exception:
            pass


def default_hook_manager(diagnose=False) -> HookManager:
    from agent_execution.hook_policies import configured_manager
    return configured_manager(diagnose)
