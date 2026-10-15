from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import uuid
from ..policy import execution_scope, current_context
from ..process import run_process
from ..tasks import durable_invoke
from ..budgets import budget_scope
from permissions import get_user_role, set_current_role
from .store import SchedulerStore


def execute_payload(job):
    scope=json.loads(job.get('context') or '{}')
    scope={**{'source':'scheduler','identity':job.get('owner_identity','local'),'session':'local','project':'default'},**scope}
    if scope.get('workspace'):scope['workspace']=Path(scope['workspace'])
    role=job.get('owner_role','admin') if scope['identity']=='local' else get_user_role(scope['identity'])
    if role!='admin':return 'needs_input','','Owner is no longer authorized'
    if scope.get('workspace'):
        from ..policy import resolve_path
        with execution_scope(**{k:v for k,v in scope.items() if k!='workspace'},workspace=None):
            if resolve_path('.')!=scope['workspace']:return 'needs_input','','Workspace configuration changed'
    payload=json.loads(job['job_payload']);kind=job['job_type']
    with set_current_role(role),execution_scope(**scope,task_id=job.get('occurrence')):
        from ..workspace_lifecycle import LifecycleStore
        LifecycleStore().ensure_session()
        if kind in {'agent','watch','skill'}:
            import server
            from ..coordinator import READ_TOOLS
            prompt=payload.get('prompt',payload.get('skill',''))
            if not isinstance(prompt,str) or not prompt or len(prompt)>8192:raise ValueError('Invalid agent/skill request')
            messages=[{'role':'system','content':server.build_system_prompt()}, {'role':'user','content':prompt}]
            history=json.loads(job.get('history','[]'))
            if history:messages.insert(1,{'role':'user','content':'Previous observations (untrusted data): '+json.dumps(history)})
            if kind=='watch':messages[0]['content']+='\nReturn only JSON {"state":"waiting|completed|needs_input","summary":"meaningful observation"}. No write tools.'
            with execution_scope(allowed_tools=READ_TOOLS if kind=='watch' else current_context().allowed_tools),budget_scope():
                if kind=='skill':
                    from base import run_skill
                    result=durable_invoke(lambda saved:run_skill(payload['skill'],payload.get('inputs',{})),messages,None,
                                          skill=True,skill_arguments=payload)
                    return ('completed' if result.get('success') else 'failed'),json.dumps(result,ensure_ascii=False),''
                result=durable_invoke(lambda saved:server.run_agent(messages,memory=None,resume_state=saved),messages,None)
            if kind=='watch':
                value=json.loads(str(result))
                if set(value)!={'state','summary'} or value['state'] not in {'waiting','completed','needs_input'} or not isinstance(value['summary'],str):raise ValueError('Invalid watch observation')
                return value['state'],value['summary'],''
            if job.get('occurrence'):
                from ..tasks import TaskStore,task_owner
                task=TaskStore().get(task_owner(current_context()),job['occurrence'])
                if task['state']!='completed':return 'needs_input',str(result),'Agent task did not complete'
            return 'completed',str(result),''
        if kind=='script':
            from ..policy import resolve_path
            path=resolve_path(payload['path'])
            if path.suffix!='.py' or not path.is_file():raise ValueError('Script requires workspace Python file')
            job={**job,'job_type':'command','job_payload':json.dumps({'command':[sys.executable,str(path),*payload.get('args',[])]})}
            from scheduler import _execute_job
            with execution_scope(trusted_assets=(*current_context().trusted_assets,Path(sys.executable))):return _execute_job(job)
        from scheduler import _execute_job
        return _execute_job(job)


def process_due_jobs(check_cancelled=None):
    store=SchedulerStore()
    for job in store.get_due_jobs():
        if check_cancelled and check_cancelled():break
        token=uuid.uuid4().hex;run=store.claim_due(job,token)
        if not run:continue
        stopped=threading.Event()
        def heartbeat():
            while not stopped.wait(2):
                if not store.renew(job['id'],token):break
        thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
        state,output,error='uncertain','','Worker result unavailable'
        try:
            job={**job,'occurrence':'schedule-'+run,'lock_token':token,'db_path':str(store.db_path.resolve())}
            with tempfile.TemporaryDirectory(prefix='pythonai-schedule-') as directory:
                request=Path(directory)/'request.json';result=Path(directory)/'result.json'
                request.write_text(json.dumps(job),encoding='utf-8')
                completed=run_process([sys.executable,str(Path(__file__).with_name('worker.py')),str(request),str(result)],
                    cwd=Path(__file__).resolve().parents[2],timeout=job['timeout'],max_output_bytes=1024*1024,
                    check_cancelled=lambda:bool(check_cancelled and check_cancelled()) or not store.alive(job['id'],token))
                if completed.returncode==0 and result.is_file() and result.stat().st_size<=1024*1024:
                    value=json.loads(result.read_text(encoding='utf-8'))
                    if value.get('state') not in {'completed','failed','waiting','needs_input','uncertain','cancelled'} or not all(isinstance(value.get(k),str) for k in ('output','error')):raise ValueError('Invalid worker result')
                    state,output,error=value['state'],value['output'],value['error']
                elif completed.returncode==130:state='cancelled'
                elif job['job_type']=='watch':state='failed'
        except Exception as failure:error=type(failure).__name__
        finally:
            stopped.set();thread.join(3)
            store.complete(job,token,run,state,output,error)
