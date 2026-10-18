"""Same-host, read-only bridge. No model-provided context or automatic replay."""
import json
import threading

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .config import Settings
from .contracts import ACTIVITY_NAME, validate_envelope
from .store import Store


def run_submission(key, settings, *, runner=None, cancelled=None):
    from agent_execution.tasks import durable_invoke
    from agent_execution.policy import current_context
    store = Store()
    row = store.claim(key, settings)
    try:
        from .context import execution_context
        messages = json.loads(row['input'])
        with execution_context(store, row, cancelled):
            from agent_execution.budgets import active_budget
            active_budget().check()
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
        from .context import host_slot
        with host_slot(activity.is_cancelled):
            return run_submission(key, Settings.load(), cancelled=activity.is_cancelled)
    except Exception as error:
        raise ApplicationError(type(error).__name__, non_retryable=True) from None
    finally:
        stop.set()
        thread.join(2)
