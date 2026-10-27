"""Opaque lifecycle Workflow: metadata-only History, no retries of filesystem I/O."""
from datetime import timedelta
import re
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError


def envelope(value):
    if not isinstance(value, dict) or set(value) != {'operation_id'} or not isinstance(value['operation_id'], str) or not re.fullmatch('[0-9a-f]{32}', value['operation_id']):
        raise ApplicationError('Invalid lifecycle reference', non_retryable=True)
    return value


@workflow.defn(name='CoordinatorLifecycleV1')
class CoordinatorLifecycle:
    @workflow.run
    async def run(self, value: dict) -> dict:
        envelope(value)
        return await workflow.execute_activity('coordinator.lifecycle.v1', value,
            start_to_close_timeout=timedelta(seconds=300), heartbeat_timeout=timedelta(seconds=15),
            retry_policy=RetryPolicy(maximum_attempts=1),
            cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED)
