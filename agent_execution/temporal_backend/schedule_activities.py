"""Idempotent metadata activities for native schedules, without model I/O."""
import asyncio
from temporalio import activity
from temporalio.exceptions import ApplicationError
from .schedules import Schedules
from .store import digest

def checked(payload,fields):
    if not isinstance(payload,dict) or set(payload)!=set(fields):
        raise ApplicationError('InvalidScheduleReference',non_retryable=True)
    schedules = Schedules()
    if not isinstance(payload['schedule_id'],str) or len(payload['schedule_id'])!=32:
        raise ApplicationError('InvalidScheduleReference',non_retryable=True)
    workflow_id = activity.info().workflow_id
    if 'workflow_id' in payload and payload['workflow_id'] != workflow_id:
        raise ApplicationError('WrongOccurrenceWorkflow',non_retryable=True)
    if 'occurrence_id' in payload and payload['occurrence_id']!=digest(workflow_id):
        raise ApplicationError('WrongOccurrenceReference',non_retryable=True)
    return schedules

@activity.defn(name='pythonai_schedule_prepare_v1')
async def prepare(payload:dict)->dict:
    schedules = checked(payload,('schedule_id','workflow_id'))
    result = await asyncio.to_thread(schedules.prepare,payload['schedule_id'],payload['workflow_id'])
    if not result['done']:
        from .client import connect,dispatch
        row = schedules.tasks.get(result['submission_id'])
        client = await connect(schedules.tasks.settings(row))
        await dispatch(schedules.tasks,row,client)
    return result

@activity.defn(name='pythonai_schedule_poll_v1')
async def poll(payload:dict)->dict:
    schedules = checked(payload,('schedule_id','occurrence_id'))
    return await asyncio.to_thread(schedules.poll,payload['schedule_id'],payload['occurrence_id'])

@activity.defn(name='pythonai_schedule_stop_v1')
async def stop(payload:dict)->dict:
    schedules = checked(payload,('schedule_id','occurrence_id'))
    await asyncio.to_thread(schedules.stop,payload['schedule_id'],payload['occurrence_id'])
    return {'done':True}

ACTIVITIES = [prepare,poll,stop]
