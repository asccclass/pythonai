"""Session erasure for native schedules and their reference-only wrapper Histories."""
from contextlib import closing
import re
import sqlite3
import time

from .store import canonical,digest
from .retention import tables
from .erasure import ErasurePending
from agent_execution.scheduling.store import scheduler_db_path

def keys_for_session(store,session):
    path = scheduler_db_path()
    registered=False
    with closing(store.tasks.connect()) as conn:
        if 'temporal_schedule_host' in tables(conn):
            configured=conn.execute('SELECT path FROM temporal_schedule_host WHERE id=1').fetchone()
            registered=bool(configured)
            if configured and configured[0]!=str(path.resolve()):
                raise PermissionError('Schedule database configuration changed; erasure retained')
        owners = [r[0] for r in conn.execute('SELECT task_owner FROM wl_session_owners WHERE session=?',(session,))]
    if not path.exists():
        if registered:
            raise ErasurePending('Registered Schedule database is missing')
        return []
    with closing(sqlite3.connect(path)) as conn:
        if 'temporal_schedule_bindings' not in tables(conn):
            return []
        return [key for owner in owners for key, in conn.execute("""SELECT j.id FROM scheduled_jobs j
            JOIN temporal_schedule_bindings b ON b.job_id=j.id WHERE j.owner=?""",(owner,))]

async def erase(store,session):
    from .schedules import Schedules,RPC_TIMEOUT
    from .client import connect
    from temporalio.client import WorkflowExecutionStatus
    from temporalio.service import RPCError,RPCStatusCode
    from temporalio.api.workflowservice.v1 import DeleteWorkflowExecutionRequest
    from temporalio.api.common.v1 import WorkflowExecution
    with closing(store.tasks.connect()) as conn:
        lifecycle=conn.execute('SELECT state FROM wl_sessions WHERE id=?',(session,)).fetchone()
        if not lifecycle or lifecycle[0] not in {'deleting','deleted'}:
            raise PermissionError('Schedule erasure requires the committed session tombstone')
    keys = keys_for_session(store,session)
    if not keys:
        return
    schedules = Schedules(tasks=store)
    for key in keys:
        with closing(schedules.scheduler.connect()) as conn,conn:
            row = schedules._row(key,conn)
            if not row['cancelled']:
                conn.execute('UPDATE temporal_schedule_bindings SET cancelled=1,revision=revision+1 WHERE job_id=?',(key,))
        schedules.cancel_tasks(key)
        await schedules.sync(key)
        with closing(schedules.scheduler.connect()) as conn:
            row = schedules._row(key,conn)
            occurrences = [dict(r) for r in conn.execute('SELECT * FROM temporal_schedule_occurrences WHERE job_id=? AND remote_deleted=0',(key,))]
        client = await connect(schedules.tasks.settings({'address':row['address'],'namespace':row['namespace']}))
        for occurrence in occurrences:
            workflow_id = occurrence['workflow_id']
            if not workflow_id.startswith(schedules.remote_id(row)+'-') or not re.fullmatch(r'[A-Za-z0-9_./:+-]+',workflow_id):
                raise PermissionError('Untrusted occurrence erasure target')
            handle = client.get_workflow_handle(workflow_id)
            try:
                description = await handle.describe(rpc_timeout=RPC_TIMEOUT)
                handle = client.get_workflow_handle(workflow_id,run_id=description.run_id)
                if description.status == WorkflowExecutionStatus.RUNNING:
                    await handle.terminate('Authorized session erasure',rpc_timeout=RPC_TIMEOUT)
                await client.workflow_service.delete_workflow_execution(DeleteWorkflowExecutionRequest(
                    namespace=row['namespace'],workflow_execution=WorkflowExecution(workflow_id=workflow_id,run_id=description.run_id)),
                    timeout=RPC_TIMEOUT)
            except RPCError as error:
                if error.status != RPCStatusCode.NOT_FOUND:
                    raise
            try:
                await handle.describe(rpc_timeout=RPC_TIMEOUT)
            except RPCError as error:
                if error.status != RPCStatusCode.NOT_FOUND:
                    raise
            else:
                raise ErasurePending('Schedule occurrence History deletion pending')
            async for _ in client.list_workflows(query="WorkflowId = '"+workflow_id+"'",limit=1,rpc_timeout=RPC_TIMEOUT):
                raise ErasurePending('Schedule occurrence Visibility deletion pending')
            with closing(schedules.scheduler.connect()) as conn,conn:
                conn.execute('UPDATE temporal_schedule_occurrences SET remote_deleted=1 WHERE id=?',(occurrence['id'],))

