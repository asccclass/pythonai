"""Private native-process boundary; only trusted scheduler launches this entry."""
import json
import os
from pathlib import Path
import sys

if __name__=='__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
    job=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    os.environ['AGENT_COMMAND_TIMEOUT_SECONDS']=str(job['timeout'])
    os.environ['SCHEDULER_DB_PATH']=job['db_path']
    from agent_execution.scheduling.store import SchedulerStore
    from agent_execution.scheduling.engine import execute_payload
    from agent_execution.policy import execution_scope
    store=SchedulerStore()
    try:
        with execution_scope(check_cancelled=lambda:not store.alive(job['id'],job['lock_token'])):
            state,output,error=execute_payload(job)
    except Exception as failure:state,output,error='needs_input','',type(failure).__name__
    Path(sys.argv[2]).write_text(json.dumps({'state':state,'output':output[:65536],'error':error[:1000]},ensure_ascii=False),encoding='utf-8')
