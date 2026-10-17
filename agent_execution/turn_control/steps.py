"""Shared I/O steps for legacy execution and Temporal Activities.

These functions perform one attempt. The caller owns checkpoints, hooks,
retry decisions and operation deduplication. Never call them from a Workflow.
"""
from __future__ import annotations

from typing import Callable


def model_step(client, request, messages, *, iteration, attempt=1,
               event_callback=None, check_cancelled=None):
    """Make one budgeted model request, with identical streaming semantics."""
    from agent_execution.budgets import budget_client
    from agent_execution.operations.tracing import span

    if check_cancelled and check_cancelled():
        raise InterruptedError('Task cancelled before model execution')
    with span('attempt', 'model.attempt', {
        'retry': attempt > 1, 'attempt': attempt, 'iteration': iteration,
    }):
        client = budget_client(client)
        if event_callback is not None:
            from agent_execution.streaming import streamed_completion
            event_callback('model.start', {'iteration': iteration, 'model': request['model']})
            return streamed_completion(client, request['model'], messages,
                                       request['tools'], event_callback, check_cancelled)
        arguments = {'model': request['model'], 'messages': messages}
        if request['tools']:
            arguments['tools'] = request['tools']
        return client.chat.completions.create(**arguments).choices[0].message


def tool_step(tool_call, *, invoke: Callable, memory=None, episode_id=None,
              check_cancelled=None):
    """Execute via the existing policy-aware dispatcher, preserving raw results.

    The caller runs PRE/POST hooks; marking the call as hook-managed prevents
    the dispatcher from running them twice. Policy, budgets and approvals still
    run inside the dispatcher. The cancellation callback stays scoped to this
    operation and is visible to process/tool implementations.
    """
    from agent_execution import execution_scope
    with execution_scope(check_cancelled=check_cancelled,
                         tool_hook_managed=tool_call.function.name):
        return invoke(tool_call, memory=memory, episode_id=episode_id)


def evaluation_step(evaluator, *, user_goal, messages, evaluation_start,
                    final_reply, pre_state):
    """Capture actual post-state and evaluate only the current turn's evidence."""
    post_state = evaluator.capture_state()
    return evaluator.evaluate(user_goal=user_goal, messages=messages[evaluation_start:],
                              final_reply=final_reply or '', pre_state=pre_state,
                              post_state=post_state)
