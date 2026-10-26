"""Restartable Temporal erasure: confirm remote closure before deleting local references."""
from contextlib import closing
from datetime import timedelta
import json
import re
import time

from .store import canonical,digest
from .retention import tables


class ErasurePending(RuntimeError):
    pass



def verified_receipt(job):
    value = json.loads(job['receipt'])
    if (not isinstance(value,dict) or type(value.get('version')) is not int or value['version']!=1
            or value.get('history')!='not_found_verified' or value.get('visibility')!='not_found_verified'):
        raise ErasurePending('Remote erasure receipt is missing or unsupported')
    return value

def pending(store,session=None):
    with closing(store.tasks.connect()) as conn:
        if 'temporal_erasures' not in tables(conn):
            return []
        return [dict(r) for r in conn.execute("""SELECT e.*,s.owner,s.task_id,s.workflow_id,s.context,
            s.address,s.namespace,s.queue FROM temporal_erasures e JOIN temporal_submissions s
            ON s.id=e.submission_id WHERE e.state!='local_done'"""+
            (' AND e.session=?' if session is not None else " AND (e.state='pending' OR e.reason!='session')")+' ORDER BY e.updated'+(' LIMIT 100' if session is None else ''),
            (session,) if session is not None else ())]


async def remote(store,key,client=None):
    from temporalio.client import WorkflowExecutionStatus
    from temporalio.service import RPCError,RPCStatusCode
    from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest,DeleteWorkflowExecutionRequest
    from temporalio.api.common.v1 import WorkflowExecution
    row = store.get(key)
    with closing(store.tasks.connect()) as conn:
        job = conn.execute('SELECT * FROM temporal_erasures WHERE submission_id=?',(key,)).fetchone()
        if not job or row['state']!='erasing':
            raise PermissionError('No committed erasure fence')
        if job['state']=='remote_done':
            verified_receipt(job)
            return
        reason = job['reason']
    expected = 'pythonai/'+digest(row['owner'])[:24]+'/'+key
    if (not re.fullmatch('[a-f0-9]{32}',key) or row['workflow_id']!=expected
            or not re.fullmatch(r'(127\.0\.0\.1|localhost):[0-9]{1,5}',row['address'])
            or not 1<=int(row['address'].rsplit(':',1)[1])<=65535):
        raise PermissionError('Untrusted erasure target')
    store.workflow_type(row)  # Verify persisted protocol/host queue before any RPC.
    if client is None:
        from .client import connect
        client = await connect(store.settings(row))
    if client.namespace!=row['namespace']:
        raise PermissionError('Erasure namespace differs from persisted target')
    timeout = timedelta(seconds=5)
    namespace = await client.workflow_service.describe_namespace(
        DescribeNamespaceRequest(namespace=row['namespace']),timeout=timeout)
    handle = client.get_workflow_handle(row['workflow_id'])
    status,run = 'NOT_FOUND',None
    try:
        description = await handle.describe(rpc_timeout=timeout)
        run = description.run_id
        handle = client.get_workflow_handle(row['workflow_id'],run_id=run)
        if description.status==WorkflowExecutionStatus.RUNNING:
            if reason not in {'session','deleted_session'}:
                raise ErasurePending('Retention is waiting for remote closure')
            await handle.cancel(rpc_timeout=timeout)
            await handle.terminate('Authorized session erasure',rpc_timeout=timeout)
            description = await handle.describe(rpc_timeout=timeout)
        if description.status==WorkflowExecutionStatus.RUNNING:
            raise ErasurePending('Remote Workflow is still running')
        status = description.status.name
    except RPCError as error:
        if error.status!=RPCStatusCode.NOT_FOUND:
            raise
    from .references import blockers
    with closing(store.tasks.connect()) as conn,conn:
        if blockers(conn,key):
            raise ErasurePending('Local Activity, dispatch or publication still holds a reference')
    if run is not None:
        if status=='NOT_FOUND':
            try:
                confirmed = await handle.describe(rpc_timeout=timeout)
                if confirmed.status==WorkflowExecutionStatus.RUNNING:
                    raise ErasurePending('Remote closure has not been confirmed')
                status = confirmed.status.name
            except RPCError as error:
                if error.status!=RPCStatusCode.NOT_FOUND:
                    raise
        try:
            await client.workflow_service.delete_workflow_execution(DeleteWorkflowExecutionRequest(
                namespace=row['namespace'],workflow_execution=WorkflowExecution(
                    workflow_id=row['workflow_id'],run_id=run)),timeout=timeout)
        except RPCError as error:
            if error.status!=RPCStatusCode.NOT_FOUND:
                raise
    try:
        await handle.describe(rpc_timeout=timeout)
    except RPCError as error:
        if error.status!=RPCStatusCode.NOT_FOUND:
            raise
    else:
        raise ErasurePending('Remote History deletion is not yet observable')
    async for _ in client.list_workflows(query="WorkflowId = '"+row['workflow_id']+"'",limit=1,rpc_timeout=timeout):
        raise ErasurePending('Visibility deletion is not yet observable')
    config = namespace.config
    receipt = {'version':1,'checked':time.time(),'workflow_digest':digest(row['workflow_id']),
        'namespace_digest':digest(row['namespace']),'remote_status':status,
        'history':'not_found_verified','visibility':'not_found_verified',
        'namespace_retention_seconds':config.workflow_execution_retention_ttl.seconds,
        'history_archival_state':config.history_archival_state,
        'visibility_archival_state':config.visibility_archival_state,
        'archival':'external_policy_required','backups':'external_policy_required'}
    with closing(store.tasks.connect()) as conn,conn:
        conn.execute("UPDATE temporal_erasures SET state='remote_done',receipt=?,updated=? WHERE submission_id=? AND state='pending'",
                     (canonical(receipt),time.time(),key))


