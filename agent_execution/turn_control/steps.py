"""Shared I/O steps for legacy execution and Temporal Activities.

These functions perform one attempt. The caller owns checkpoints, hooks,
retry decisions and operation deduplication. Never call them from a Workflow.
"""
from __future__ import annotations

from typing import Callable


def model_step(client, request, messages, *, iteration, attempt=1,
               event_callback=None, check_cancelled=None, operation_id=None):
    """Make one budgeted model request, with identical streaming semantics."""
    from agent_execution.budgets import budget_client
    from agent_execution.operations.tracing import span

    if check_cancelled and check_cancelled():
        raise InterruptedError('Task cancelled before model execution')
    def execute():
        with span('attempt', 'model.attempt', {
            'retry': attempt > 1, 'attempt': attempt, 'iteration': iteration,
        }):
            budgeted = budget_client(client)
            if event_callback is not None:
                from agent_execution.streaming import streamed_completion
                event_callback('model.start', {'iteration': iteration, 'model': request['model']})
                return streamed_completion(budgeted, request['model'], messages,
                                           request['tools'], event_callback, check_cancelled)
            arguments = {'model': request['model'], 'messages': messages}
            if request['tools']:
                arguments['tools'] = request['tools']
            return budgeted.chat.completions.create(**arguments).choices[0].message
    from agent_execution.temporal_backend.operations import run_operation
    from openai.types.chat import ChatCompletionMessage
    return run_operation('model', operation_id, {'request': request, 'messages': messages}, execute,
        encode=lambda value: value.model_dump(exclude_none=True),
        decode=lambda value: ChatCompletionMessage(**value),
        evidence=lambda value: {'kind': 'model_response', 'tool_call_ids': [call.id for call in value.tool_calls or []]})


def tool_step(tool_call, *, invoke: Callable, memory=None, episode_id=None,
              check_cancelled=None, operation_id=None):
    """Execute via the existing policy-aware dispatcher, preserving raw results.

    The caller runs PRE/POST hooks; marking the call as hook-managed prevents
    the dispatcher from running them twice. Policy, budgets and approvals still
    run inside the dispatcher. The cancellation callback stays scoped to this
    operation and is visible to process/tool implementations.
    """
    from agent_execution import execution_scope
    from agent_execution.results import normalize_result, ToolResult
    from agent_execution.temporal_backend.operations import run_operation
    name = tool_call.function.name
    with execution_scope(check_cancelled=check_cancelled,
                         tool_hook_managed=name):
        return run_operation('tool', operation_id,
            lambda: {'name': name, 'arguments': _tool_arguments(tool_call.function.arguments)},
            lambda: invoke(tool_call, memory=memory, episode_id=episode_id),
            encode=lambda value: normalize_result(value, name).to_dict(),
            decode=lambda value: ToolResult(**value),
            evidence=lambda value: {'kind': 'tool_execution',
                'execution': normalize_result(value, name).evidence,
                'side_effects': normalize_result(value, name).side_effects})


def _tool_arguments(raw):
    import json
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {'invalid_json': raw}


def evaluation_step(evaluator, *, user_goal, messages, evaluation_start,
                    final_reply, pre_state, operation_id=None):
    """Capture actual post-state and evaluate only the current turn's evidence."""
    def execute():
        post_state = evaluator.capture_state()
        return evaluator.evaluate(user_goal=user_goal, messages=messages[evaluation_start:],
                                  final_reply=final_reply or '', pre_state=pre_state,
                                  post_state=post_state)
    from dataclasses import asdict
    from task_evaluator import EvaluationResult
    from agent_execution.temporal_backend.operations import run_operation
    return run_operation('evaluation', operation_id,
        {'user_goal': user_goal, 'messages': messages[evaluation_start:],
         'final_reply': final_reply or '', 'pre_state': pre_state}, execute,
        encode=asdict, decode=lambda value: EvaluationResult(**value),
        evidence=lambda value: {'verified': value.verified, 'evidence': value.evidence})
