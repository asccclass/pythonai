"""One bounded runner continuation per Activity, with no raw data in wire results."""
from contextlib import closing
from functools import wraps
import threading

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .config import Settings
from .contracts import STEP_ACTIVITIES, STEP_WORKFLOW_NAME, validate_cursor, validate_envelope, validate_snapshot
from .context import execution_context
from .step_store import StepStore
from .store import Store


def protected(function):
    @wraps(function)
    def call(payload):
        stop = threading.Event()
        def heartbeat():
            while not stop.wait(10):
                try:
                    activity.heartbeat()
                except Exception:
                    return
        from contextvars import copy_context
        thread = threading.Thread(target=copy_context().run, args=(heartbeat,), daemon=True)
        thread.start()
        try:
            from .context import host_slot
            with host_slot(activity.is_cancelled):
                return function(payload)
        except Exception as error:
            from agent_execution.budgets import BudgetExhausted
            from agent_execution.delivery import CapacityError
            from .ledger import OperationUncertain
            code = ('OperationUncertain' if isinstance(error, OperationUncertain) else
                    'BudgetExhausted' if isinstance(error, BudgetExhausted) else
                    'CapacityExceeded' if isinstance(error, CapacityError) else
                    'PermissionDenied' if isinstance(error, PermissionError) else
                    'Cancelled' if isinstance(error, InterruptedError) else
                    'MissingArtifact' if isinstance(error, KeyError) else
                    'InvalidStateOrArtifact' if isinstance(error, (ValueError, TypeError)) else
                    'StepExecutionUnknown')
            raise ApplicationError(code, type=code, non_retryable=code != 'StepExecutionUnknown') from None
        finally:
            stop.set()
            thread.join()
    return call


def authority(payload, *, bootstrap=False):
    key = validate_envelope(payload) if bootstrap else validate_cursor(payload)['submission_id']
    store = Store()
    row = store.get(key)
    if activity.info().workflow_id != row['workflow_id'] or store.workflow_type(row) != STEP_WORKFLOW_NAME:
        raise PermissionError('Step does not belong to this Workflow protocol')
    return store, row, StepStore(store)


@activity.defn(name=STEP_ACTIVITIES['bootstrap'])
@protected
def bootstrap(payload: dict) -> dict:
    store, row, steps = authority(payload, bootstrap=True)
    row = steps.bootstrap_claim(row['id'], Settings.load())
    with execution_context(store, row, activity.is_cancelled) as ledger:
        return steps.bootstrap(row, ledger)


def advance(payload, kind):
    from agent_execution import execution_scope
    from agent_execution.policy import current_context
    from agent_execution.tasks import durable_invoke
    from agent_execution.budgets import BudgetExhausted, active_budget
    from agent_execution.turn_control.continuation import StepController, TaskYield
    store, row, steps = authority(payload)
    if payload['next_kind'] != kind:
        raise PermissionError('Activity kind does not match its cursor')
    with execution_context(store, row, activity.is_cancelled) as ledger:
        fence, cached = steps.claim(payload, ledger)
        if cached is not None:
            return cached
        original = current_context().check_cancelled
        with execution_scope(check_cancelled=lambda: bool(original and original()) or not steps.current(payload, fence)):
            messages = store.tasks.get(row['owner'], row['task_id'])['checkpoint']['messages']
            def execute(saved):
                from agent_execution.user_profile import refresh_messages
                from base import workspace_root
                import server
                active_budget().check()
                refresh_messages(messages)
                if saved is not None:
                    saved['messages'] = messages
                evaluator = server.TaskEvaluator(workspace_root=workspace_root(), client=server.get_client(), model=server.OLLAMA_MODEL)
                return server.run_agent(messages, resume_state=saved, task_evaluator=evaluator,
                    check_cancelled=current_context().check_cancelled, step_controller=StepController(kind))
            try:
                durable_invoke(execute, messages, None, resume=True)
            except TaskYield as boundary:
                next_kind = boundary.next_kind
            except BudgetExhausted:
                next_kind = 'finalize'
            else:
                next_kind = 'finalize'
        return steps.commit(payload, fence, ledger, next_kind)


@activity.defn(name=STEP_ACTIVITIES['model'])
@protected
def model(payload: dict) -> dict:
    return advance(payload, 'model')


@activity.defn(name=STEP_ACTIVITIES['tool'])
@protected
def tool(payload: dict) -> dict:
    return advance(payload, 'tool')


@activity.defn(name=STEP_ACTIVITIES['evaluation'])
@protected
def evaluation(payload: dict) -> dict:
    return advance(payload, 'evaluation')


@activity.defn(name=STEP_ACTIVITIES['recover'])
@protected
def recover(payload: dict) -> dict:
    from .ledger import OperationUncertain
    store, row, steps = authority(payload)
    try:
        with execution_context(store, row, activity.is_cancelled) as ledger:
            return steps.recover(payload, ledger)
    except OperationUncertain:
        store.workflow_failed(row['id'], row['owner'], 'OperationUncertain')
        raise


@activity.defn(name=STEP_ACTIVITIES['finalize'])
@protected
def finalize(payload: dict) -> dict:
    store, row, steps = authority(payload)
    if payload['next_kind'] != 'finalize':
        raise PermissionError('Finalization requires a terminal cursor')
    from agent_execution.tasks import assert_owner_active
    with closing(store.tasks.connect()) as conn:
        assert_owner_active(conn, row['owner'])
        steps._row(conn, payload)
    if row['state'] in {'completed', 'failed'} and row['result']:
        return {'submission_id': row['id']}
    with execution_context(store, row, activity.is_cancelled) as ledger:
        snapshot = validate_snapshot(ledger.load(payload['state_ref']))
        task = store.tasks.get(row['owner'], row['task_id'])
        if snapshot['task_state'] not in {'completed', 'failed'} or task['state'] != snapshot['task_state']:
            raise PermissionError('Task is not terminal or was cancelled')
        store.finish(row['id'], row['fence'], snapshot['task_state'],
                     {'reply': snapshot['reply'] or '', 'messages': snapshot['checkpoint']['messages']})
    return {'submission_id': row['id']}


ACTIVITIES = [bootstrap, model, tool, evaluation, recover, finalize]
