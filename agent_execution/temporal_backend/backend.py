"""Synchronous legacy API bridge to an opt-in read-only Temporal execution."""
import asyncio
import json

from .config import Settings
from .contracts import READ_TOOLS


def restore_result(row, messages):
    value = json.loads(row['result'])
    if not isinstance(value.get('reply'), str) or not isinstance(value.get('messages'), list):
        raise ValueError('Invalid persisted Temporal result')
    messages[:] = value['messages']
    return value['reply']


def invoke(store, messages):
    from agent_execution.policy import current_context
    from agent_execution.tasks import task_owner
    from agent_execution.budgets import TaskBudget
    from permissions import current_role
    from base import workspace_root
    context = current_context()
    if context.source not in {'local', 'api', 'websocket', 'communication'}:
        raise ValueError('M0 Temporal does not migrate scheduler/coordinator execution')
    # Root budgets and lifecycle authority stay in existing stores on this host.
    owner, task = task_owner(context), context.task_id
    scope = {name: getattr(context, name) for name in ('source', 'identity', 'session', 'conversation', 'project')}
    scope['workspace'] = str(workspace_root())
    scope['allowed_tools'] = sorted(READ_TOOLS if context.allowed_tools is None else READ_TOOLS & context.allowed_tools)
    if context.communication_db:
        scope['communication_db'] = str(context.communication_db.resolve())
    if context.coordinator_owner or context.lifecycle_owner:
        raise ValueError('M0 Temporal does not accept delegated coordinator execution')
    budget = TaskBudget(owner, task)
    budget.check()
    settings = Settings.load()
    row = store.submit(owner, task, messages, scope, current_role(), settings)
    if row['state'] in {'completed', 'failed'} and row['result']:
        return restore_result(row, messages)
    def cancelled():
        return (bool(context.check_cancelled and context.check_cancelled())
                or store.tasks.get(owner, task)['state'] == 'cancelled')

    async def run():
        from .client import connect, dispatch
        client = await connect(store.settings(row))
        handle = await dispatch(store, row, client)
        try:
            return await handle.result()
        except asyncio.CancelledError:
            # Cancelling a client-side wait does not cancel the remote workflow by itself.
            store.cancel(row['id'], owner)
            await handle.cancel()
            raise

    from .client import bridge
    try:
        envelope = bridge().call(run(), min(600, budget.remaining_seconds()), cancelled)
    except Exception as error:
        if isinstance(error, (InterruptedError, TimeoutError)):
            store.cancel(row['id'], owner)
        elif type(error).__name__ == 'WorkflowFailureError':
            store.workflow_failed(row['id'], owner, type(error).__name__)
        # Leave durable evidence intact, never run the supplied Python closure locally.
        return f'Error: Temporal {type(error).__name__}; task {task} remains bound to Temporal. Inspect status before recovery.'
    if envelope != {'submission_id': row['id']}:
        raise ValueError('Workflow returned an invalid result reference')
    result = store.get(row['id'], owner)
    if result['state'] not in {'completed', 'failed'} or not result['result']:
        return f'Error: Temporal task {task} has state {result["state"]}; inspect before recovery.'
    return restore_result(result, messages)
