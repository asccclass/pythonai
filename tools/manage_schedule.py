import json
from agent_execution.policy import policy_tool
from agent_execution.scheduling.store import SchedulerStore,natural_cron

@policy_tool('manage_schedule')
def manage_schedule(action,job_id='',name='',schedule='',job_type='watch',payload=None,timezone='Asia/Taipei',timeout=60,max_runs=1000):
    store=SchedulerStore()
    if action=='list':result=store.list_jobs()
    elif action=='notifications':result=store.notifications()
    elif action=='disable':result={'disabled':store.disable(job_id)}
    elif action=='add':
        expression=schedule if len(schedule.split())==5 else natural_cron(schedule)
        result={'job_id':store.add_job(name,expression,job_type,payload or {},timezone=timezone,timeout=timeout,max_runs=max_runs)}
    else:raise ValueError('Unknown schedule action')
    return json.dumps(result,ensure_ascii=False)
