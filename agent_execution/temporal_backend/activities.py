"""Same-host, read-only bridge. No model-provided context or automatic replay."""
import json

from temporalio import activity
from temporalio.exceptions import ApplicationError, CancelledError

from .activity_guard import activity_guard, cancelled
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
                from .publication import writer
                sink = writer(store,row,'bridge')
                try:
                    return server.run_agent(messages, memory=None, resume_state=saved,
                        event_callback=sink, check_cancelled=current_context().check_cancelled,task_evaluator=evaluator)
                finally:
                    if sink is not None:
                        sink.flush()
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


@activity.defn(name=ACTIVITY_NAME, no_thread_cancel_exception=True)
def execute_readonly(envelope: dict) -> dict:
    key = validate_envelope(envelope)
    if activity.info().workflow_id != Store().get(key)['workflow_id']:
        raise ApplicationError('Submission does not belong to this Workflow', non_retryable=True)
    try:
        from .context import host_slot
        with activity_guard(), host_slot(cancelled):
            return run_submission(key, Settings.load(), cancelled=cancelled)
    except Exception as error:
        if activity.is_cancelled():
            raise CancelledError('Activity cancellation acknowledged') from None
        raise ApplicationError(type(error).__name__, non_retryable=True) from None
