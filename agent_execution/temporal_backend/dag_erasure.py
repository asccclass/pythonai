"""Session erasure for opaque DAG/Child Workflow Histories, with live-process drains."""
from contextlib import closing
import json
import time
from .store import canonical,digest
from .dag import Graphs,busy
from .erasure import ErasurePending


def keys(conn,session):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_agent_graphs'").fetchone():return []
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='wl_session_owners'").fetchone():return []
    return [r[0] for r in conn.execute("""SELECT g.id FROM temporal_agent_graphs g
        JOIN wl_session_owners s ON s.task_owner=g.owner WHERE s.session=? ORDER BY g.id""",(session,))]


def preview(conn,owners):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_agent_graphs'").fetchone():return []
    result=[]
    for owner in owners:
        for row in conn.execute('SELECT id,root,address,namespace,queue FROM temporal_agent_graphs WHERE owner=? ORDER BY id',(owner,)):
            nodes=[r[0] for r in conn.execute('SELECT id FROM agent_nodes WHERE owner=? AND root=? ORDER BY id',(owner,row[1]))]
            item={'id':row[0],'address':row[2],'namespace':row[3],'queue':row[4],'nodes':nodes}
            from .dag_lifecycle import operation_ids
            operations=operation_ids(conn,row[0])
            if operations:item['operations']=operations
            result.append(item)
    return sorted(result,key=lambda r:r['id'])


async def erase(graphs,session):
    from datetime import timedelta
    from .client import connect
    from .config import Settings
    timeout=timedelta(seconds=10)
    with closing(graphs.coordinator.connect()) as conn:
        lifecycle=conn.execute('SELECT state FROM wl_sessions WHERE id=?',(session,)).fetchone()
        if not lifecycle or lifecycle[0] not in {'deleting','deleted'}:
            raise PermissionError('DAG erasure requires the committed session tombstone')
        selected=keys(conn,session)
    if not selected:return
    client=await connect(Settings.load())
    for key in selected:
        graph=graphs.get(key)
        graphs.configuration(graph)
        graphs.stop(key)
        with closing(graphs.coordinator.connect()) as conn:
            nodes=graphs.nodes(conn,graph)
            if busy(conn,[graph['owner']]) or any((n['expires'] or 0)>time.time() for n in nodes):
                raise ErasurePending('DAG local execution still needs to drain')
        targets=[graphs.workflow_id(key)]+[graphs.child_id(key,n['id']) for n in nodes]
        from .dag_lifecycle import Lifecycle,operation_ids
        with closing(graphs.coordinator.connect()) as conn:
            operations=operation_ids(conn,key)
        targets += [Lifecycle.workflow_id(operation) for operation in operations]
        for workflow_id in targets:
            await delete_history(client,graph['namespace'],workflow_id,timeout,terminate_running=True)
        with closing(graphs.coordinator.connect()) as conn,conn:
            if sorted(n['id'] for n in graphs.nodes(conn,graph))!=sorted(n['id'] for n in nodes):
                raise ErasurePending('DAG targets changed during erasure')
            if operation_ids(conn,key)!=operations:
                raise ErasurePending('DAG lifecycle targets changed during erasure')
            conn.execute('UPDATE temporal_agent_graphs SET remote_deleted=1,updated=? WHERE id=?',(time.time(),key))


async def delete_history(client,namespace,workflow_id,timeout,*,terminate_running=False):
    """Delete every run in a Workflow ID chain, including runs closed by Continue-As-New."""
    from temporalio.client import WorkflowExecutionStatus
    from temporalio.service import RPCError,RPCStatusCode
    from temporalio.api.workflowservice.v1 import DeleteWorkflowExecutionRequest
    from temporalio.api.common.v1 import WorkflowExecution

    run_ids=set()
    latest=client.get_workflow_handle(workflow_id)
    try:
        latest_description=await latest.describe(rpc_timeout=timeout)
        run_ids.add(latest_description.run_id)
    except RPCError as error:
        if error.status!=RPCStatusCode.NOT_FOUND:raise
    async for execution in client.list_workflows(
            query="WorkflowId = '"+workflow_id+"'",rpc_timeout=timeout):
        if execution.run_id:run_ids.add(execution.run_id)

    for run_id in sorted(run_ids):
        handle=client.get_workflow_handle(workflow_id,run_id=run_id)
        try:
            description=await handle.describe(rpc_timeout=timeout)
        except RPCError as error:
            if error.status==RPCStatusCode.NOT_FOUND:continue
            raise
        if description.status==WorkflowExecutionStatus.RUNNING:
            if not terminate_running:
                raise ErasurePending('DAG Workflow run is still open')
            await handle.terminate('Authorized session erasure',rpc_timeout=timeout)
        try:
            await client.workflow_service.delete_workflow_execution(DeleteWorkflowExecutionRequest(
                namespace=namespace,workflow_execution=WorkflowExecution(workflow_id=workflow_id,run_id=run_id)),
                timeout=timeout)
        except RPCError as error:
            if error.status!=RPCStatusCode.NOT_FOUND:raise
        try:
            await handle.describe(rpc_timeout=timeout)
        except RPCError as error:
            if error.status==RPCStatusCode.NOT_FOUND:continue
            raise
        raise ErasurePending('DAG History deletion pending')

    async for _ in client.list_workflows(
            query="WorkflowId = '"+workflow_id+"'",rpc_timeout=timeout):
        raise ErasurePending('DAG visibility deletion pending')


def guard(store,session):
    with closing(store.connect()) as conn:
        if not keys(conn,session):return
    from .client import bridge
    from agent_execution.coordinator import Coordinator
    bridge().call(erase(Graphs(Coordinator(store.path)),session),45,lambda:False)


def scrub(store,session):
    with closing(store.connect()) as conn,conn:
        selected=keys(conn,session)
        if not selected:return
        lifecycle=conn.execute('SELECT state FROM wl_sessions WHERE id=?',(session,)).fetchone()
        if not lifecycle or lifecycle[0] not in {'deleting','deleted'}:
            raise PermissionError('DAG erasure requires the committed session tombstone')
        for key in selected:
            graph=conn.execute('SELECT owner,remote_deleted FROM temporal_agent_graphs WHERE id=?',(key,)).fetchone()
            if not graph[1] or busy(conn,[graph[0]]):raise ErasurePending('DAG erasure proof is incomplete')
            conn.execute('INSERT OR IGNORE INTO temporal_erased_dags VALUES(?,?,?)',(digest(key),digest(session),canonical({
                'version':1,'history':'not_found_verified','visibility':'not_found_verified',
                'checked':time.time(),'archival':'external_policy_required','backups':'external_policy_required'})))
            conn.execute('DELETE FROM temporal_agent_runs WHERE graph_id=?',(key,))
            for table in ('temporal_agent_processes','temporal_dag_operations'):
                if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():
                    conn.execute(f'DELETE FROM {table} WHERE graph_id=?',(key,))
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_dag_gc'").fetchone():
                conn.execute('DELETE FROM temporal_dag_gc WHERE graph_id=?',(key,))
            conn.execute('DELETE FROM temporal_agent_graphs WHERE id=?',(key,))


def receipts(conn,session):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_erased_dags'").fetchone():return []
    return [{'graph_ref':r[0],'state':'remote_done','receipt':json.loads(r[1])} for r in conn.execute(
        'SELECT token,receipt FROM temporal_erased_dags WHERE session_hash=? ORDER BY token',(digest(session),))]
