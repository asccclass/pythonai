from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import wraps
import inspect
import json
from pathlib import Path
from typing import Any, Callable

from permissions import check_tool_permission, current_role
from .operations.tracing import traced


class PolicyDenied(PermissionError):
    """An action denied before invoking the tool."""


@dataclass(frozen=True)
class ExecutionContext:
    source: str = "local"
    identity: str = "local"
    session: str = "local"
    project: str = "default"
    task_id: str | None = None
    workspace: Path | None = None
    repository_workspace: Path | None = None
    memory: Any = None
    episode_id: int | None = None
    trusted_assets: tuple[Path, ...] = ()
    check_cancelled: Callable[[], bool] | None = None
    tool_hook_managed: str | None = None
    allowed_tools: frozenset[str] | None = None
    coordinator_node: str | None = None
    coordinator_owner: str | None = None


_context: ContextVar[ExecutionContext] = ContextVar("execution_context", default=ExecutionContext())
_approved: ContextVar[tuple | None] = ContextVar("approved_action", default=None)
_delegated_wrapper=ContextVar('delegated_policy_wrapper',default=None)
PATH_KEYS = {"path", "file_path", "image_path", "project_path", "project_name", "cwd",
             "env_file", "test_target", "directory", "directory_path", "output_path"}


@contextmanager
def execution_scope(**changes):
    token = _context.set(replace(_context.get(), **changes))
    try:
        yield _context.get()
    finally:
        _context.reset(token)


def resolve_path(path: str | Path) -> Path:
    # Import lazily: base imports Skills which in turn uses this policy.
    from base import resolve_workspace_path
    return resolve_workspace_path(path)


def current_context() -> ExecutionContext:
    return _context.get()


def git_workspace() -> Path:
    """Reject Git's implicit traversal into a repository outside the workspace."""
    from .process import run_process
    root = resolve_path(".")
    result = run_process(["git", "rev-parse", "--show-toplevel"], cwd=root,
                            capture_output=True, text=True, encoding="utf-8", check=True)
    return resolve_path(result.stdout.strip())


def _validate_paths(arguments: dict[str, Any], trusted_assets: tuple[Path, ...]) -> None:
    from base import resolve_workspace_or_trusted_path, validate_command_paths
    for key, value in arguments.items():
        if key in {"memory", "episode_id"}:
            continue
        if key == "trusted_asset_roots":
            roots = tuple(Path(item).resolve() for item in value or [])
            if any(root not in trusted_assets for root in roots):
                raise PolicyDenied("Untrusted caller cannot grant trusted_asset_roots")
        elif key in PATH_KEYS or key.endswith(("_path", "_dir", "_directory")):
            if value not in (None, ""):
                # Only run_command may read/use trusted executable assets outside workspace.
                resolve_workspace_or_trusted_path(value, list(trusted_assets))
        elif key == "command" and isinstance(value, list):
            validate_command_paths(value, list(trusted_assets))
        elif isinstance(value, dict):
            _validate_paths(value, ())
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _validate_paths(item, ())


def _audit_denial(name: str, reason: str) -> None:
    context = _context.get()
    metadata = {"tool": name, "role": current_role(), "source": context.source,
                "identity": context.identity, "reason": reason}
    print(f"AUDIT: Permission denied: {json.dumps(metadata, ensure_ascii=False)}")
    if context.memory is not None and context.episode_id is not None:
        context.memory.add_event(context.episode_id, "permission_denied", content=reason,
                                 metadata=metadata)


