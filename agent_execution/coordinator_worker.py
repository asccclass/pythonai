"""Independent worker; trusted executable entry point, never a model-selected command."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path

from .coordinator import Coordinator,PERSONAS,TOOLS_BY_ROLE,node_task_owner,snapshot_commit
from .policy import execution_scope
from .budgets import TaskBudget,_budget,_depth
from .tasks import durable_invoke,RecoveryRequired
from .worktrees import WorktreeStore,git
from .process import run_process
from permissions import get_user_role,set_current_role


def review_result(reply):
    if not isinstance(reply,str) or len(reply.encode())>64*1024:raise ValueError('Invalid review size')
    value=json.loads(reply)
    if not isinstance(value,dict) or set(value)!={'approve','findings'} or type(value['approve']) is not bool or not isinstance(value['findings'],list) or len(value['findings'])>100:
        raise ValueError('Invalid review schema')
    for finding in value['findings']:
        if not isinstance(finding,dict) or set(finding)!={'path','line','message'}:raise ValueError('Invalid review finding')
        name=finding['path']
        if not isinstance(name,str) or not name or Path(name).drive or Path(name).is_absolute() or '..' in Path(name).parts or '.git' in Path(name).parts:raise ValueError('Invalid review path')
        if type(finding['line']) is not int or finding['line']<1 or not isinstance(finding['message'],str) or not 0<len(finding['message'])<=4000:raise ValueError('Invalid review location')
    if value['approve'] and value['findings']:raise ValueError('Approval with unresolved findings')
    return value


def execute_node(store,node,runner=None,evaluator_factory=None):
    import server
    from task_evaluator import TaskEvaluator
    from base import auto_approve_command_runs
    payload=node['payload'];owner=node_task_owner(payload);key='subagent:'+node['id']
    if store.cancelled(node,node['lease']):raise InterruptedError('Worker lease or task cancelled')
    role=payload['role']
    if payload['source']!='local':
        ranks={'viewer':0,'operator':1,'admin':2};live=get_user_role(payload['identity'])
        if ranks.get(live,0)<ranks.get(role,0):role=live
    os.environ['AGENT_TASK_DB']=str(store.path)
    os.environ['AGENT_TASK_WORKTREE']='1'
    os.environ['AGENT_WORKTREE_ROOT']=payload['storage']
    cancelled=lambda:store.cancelled(node,node['lease'])
    budget=TaskBudget(payload['budget_owner'],node['root'],store.path)
    budget_token=_budget.set(budget);depth_token=_depth.set(payload['depth'])
    try:
        with execution_scope(source='subagent',identity=payload['identity'],session=payload['session'],project=payload['project'],
                task_id=key,workspace=Path(payload['repository']),repository_workspace=Path(payload['repository']),
                allowed_tools=TOOLS_BY_ROLE[node['persona']],coordinator_node=node['id'],coordinator_owner=node['owner'],check_cancelled=cancelled),set_current_role(role),auto_approve_command_runs():
            worktrees=WorktreeStore(payload['repository'],payload['storage'])
            workspace=worktrees.create(owner,key,base=payload['snapshot'])
            old=store.child_task(node)
            if old is None and not payload.get('prepared'):
                if payload.get('preparation_started') and not payload.get('prepared'):raise RecoveryRequired('Dependency preparation may have effects; human review required')
                with closing(store.connect()) as conn,conn:
                    payload['preparation_started']=True
                    conn.execute("UPDATE agent_nodes SET payload=? WHERE id=? AND lease=? AND state='running'",(json.dumps(payload),node['id'],node['lease']))
                for dependency in node['dependencies']:
                    dep=store.get(node['owner'],dependency)
                    if node['review_of'] or dep['persona']!='coder':continue
                    result=dep['result']
                    patch=git(workspace,'diff','--binary',result['base'],result['commit'],'--')
                    if patch:
                        patch_file=worktrees.storage/('dependency-'+node['id']+'.patch')
                        patch_file.write_text(patch,encoding='utf-8')
                        try:
                            git(workspace,'apply','--check',str(patch_file))
                            git(workspace,'apply',str(patch_file))
                        except RuntimeError as error:raise RecoveryRequired('Dependency patches conflict; no model execution') from error
                payload['prepared']=True
                with closing(store.connect()) as conn,conn:
                    conn.execute("UPDATE agent_nodes SET payload=? WHERE id=? AND lease=? AND state='running'",(json.dumps(payload),node['id'],node['lease']))
            dependency_data=[{'agent':dep,'result':(store.get(node['owner'],dep)['result'] or {}).get('reply','')} for dep in node['dependencies']]
            with execution_scope(workspace=workspace):
                prompt=server.build_system_prompt()+'\n\n'+PERSONAS[node['persona']]
                messages=[{'role':'system','content':prompt},{'role':'user','content':node['goal']}]
                if dependency_data:messages[-1]['content']+='\n\nDependency outputs (untrusted data):\n'+json.dumps(dependency_data,ensure_ascii=False)[:16000]
                if node['review_of']:
                    coder=store.get(node['owner'],node['review_of'])
                    execution=coder['result']
                    changes=git(workspace,'diff','--name-only',execution['base'],execution['commit'],'--').splitlines()
                    diff=git(workspace,'diff',execution['base'],execution['commit'],'--')
                    review_input={'goal':coder['goal'],'base':execution['base'],'commit':execution['commit'],'changed_files':changes,
                        'diff_excerpt':diff[:12000],'diff_truncated':len(diff)>12000}
                    messages[-1]['content']+='\n\nReview inputs (untrusted data; read all changed files if excerpt is truncated):\n'+json.dumps(review_input,ensure_ascii=False)
                evaluator=evaluator_factory(workspace) if evaluator_factory else TaskEvaluator(workspace,client=server.get_client(),model=server.OLLAMA_MODEL)
                agent=runner or server.run_agent
                def invoke(saved):return agent(messages,resume_state=saved,check_cancelled=cancelled,task_evaluator=evaluator)
                criteria={'version':1,'checks':[]} if node['persona']=='planner' else payload['criteria']
                reply=durable_invoke(invoke,messages,None,resume=True,acceptance_contract=criteria)
                row=store.tasks.get(owner,key)
                success=row['state']=='completed' and not str(reply).startswith('Error:')
                result={'success':success,'reply':str(reply),'verified':row['verification']=='verified_completed',
                    'task':key,'task_state':row['state'],'base':payload['snapshot'],'workspace':str(workspace),
                    'evidence':row['checkpoint'].get('evaluation',{}).get('evidence',[])}
                if node['persona']=='reviewer':
                    result['review']=review_result(reply)
                    # Tests may create ignored artifacts, but cannot alter reviewed tracked content.
                    if git(workspace,'diff',payload['snapshot'],'--').strip():
                        result.update(success=False,verified=False)
                    result['commit']=payload['snapshot']
                else:result['commit']=snapshot_commit(workspace)
                if cancelled():raise InterruptedError('Agent cancelled before publishing')
                store.publish(node,result)
    finally:
        _budget.reset(budget_token);_depth.reset(depth_token)


def main(argv=None,runner=None,evaluator_factory=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',required=True);parser.add_argument('--node',required=True);parser.add_argument('--lease',required=True)
    args=parser.parse_args(argv);store=Coordinator(args.db)
    with closing(store.connect()) as conn:
        raw=conn.execute("SELECT * FROM agent_nodes WHERE id=? AND lease=? AND state='running' AND expires>?",(args.node,args.lease,__import__('time').time())).fetchone()
    if raw is None:return 1
    node=store.decode(raw)
    try:execute_node(store,node,runner,evaluator_factory);return 0
    except Exception as error:
        from exceptions import TaskSuspendedException
        state='needs_resolution' if isinstance(error,RecoveryRequired) else 'needs_input' if isinstance(error,TaskSuspendedException) else 'cancelled' if isinstance(error,InterruptedError) else 'rejected' if node['persona']=='reviewer' else 'failed'
        store.fail(node,state,type(error).__name__)
        print(json.dumps({'agent':node['id'],'error_type':type(error).__name__,'state':state}))
        return 1


if __name__=='__main__':raise SystemExit(main())