def guard(store,session):
    if not keys_for_session(store,session):
        return
    from .client import bridge
    bridge().call(erase(store,session),45,lambda:False)

def scrub(store,session):
    keys = keys_for_session(store,session)
    if not keys:
        return
    path = scheduler_db_path()
    with closing(sqlite3.connect(path)) as conn:
        reserved=[(r[0],r[1]) for key in keys for r in conn.execute('SELECT j.owner,o.task_id FROM temporal_schedule_occurrences o JOIN scheduled_jobs j ON j.id=o.job_id WHERE o.job_id=?',(key,))]
        for key in keys:
            binding=conn.execute('SELECT remote_deleted FROM temporal_schedule_bindings WHERE job_id=?',(key,)).fetchone()
            if not binding or not binding[0] or conn.execute('SELECT 1 FROM temporal_schedule_occurrences WHERE job_id=? AND remote_deleted=0',(key,)).fetchone():
                raise ErasurePending('Schedule remote erasure pending')
    with closing(store.tasks.connect()) as conn,conn:
        conn.execute('BEGIN IMMEDIATE')
        lifecycle=conn.execute('SELECT state FROM wl_sessions WHERE id=?',(session,)).fetchone()
        if not lifecycle or lifecycle[0] not in {'deleting','deleted'}:
            raise PermissionError('Schedule erasure requires the committed session tombstone')
        for owner,task in reserved:
            if conn.execute("SELECT 1 FROM execution_bindings WHERE owner=? AND task_id=? AND backend='temporal'",(owner,task)).fetchone():
                # Submission erasure already finished. These are binding/budget-only
                # reservations interrupted before Store.submit committed.
                if conn.execute('SELECT 1 FROM temporal_submissions WHERE owner=? AND task_id=?',(owner,task)).fetchone():
                    raise ErasurePending('Occurrence submission erasure pending')
                for table,column in [('budget_calls','task_id'),('task_budgets','id'),('execution_bindings','task_id')]:
                    if table in tables(conn):
                        conn.execute('DELETE FROM '+table+' WHERE owner=? AND '+column+'=?',(owner,task))
    with closing(sqlite3.connect(path)) as conn,conn:
        for key in keys:
            binding = conn.execute('SELECT remote_deleted FROM temporal_schedule_bindings WHERE job_id=?',(key,)).fetchone()
            if not binding or not binding[0] or conn.execute(
                    'SELECT 1 FROM temporal_schedule_occurrences WHERE job_id=? AND remote_deleted=0',(key,)).fetchone():
                raise ErasurePending('Schedule remote erasure pending')
            occurrences=[digest(r[0]) for r in conn.execute('SELECT workflow_id FROM temporal_schedule_occurrences WHERE job_id=?',(key,))]
            conn.execute('INSERT OR IGNORE INTO temporal_erased_schedules VALUES(?,?)',(digest(key),canonical({
                'version':1,'schedule':'not_found_verified','occurrence_history':'not_found_verified',
                'occurrence_visibility':'not_found_verified','occurrences':occurrences,'checked':time.time(),
                'archival':'external_policy_required','backups':'external_policy_required'})))
            conn.execute('DELETE FROM temporal_schedule_occurrences WHERE job_id=?',(key,))
            for table in ('schedule_occurrence_fences','temporal_schedule_migrations'):
                if table in tables(conn):conn.execute('DELETE FROM '+table+' WHERE job_id=?',(key,))
            conn.execute('DELETE FROM temporal_schedule_bindings WHERE job_id=?',(key,))
