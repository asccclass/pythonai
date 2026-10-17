"""Reference-only Workflow. No application imports, file/DB/network/LLM access."""
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from .contracts import ACTIVITY_NAME, WORKFLOW_NAME, validate_envelope


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
