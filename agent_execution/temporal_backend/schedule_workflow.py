"""Reference-only native Schedule occurrence; application I/O lives in Activities."""
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy

@workflow.defn(name='PythonAIScheduleOccurrenceV1')
class ScheduleOccurrence:
    @workflow.run
    async def run(self, envelope: dict) -> dict:
        if set(envelope)!={'schedule_id'} or not isinstance(envelope['schedule_id'],str) or len(envelope['schedule_id'])!=32:
            from temporalio.exceptions import ApplicationError
            raise ApplicationError('InvalidScheduleReference',non_retryable=True)
        payload = {**envelope,'workflow_id':workflow.info().workflow_id}
        async def execute(name,value):
            return await workflow.execute_activity('pythonai_schedule_'+name+'_v1',value,
                start_to_close_timeout=timedelta(seconds=30),
                schedule_to_close_timeout=timedelta(seconds=90),
                retry_policy=RetryPolicy(maximum_attempts=3))
        prepared = await execute('prepare',payload)
        if prepared['done']:
            return {'done':True}
        probe = {**envelope,'occurrence_id':prepared['occurrence_id']}
        try:
            while not (await execute('poll',probe))['done']:
                await workflow.sleep(2)
        except BaseException:
            await execute('stop',probe)
            raise
        return {'done':True}
