"""Temporal manages DAG/Child Workflow state; all application authority remains local."""
import asyncio
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy,WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError,ApplicationError,CancelledError
from .dag_contracts import PARENT,CHILD,SNAPSHOT,RUN,FENCE,STOP,envelope,probe,node_result


async def metadata(name,value):
    return await workflow.execute_activity(name,value,start_to_close_timeout=timedelta(seconds=20),
        retry_policy=RetryPolicy(maximum_attempts=3))


@workflow.defn(name=CHILD)
class CoordinatorNode:
    @workflow.run
    async def run(self,value:dict)->dict:
        try:envelope(value,child=True)
        except (ValueError,TypeError):raise ApplicationError('InvalidDAGNode',non_retryable=True) from None
        try:
            # One process execution, no automatic retry after a lost Activity result.
            result=await workflow.execute_activity(RUN,value,start_to_close_timeout=timedelta(seconds=660),
                task_queue=workflow.info().task_queue+'-io',
                heartbeat_timeout=timedelta(seconds=10),cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                retry_policy=RetryPolicy(maximum_attempts=1))
            try:cancelled=node_result(result)['cancelled']
            except (ValueError,TypeError):raise ApplicationError('InvalidDAGNodeResult',non_retryable=True) from None
            if cancelled:raise CancelledError('DAGNodeCancelled')
        except BaseException:
            await metadata(FENCE,value)
            raise
        return value


@workflow.defn(name=PARENT)
class CoordinatorDAG:
    @workflow.run
    async def run(self,value:dict)->dict:
        try:envelope(value)
        except (ValueError,TypeError):raise ApplicationError('InvalidDAG',non_retryable=True) from None
        handles={}
        started=set()

        async def settle(cancel=False):
            selected=[h for h in handles.values() if not h.done()]
            if cancel:
                for handle in selected:handle.cancel()
            if selected:
                await asyncio.gather(*selected,return_exceptions=True)

        try:
            for _ in range(4096):
                raw=await metadata(SNAPSHOT,value)
                try:status=probe(raw)
                except (ValueError,TypeError):
                    raise ApplicationError('InvalidDAGProbe',non_retryable=True) from None
                if status['stop'] or status['done']:
                    await settle(cancel=status['stop'])
                    if status['done']:return value
                else:
                    active=sum(not h.done() for h in handles.values())
                    for node in status['ready']:
                        if active>=status['concurrency']:break
                        if node in started:continue
                        child={**value,'node_id':node}
                        handles[node]=await workflow.start_child_workflow(CHILD,child,
                            id='pythonai/dag/'+value['graph_id']+'/node/'+node,
                            task_queue=workflow.info().task_queue,
                            execution_timeout=timedelta(seconds=720),
                            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                            cancellation_type=workflow.ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
                            parent_close_policy=workflow.ParentClosePolicy.REQUEST_CANCEL)
                        started.add(node)
                        active+=1
                    # Consume errors: a failed Child never makes its node a success.
                    for node,handle in list(handles.items()):
                        if handle.done():
                            try:await handle
                            except BaseException:
                                await metadata(FENCE,{**value,'node_id':node})
                            del handles[node]
                await workflow.sleep(1)
            raise ApplicationError('DAGHistoryBoundReached',non_retryable=True)
        except BaseException:
            # Cancellation fences local publication before asking children to stop.
            await metadata(STOP,value)
            await settle(cancel=True)
            raise