@traced('tool')
def execute_tool(name: str, arguments: dict[str, Any], function: Callable[..., Any], *, _skip_hooks=False) -> Any:
    from .approvals import ApprovalStore, ApprovalRequired, canonical
    from .budgets import active_budget, BudgetExhausted
    budget=active_budget()
    values = dict(arguments)
    approval_id = values.pop("approval_id", None)
    authorization = None
    managed=_skip_hooks or current_context().tool_hook_managed==name
    manager=None
    hook_context=None
    try:
        if not managed:
            from hooks import default_hook_manager,HookDecision,HookLifecycle
            from .hook_policies import tool_context
            manager=default_hook_manager()
            hook_context=tool_context(name,arguments,function)
            decision=manager.dispatch(HookLifecycle.PRE_TOOL_EXECUTE,hook_context)
            if decision.decision!=HookDecision.CONTINUE:
                if decision.metadata.get('error_type')=='BudgetExhausted':raise BudgetExhausted(decision.message)
                raise PolicyDenied(decision.tool_error or decision.message or 'Tool blocked by hook')
        if budget is not None:
            budget.check()
            if current_context().check_cancelled and current_context().check_cancelled():
                raise BudgetExhausted('Task cancelled before tool execution')
        if not check_tool_permission(name):
            raise PolicyDenied(f"Role '{current_role()}' cannot execute {name}")
        if current_context().allowed_tools is not None and name not in current_context().allowed_tools:
            raise PolicyDenied(f'Tool is outside this agent role: {name}')
        assets = _context.get().trusted_assets if name == "run_command" else ()
        _validate_paths(arguments, assets)
        external_delivery=name=='project_delivery' and values.get('action') in {'publish','merge'}
        if name=='manage_schedule' and values.get('action')=='add':
            from .scheduling.store import natural_cron,validate_job
            schedule=values.get('schedule','')
            expression=schedule if isinstance(schedule,str) and len(schedule.split())==5 else natural_cron(schedule)
            validate_job(values.get('name',''),expression,values.get('job_type','watch'),values.get('payload') or {},
                         values.get('timezone','Asia/Taipei'),values.get('timeout',60),values.get('max_runs',1000))
        if name == 'git_rollback' or external_delivery or (name == 'manage_schedule' and values.get('action') == 'add') or (name == "manage_memory" and values.get("action") in {"archive", "contradict", "supersede"}):
            if name=='manage_memory':values = {"reason": "", "superseded_by": None, **values}
            context = _context.get()
            action = f"{context.project}:{name}" + (f':{context.task_id}' if name=='git_rollback' or external_delivery else '')
            approval_values=values
            if external_delivery:
                from .patches import ArtifactStore
                plan=ArtifactStore().get(values.get('plan_id'),'delivery')
                required='prepared' if values['action']=='publish' else 'ready'
                if plan['state']!=required:raise ValueError('Prepare a reviewable/ready delivery before requesting approval')
                approval_values={**values,'prepared_digest':plan['payload']['digest'],'commit':plan['payload']['head']}
            authorization = (context.identity, context.session, action, canonical(approval_values))
            if _approved.get() != authorization:
                store = ApprovalStore()
                if not approval_id:
                    raise ApprovalRequired(store.request(context.identity, context.session, action, approval_values))
                store.consume(approval_id, context.identity, context.session, action, approval_values)
    except PermissionError as error:
        _audit_denial(name, str(error))
        if isinstance(error, ApprovalRequired):
            from .companion.approval_bridge import wait_for_approval
            approved = wait_for_approval(error.approval_id, name, arguments)
            if approved is None:
                raise
            if not approved:
                raise PolicyDenied('Human denied the operation; it was not executed') from error
            # The original operation remains paused in this stack. Recheck paths,
            # role and delivery snapshot before consuming its single-use grant.
            from permissions import get_user_role, set_current_role
            with set_current_role(get_user_role(context.identity)):
                if not check_tool_permission(name):
                    raise PolicyDenied('Role changed while awaiting approval')
            _validate_paths(arguments, assets)
            if budget is not None:
                budget.check()
            if context.check_cancelled and context.check_cancelled():
                raise InterruptedError('Task cancelled while awaiting approval')
            if external_delivery:
                latest = ArtifactStore().get(values.get('plan_id'), 'delivery')
                if latest['state'] != required or latest['payload']['digest'] != plan['payload']['digest'] or latest['payload']['head'] != plan['payload']['head']:
                    raise PolicyDenied('Delivery changed while awaiting approval')
            store.consume(error.approval_id, context.identity, context.session, action, approval_values)
        else:
            raise PolicyDenied(str(error)) from error
    token = _approved.set(authorization)
    delegate=_delegated_wrapper.set(function if getattr(function,'_policy_tool_name',None)==name else None)
    try:
        with execution_scope(tool_hook_managed=None):
            try:
                result=function(**values)
            except Exception as error:
                if manager is not None:
                    from .results import ToolResult
                    hook_context.tool_result=ToolResult(False,error=f'{type(error).__name__}: {error}')
                    manager.dispatch(HookLifecycle.POST_TOOL_EXECUTE,hook_context)
                raise
        if manager is not None:
            from hooks import HookDecision,HookLifecycle
            hook_context.tool_result=result
            decision=manager.dispatch(HookLifecycle.POST_TOOL_EXECUTE,hook_context)
            if decision.decision!=HookDecision.CONTINUE:
                raise PolicyDenied(decision.tool_error or decision.message or 'Tool result rejected by hook')
        return result
    finally:
        _delegated_wrapper.reset(delegate)
        _approved.reset(token)


def policy_tool(name: str):
    """Wrap registered tools and direct public calls without changing their signature."""
    def decorate(function):
        if getattr(function, "_policy_tool_name", None) == name:
            return function
        signature = inspect.signature(function)

        @wraps(function)
        def wrapped(*args, **kwargs):
            kwargs = dict(kwargs)
            approval_id = kwargs.pop("approval_id", None)
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            arguments = dict(bound.arguments)
            for parameter in signature.parameters.values():
                if parameter.kind == inspect.Parameter.VAR_KEYWORD:
                    arguments.update(arguments.pop(parameter.name, {}))
            if approval_id:
                arguments["approval_id"] = approval_id
            delegated=_delegated_wrapper.get() is wrapped
            cleared=_delegated_wrapper.set(None)
            try:
                def invoke(**_values):return function(*args,**kwargs)
                invoke.__signature__=signature
                return execute_tool(name, arguments, invoke,_skip_hooks=delegated)
            finally:
                _delegated_wrapper.reset(cleared)

        wrapped._policy_tool_name = name
        return wrapped
    return decorate
