"""Synchronous legacy API bridge to an opt-in read-only Temporal execution."""
import asyncio
from contextlib import closing
import json

from .config import Settings
from .contracts import READ_TOOLS, STEP_TOOLS, HUMAN_WORKFLOW_NAME, APPROVAL_WORKFLOW_NAME


def restore_result(row, messages):
    value = json.loads(row['result'])
    if not isinstance(value.get('reply'), str) or not isinstance(value.get('messages'), list):
        raise ValueError('Invalid persisted Temporal result')
    messages[:] = value['messages']
    return value['reply']


def invoke(store, messages, *, resume=False):
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
    settings = Settings.load()
    scope = {name: getattr(context, name) for name in ('source', 'identity', 'session', 'conversation', 'project')}
    scope['workspace'] = str(workspace_root())
    if settings.engine == 'approval':
        from agent_execution.approvals import ApprovalStore
        scope['approval_db'] = str(ApprovalStore().path.resolve())
    tools = STEP_TOOLS if settings.engine in {'human', 'approval'} else READ_TOOLS
    scope['allowed_tools'] = sorted(tools if context.allowed_tools is None else tools & context.allowed_tools)
    if context.communication_db:
        scope['communication_db'] = str(context.communication_db.resolve())
    if context.coordinator_owner or context.lifecycle_owner:
        raise ValueError('M0 Temporal does not accept delegated coordinator execution')
    budget = TaskBudget(owner, task)
    budget.check()
    row = store.for_task(owner, task) if resume else store.submit(owner, task, messages, scope, current_role(), settings)
    if row is None:
        raise ValueError('No Temporal task to resume')
    if row['state'] in {'completed', 'failed'} and row['result']:
        return restore_result(row, messages)
    if resume:
        if store.workflow_type(row) not in {HUMAN_WORKFLOW_NAME, APPROVAL_WORKFLOW_NAME}:
            raise ValueError('Human answers require the step protocol')
        task_row = store.tasks.get(owner, task)
        checkpoint = task_row['checkpoint']
        if task_row['phase'] == 'waiting_approval':
            from agent_execution.approvals import ApprovalStore
            key = checkpoint['approval_wait']['id']
            if ApprovalStore().decision_state(key, context.identity, context.session) == 'pending':
                from exceptions import TaskSuspendedException
                raise TaskSuspendedException(task_row['reply'], checkpoint['suspended_tool_call_id'], checkpoint['messages'])
        call_id = checkpoint.get('suspended_tool_call_id')
        if call_id and task_row['phase'] != 'waiting_approval':
            saved_messages = checkpoint['messages']
            if (len(messages) == len(saved_messages)+1 and messages[:-1] == saved_messages
                    and messages[-1].get('role') == 'tool' and messages[-1].get('tool_call_id') == call_id):
                from .decisions import Decisions
                Decisions(store).answer(row['id'], owner, call_id, messages[-1].get('content'))
            else:
                from exceptions import TaskSuspendedException
                raise TaskSuspendedException(store.tasks.get(owner, task)['reply'], call_id, checkpoint['messages'])
    def cancelled():
        return (bool(context.check_cancelled and context.check_cancelled())
                or store.tasks.get(owner, task)['state'] == 'cancelled')

    async def run():
        from .client import connect, dispatch
        client = await connect(store.settings(row))
        handle = (client.get_workflow_handle(row['workflow_id']) if resume else await dispatch(store, row, client))
        result = asyncio.create_task(handle.result())
        try:
            from .decisions import Decisions
            decisions = Decisions(store)
            gate = None
            if store.workflow_type(row) == APPROVAL_WORKFLOW_NAME:
                from .approval_gate import ApprovalGate
                gate = ApprovalGate(store)
            while True:
                done, _ = await asyncio.wait({result}, timeout=.2)
                if done:
                    return result.result()
                task_row = store.tasks.get(owner, task)
                if task_row['state'] == 'suspended' and task_row['phase'] == 'waiting_approval':
                    from agent_execution.approvals import ApprovalStore
                    key = task_row['checkpoint']['approval_wait']['id']
                    if ApprovalStore().decision_state(key, context.identity, context.session) == 'pending':
                        return {'submission_id': row['id'], 'waiting': True}
                if task_row['state'] == 'suspended' and task_row['phase'] == 'waiting_user':
                    # Only expose a wait after its cursor and outbox slot commit.
                    with closing(store.tasks.connect()) as conn:
                        wait = conn.execute('SELECT state FROM temporal_human_answers WHERE submission_id=? ORDER BY revision DESC LIMIT 1',
                                            (row['id'],)).fetchone()
                    if wait and wait['state'] == 'waiting':
                        return {'submission_id': row['id'], 'waiting': True}
                if gate is not None:
                    from agent_execution.approvals import ApprovalStore
                    from .approval_outbox import pending as approval_events
                    for event in approval_events(ApprovalStore(), queues={row['queue']}):
                        if json.loads(event['binding'])['submission_id'] == row['id']:
                            try:
                                await gate.dispatch(client, event)
                            except Exception:
                                pass
                for pending in decisions.pending():
                    if pending['id'] == row['id']:
                        try:
                            await decisions.dispatch(client, pending)
                        except Exception:
                            pass  # Worker reconcile retains the durable retry.
        except asyncio.CancelledError:
            # Cancelling a client-side wait does not cancel the remote workflow by itself.
            store.cancel(row['id'], owner)
            await handle.cancel()
            raise
        finally:
            result.cancel()
            await asyncio.gather(result, return_exceptions=True)

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
    if envelope == {'submission_id': row['id'], 'waiting': True}:
        from exceptions import TaskSuspendedException
        waiting = store.tasks.get(owner, task)
        raise TaskSuspendedException(waiting['reply'], waiting['checkpoint']['suspended_tool_call_id'], waiting['checkpoint']['messages'])
    if envelope != {'submission_id': row['id']}:
        raise ValueError('Workflow returned an invalid result reference')
    result = store.get(row['id'], owner)
    if result['state'] not in {'completed', 'failed'} or not result['result']:
        return f'Error: Temporal task {task} has state {result["state"]}; inspect before recovery.'
    return restore_result(result, messages)