def erase_approvals(row):
    path = json.loads(row['context']).get('approval_db')
    if path is None:
        return
    from agent_execution.approvals import ApprovalStore
    store = ApprovalStore()
    if str(store.path.resolve())!=path:
        raise PermissionError('Approval database changed during erasure')
    with closing(store.connect()) as conn,conn:
        conn.execute('BEGIN IMMEDIATE')
        if 'temporal_approval_bindings' not in tables(conn):
            return
        bindings = [(r[0],r[1]) for r in conn.execute('SELECT binding,approval_id FROM temporal_approval_bindings')
                    if json.loads(r[0])['submission_id']==row['id']]
        for binding,key in bindings:
            conn.execute('DELETE FROM temporal_approval_outbox WHERE approval_id=?',(key,))
            conn.execute('DELETE FROM approval_events WHERE approval_id=?',(key,))
            conn.execute('DELETE FROM approvals WHERE id=?',(key,))
            conn.execute('DELETE FROM temporal_approval_bindings WHERE binding=?',(binding,))


def purge(store,key):
    row = store.get(key)
    with closing(store.tasks.connect()) as conn,conn:
        job = conn.execute('SELECT * FROM temporal_erasures WHERE submission_id=?',(key,)).fetchone()
        from .references import blockers
        if not job or job['state']!='remote_done' or row['state']!='erasing' or blockers(conn,key):
            raise ErasurePending('Remote closure and local quiescence must precede GC')
        verified_receipt(job)
    erase_approvals(row)
    with closing(store.tasks.connect()) as conn,conn:
        conn.execute('BEGIN IMMEDIATE')
        job = conn.execute('SELECT * FROM temporal_erasures WHERE submission_id=?',(key,)).fetchone()
        if not job or job['state']!='remote_done' or blockers(conn,key):
            raise ErasurePending('Erasure changed before local GC')
        names = tables(conn)
        pair = (row['owner'],row['task_id'])
        if 'budget_rates' in names and 'budget_calls' in names:
            conn.execute('DELETE FROM budget_rates WHERE id IN (SELECT id FROM budget_calls WHERE owner=? AND task_id=?)',pair)
        for table,column in [('operation_ledger','task_id'),('temporal_artifacts','task_id'),
            ('temporal_profile_inputs','task_id'),('task_steps','task_id'),('task_workspaces','task_id'),
            ('budget_calls','task_id'),('task_budgets','id'),('execution_bindings','task_id'),('tasks','id')]:
            if table in names:
                conn.execute('DELETE FROM '+table+' WHERE owner=? AND '+column+'=?',pair)
        for table in ['temporal_step_runs','temporal_human_answers','temporal_approval_waits',
            'temporal_events','temporal_finalizations','temporal_file_writes','temporal_write_resolutions',
            'temporal_budget_contracts','temporal_cancellations','temporal_protocols']:
            if table in names:
                conn.execute('DELETE FROM '+table+' WHERE submission_id=?',(key,))
        conn.execute('DELETE FROM temporal_submissions WHERE id=?',(key,))
        conn.execute("UPDATE temporal_erased_tasks SET state='completed',receipt=?,updated=? WHERE token=?",
                     (job['receipt'],time.time(),job['token']))
        conn.execute("UPDATE temporal_erasures SET state='local_done',updated=? WHERE submission_id=?",(time.time(),key))


