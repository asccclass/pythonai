"""Same-host, read-only bridge. No model-provided context or automatic replay."""
from contextlib import ExitStack
import json
from pathlib import Path
import threading

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .config import Settings
from .contracts import ACTIVITY_NAME, READ_TOOLS, validate_envelope
from .store import Store


def run_submission(key, settings, *, runner=None, cancelled=None):
    from agent_execution import execution_scope
    from agent_execution.budgets import budget_scope
    from agent_execution.tasks import durable_invoke, task_owner
    from agent_execution.policy import current_context
    from agent_execution.sessions import project_workspace
    from permissions import get_user_role, set_current_role
    from .router import activity_scope
    store = Store()
    row = store.claim(key, settings)
    try:
        scope = json.loads(row['context'])
        role = row['role']
        if scope['identity'] == 'local':
            if scope['source'] != 'local':
                raise PermissionError('Remote input cannot claim local authority')
        else:
            latest_role = get_user_role(scope['identity'])
            levels = {'viewer': 0, 'operator': 1, 'admin': 2}
            role = min((role, latest_role), key=lambda value: levels[value])
        # Resolve the configured project without trusting the saved workspace override.
        with execution_scope(workspace=None, repository_workspace=None):
            configured = project_workspace(scope['project']).resolve()
        if Path(scope['workspace']).resolve() != configured:
            raise PermissionError('Workspace configuration changed; M0 refuses relocation')
        scope['workspace'] = configured
        scope['allowed_tools'] = frozenset(scope['allowed_tools']) & READ_TOOLS
        if scope.get('communication_db'):
            scope['communication_db'] = Path(scope['communication_db'])
        scope['task_id'] = row['task_id']
        scope['check_cancelled'] = lambda: bool(cancelled and cancelled()) or store.get(key)['state'] == 'cancelled'
        messages = json.loads(row['input'])
        with ExitStack() as stack:
            stack.enter_context(set_current_role(role))
            stack.enter_context(execution_scope(**scope))
            if task_owner(current_context()) != row['owner']:
                raise PermissionError('Invalid stored task scope')
            stack.enter_context(activity_scope(row['owner'], row['task_id']))
            from agent_execution.workspace_lifecycle import LifecycleStore
            LifecycleStore().ensure_session()
            stack.enter_context(budget_scope())
            from .ledger import Ledger
            from .operations import operation_scope
            stack.enter_context(operation_scope(Ledger(store, key, row['fence'])))
            def execute(saved):
                from agent_execution.user_profile import refresh_messages
                refresh_messages(messages)
                if saved is not None:
                    saved['messages'] = messages
                if runner is not None:
                    return runner(messages, saved)
                import server
                from base import workspace_root
                evaluator = server.TaskEvaluator(workspace_root=workspace_root(), client=server.get_client(), model=server.OLLAMA_MODEL)
                return server.run_agent(messages, memory=None, resume_state=saved,
                                        check_cancelled=current_context().check_cancelled,
                                        task_evaluator=evaluator)
            reply = durable_invoke(execute, messages, None)
        task = store.tasks.get(row['owner'], row['task_id'])
        state = 'completed' if task['state'] == 'completed' else 'failed'
        store.finish(key, row['fence'], state, {'reply': str(reply), 'messages': messages})
    except Exception as error:
        try:
            store.finish(key, row['fence'], 'uncertain', {'error_type': type(error).__name__})
        except Exception:
            pass  # A cancellation/deletion fence must not be undone by error reporting.
        raise
    return {'submission_id': key}


@activity.defn(name=ACTIVITY_NAME)
def execute_readonly(envelope: dict) -> dict:
    key = validate_envelope(envelope)
    if activity.info().workflow_id != Store().get(key)['workflow_id']:
        raise ApplicationError('Submission does not belong to this Workflow', non_retryable=True)
    stop = threading.Event()
    def heartbeat():
        while not stop.wait(10):
            activity.heartbeat({'submission_id': key})
    # Context explicitly propagated to the heartbeat thread; user auth is rebuilt above.
    from contextvars import copy_context
    thread = threading.Thread(target=copy_context().run, args=(heartbeat,), daemon=True)
    thread.start()
    try:
        return run_submission(key, Settings.load(), cancelled=activity.is_cancelled)
    except Exception as error:
        raise ApplicationError(type(error).__name__, non_retryable=True) from None
    finally:
        stop.set()
        thread.join(2)
