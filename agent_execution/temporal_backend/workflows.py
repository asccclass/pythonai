"""Reference-only Workflow. No application imports, file/DB/network/LLM access."""
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from .contracts import ACTIVITY_NAME, WORKFLOW_NAME, validate_envelope

from .contracts import (STEP_WORKFLOW_NAME, STEP_ACTIVITIES, STEP_IO_MAX_ATTEMPTS,
                        STEP_METADATA_MAX_ATTEMPTS, STEP_RECOVERY_LIMIT, STEP_LIMIT,
                        HUMAN_SIGNAL, HUMAN_WORKFLOW_NAME, validate_cursor)


@workflow.defn(name=WORKFLOW_NAME)
class ReadOnlyBridge:
    @workflow.run
    async def run(self, envelope: dict) -> dict:
        validate_envelope(envelope)
        return await workflow.execute_activity(
            ACTIVITY_NAME, envelope, start_to_close_timeout=timedelta(minutes=10),
            schedule_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=timedelta(seconds=45),
            retry_policy=RetryPolicy(maximum_attempts=1))


@workflow.defn(name=STEP_WORKFLOW_NAME)
class AgentTaskWorkflowV1:
    def __init__(self):
        self.notifications = 0

    @workflow.signal(name=HUMAN_SIGNAL)
    def human_decision(self, envelope: dict):
        # Signals are hints. The Activity rechecks the persisted owner and wait.
        try:
            validate_envelope(envelope)
        except (ValueError, TypeError, KeyError):
            return
        if envelope == getattr(self, 'envelope', None):
            self.notifications += 1

    @workflow.run
    async def run(self, envelope: dict) -> dict:
        from temporalio.exceptions import ActivityError, ApplicationError
        try:
            key = validate_envelope(envelope)
        except (ValueError, TypeError, KeyError):
            raise ApplicationError('InvalidStepProtocol', non_retryable=True) from None
        self.envelope = envelope

        async def execute(kind, payload):
            return await workflow.execute_activity(STEP_ACTIVITIES[kind], payload,
                start_to_close_timeout=timedelta(minutes=10),
                schedule_to_close_timeout=timedelta(minutes=10),
                heartbeat_timeout=timedelta(seconds=45),
                retry_policy=RetryPolicy(maximum_attempts=STEP_METADATA_MAX_ATTEMPTS
                    if kind in {'bootstrap', 'recover', 'finalize', 'human'} else STEP_IO_MAX_ATTEMPTS))

        def checked(value):
            try:
                validate_cursor(value)
                if value['submission_id'] != key:
                    raise ValueError('Different submission')
            except (ValueError, TypeError, KeyError):
                raise ApplicationError('InvalidStepProtocol', non_retryable=True) from None
            return value

        cursor = checked(await execute('bootstrap', envelope))
        if cursor['revision'] != 0:
            raise ApplicationError('InvalidBootstrapRevision', non_retryable=True)
        recoveries = 0
        for _ in range(STEP_LIMIT + 1):
            if cursor['next_kind'] == 'human':
                while True:
                    before = self.notifications
                    probe = await execute('human', cursor)
                    following = checked(probe['cursor'])
                    if following != cursor:
                        if following['revision'] != cursor['revision']+1:
                            raise ApplicationError('InvalidHumanRevision', non_retryable=True)
                        cursor = following
                        break
                    remaining = probe['expires'] - workflow.now().timestamp()
                    try:
                        await workflow.wait_condition(lambda: self.notifications != before,
                            timeout=timedelta(seconds=max(.001, remaining)))
                    except TimeoutError:
                        pass  # Recheck TTL and persist the terminal projection.
            if cursor['next_kind'] == 'finalize':
                result = await execute('finalize', cursor)
                if result != {'submission_id': key}:
                    raise ApplicationError('InvalidFinalization', non_retryable=True)
                return result
            if cursor['revision'] >= STEP_LIMIT:
                raise ApplicationError('StepLimitReached', non_retryable=True)
            try:
                following = checked(await execute(cursor['next_kind'], cursor))
                if following['revision'] != cursor['revision']+1:
                    raise ApplicationError('InvalidStepRevision', non_retryable=True)
            except ActivityError as error:
                if (isinstance(error.cause, ApplicationError) and error.cause.non_retryable) or recoveries >= STEP_RECOVERY_LIMIT:
                    raise
                recoveries += 1
                following = checked(await execute('recover', cursor))
                if following['revision'] not in {cursor['revision'], cursor['revision']+1}:
                    raise ApplicationError('InvalidRecoveryRevision', non_retryable=True)
            cursor = following
        raise ApplicationError('StepLimitReached', non_retryable=True)


@workflow.defn(name=HUMAN_WORKFLOW_NAME)
class AgentTaskWorkflowV2(AgentTaskWorkflowV1):
    """New admission/queue keeps V1 workers away from human continuations."""
    @workflow.run
    async def run(self, envelope: dict) -> dict:
        return await super().run(envelope)