async def reconcile(store,settings=None,queues=None,session=None):
    if session is None:
        with closing(store.tasks.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            from .retention import queue_deleted_sessions
            queue_deleted_sessions(conn)
    done = 0
    for job in pending(store,session)[:10]:
        if settings is not None and ((job['address'],job['namespace'])!=(settings.address,settings.namespace)
                                    or job['queue'] not in queues):
            continue
        try:
            await remote(store,job['submission_id'])
            if job['reason'] in {'retention','deleted_session'}:
                purge(store,job['submission_id'])
            done += 1
        except Exception as error:
            with closing(store.tasks.connect()) as conn,conn:
                conn.execute('UPDATE temporal_erasures SET updated=? WHERE submission_id=?',(time.time(),job['submission_id']))
            if settings is not None:
                print(canonical({'temporal':'erasure_pending','error_type':type(error).__name__}),flush=True)
            continue  # Rotate failed intents fairly; never persist payload/error text.
    return done


def ensure_session(store,session):
    jobs = pending(store,session)
    if any(j['state']!='remote_done' for j in jobs):
        from .client import bridge
        bridge().call(reconcile(store,session=session),30,lambda:False)
    with closing(store.tasks.connect()) as conn,conn:
        from .references import blockers
        jobs = pending(store,session)
        for job in jobs:
            if job['state']=='remote_done':
                verified_receipt(job)
        if any(j['state']!='remote_done' or blockers(conn,j['submission_id']) for j in jobs):
            raise ErasurePending('Temporal cleanup pending; restore its Worker/service and resume this frozen plan')



def guard_session(store,session):
    from .dag_erasure import guard as guard_dags
    guard_dags(store,session)
    with closing(store.connect()) as conn,conn:
        if 'temporal_submissions' not in tables(conn):
            return
        conn.execute('BEGIN IMMEDIATE')
        from .retention import prepare_session
        prepare_session(conn,session)
    from .store import Store
    from .schedule_erasure import guard as guard_schedules
    guard_schedules(Store(store.path),session)
    ensure_session(Store(store.path),session)


def session_receipts(store,session):
    with closing(store.connect()) as conn:
        from .dag_erasure import receipts
        dags=receipts(conn,session)
        if 'temporal_erasures' not in tables(conn):
            return dags
        return [{'submission_id':r[0],'state':r[1],'receipt':json.loads(r[2])}
            for r in conn.execute('SELECT submission_id,state,receipt FROM temporal_erasures WHERE session=? ORDER BY submission_id',(session,))]+dags

def finish_session(store,session):
    ensure_session(store,session)
    for job in pending(store,session):
        purge(store,job['submission_id'])
    from .schedule_erasure import scrub
    scrub(store,session)
