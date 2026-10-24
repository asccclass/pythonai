"""Owner-scoped native Schedule bridge. SQLite is the authority; RPCs reconcile it."""
from contextlib import closing, contextmanager, nullcontext
from dataclasses import replace
from datetime import timedelta, datetime
import asyncio
import json
from pathlib import Path
import os
import sqlite3
import time
import uuid

from agent_execution.policy import current_context, execution_scope
from agent_execution.tasks import task_owner, assert_owner_active
from agent_execution.scheduling.store import SchedulerStore, validate_job, next_time
from permissions import current_role, get_user_role
from .config import Settings
from .store import Store, canonical, digest

WORKFLOW = 'PythonAIScheduleOccurrenceV1'
QUEUE_SUFFIX = '-schedules-v1'
RPC_TIMEOUT = timedelta(seconds=10)


class Schedules:
    def __init__(self, scheduler=None, tasks=None):
        self.scheduler = scheduler or SchedulerStore()
        self.tasks = tasks or Store()
        with closing(self.tasks.tasks.connect()) as conn,conn:
            conn.execute('CREATE TABLE IF NOT EXISTS temporal_schedule_host(id INTEGER PRIMARY KEY CHECK(id=1),path TEXT NOT NULL)')
            conn.execute('INSERT OR IGNORE INTO temporal_schedule_host VALUES(1,?)',(str(self.scheduler.db_path.resolve()),))
            if conn.execute('SELECT path FROM temporal_schedule_host WHERE id=1').fetchone()[0]!=str(self.scheduler.db_path.resolve()):
                raise PermissionError('Schedule database configuration changed')
        with closing(self.scheduler.connect()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS temporal_schedule_bindings(
                    job_id TEXT PRIMARY KEY, address TEXT NOT NULL, namespace TEXT NOT NULL,
                    queue TEXT NOT NULL, overlap TEXT NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 1, applied INTEGER NOT NULL DEFAULT 0,
                    remote_deleted INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS temporal_schedule_occurrences(
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, workflow_id TEXT NOT NULL UNIQUE,
                    task_id TEXT NOT NULL UNIQUE, submission_id TEXT, state TEXT NOT NULL, deadline REAL NOT NULL,
                    remote_deleted INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS temporal_schedule_rpc_locks(
                    job_id TEXT PRIMARY KEY, token TEXT NOT NULL, pid INTEGER NOT NULL, process TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS temporal_erased_schedules(
                    token TEXT PRIMARY KEY, receipt TEXT NOT NULL);
            """)
            conn.execute('BEGIN IMMEDIATE')
            columns={r['name'] for r in conn.execute('PRAGMA table_info(temporal_schedule_bindings)')}
            if 'enabled' not in columns:
                conn.execute('ALTER TABLE temporal_schedule_bindings ADD COLUMN enabled INTEGER NOT NULL DEFAULT 0')
                conn.execute('UPDATE temporal_schedule_bindings SET enabled=(SELECT is_enabled FROM scheduled_jobs WHERE id=job_id)')
            for column,definition in [('retired','INTEGER NOT NULL DEFAULT 0'),('start_at','TEXT')]:
                if column not in columns:conn.execute('ALTER TABLE temporal_schedule_bindings ADD COLUMN '+column+' '+definition)
            # Also fence schedulers which loaded the pre-TP19 claim query.
            conn.execute("UPDATE scheduled_jobs SET is_enabled=0 WHERE execution_backend='temporal'")

    @property
    def queue(self):
        return self.tasks.queue + QUEUE_SUFFIX

    def _row(self, key, conn):
        row = conn.execute("""SELECT j.*, b.address,b.namespace,b.queue,b.overlap,b.cancelled,
            b.revision,b.applied,b.remote_deleted,b.enabled,b.retired,b.start_at FROM scheduled_jobs j
            JOIN temporal_schedule_bindings b ON b.job_id=j.id WHERE j.id=?""", (key,)).fetchone()
        if row is None:
            raise KeyError('Temporal schedule not found')
        value=dict(row)
        value['is_enabled']=value['enabled']
        return value

    def get(self, key):
        with closing(self.scheduler.connect()) as conn:
            row = self._row(key, conn)
        if row['owner'] != task_owner(current_context()):
            raise KeyError('Temporal schedule not found in this scope')
        return row

    def list(self):
        owner = task_owner(current_context())
        with closing(self.scheduler.connect()) as conn:
            keys = [r[0] for r in conn.execute("""SELECT j.id FROM scheduled_jobs j
                JOIN temporal_schedule_bindings b ON b.job_id=j.id WHERE j.owner=? LIMIT 1000""", (owner,))]
            return [self._row(key, conn) for key in keys]

    def authority(self, row):
        self.configuration(row)
        scope = json.loads(row['context'])
        if (scope.get('source') not in {'local','api','websocket','communication'}
                or scope.get('lifecycle_owner') or scope.get('coordinator_owner')):
            raise PermissionError('Delegated schedules are not enabled')
        from agent_execution.policy import ExecutionContext
        if task_owner(ExecutionContext(**scope)) != row['owner']:
            raise PermissionError('Schedule owner changed')
        if scope['identity'] == 'local':
            if scope['source'] != 'local':
                raise PermissionError('Remote schedule cannot claim local authority')
            role = row['owner_role']
        else:
            role = get_user_role(scope['identity'])
        if role != 'admin' or row['owner_role'] != 'admin':
            raise PermissionError('Schedule owner is no longer authorized')
        with closing(self.tasks.tasks.connect()) as conn:
            assert_owner_active(conn, row['owner'])
        with execution_scope(workspace=None, repository_workspace=None):
            from agent_execution.sessions import project_workspace
            configured = project_workspace(scope['project']).resolve()
        if Path(scope['workspace']).resolve() != configured:
            raise PermissionError('Schedule workspace changed')
        scope.pop('lifecycle_owner', None)
        from .contracts import READ_TOOLS
        scope['allowed_tools'] = sorted(READ_TOOLS if scope.get('allowed_tools') is None else READ_TOOLS & frozenset(scope['allowed_tools']))
        return scope

    def configuration(self,row):
        settings=Settings.load()
        if (row.get('address',settings.address),row.get('namespace',settings.namespace),row.get('queue',self.queue)) != (settings.address,settings.namespace,self.queue):
            raise PermissionError('Schedule connection/host configuration changed')

    def create(self, name, cron, kind, payload, *, timezone='Asia/Taipei',
               timeout=60, max_runs=1000, overlap='SKIP', paused=True):
        if current_role() != 'admin':
            raise PermissionError('Schedule management requires admin')
        if kind not in {'agent','watch'}:
            raise ValueError('Temporal schedules currently support read-only agent/watch')
        if overlap not in {'SKIP','BUFFER_ONE'} or (kind == 'watch' and overlap != 'SKIP'):
            raise ValueError('Use SKIP for watch; agent supports SKIP or BUFFER_ONE')
        if type(paused) is not bool:
            raise ValueError('paused must be boolean')
        validate_job(name, cron, kind, payload, timezone, timeout, max_runs)
        if set(payload) != {'prompt'} or not isinstance(payload['prompt'], str) or not 1 <= len(payload['prompt']) <= 8192:
            raise ValueError('Expected only a bounded prompt')
        settings = Settings.load()
        from .admission import new_backend
        if new_backend(task_owner(current_context()), 'temporal') != 'temporal':
            raise PermissionError('Owner is outside the Temporal trial')
        context = current_context()
        scope = {k:getattr(context,k) for k in ('source','identity','session','project','conversation','lifecycle_owner','coordinator_owner')}
        from agent_execution.policy import resolve_path
        scope['workspace'] = str(resolve_path('.'))
        scope['allowed_tools'] = None if context.allowed_tools is None else sorted(context.allowed_tools)
        self.authority({'context':canonical(scope),'owner':task_owner(context),'owner_role':current_role()})
        from agent_execution.workspace_lifecycle import LifecycleStore
        LifecycleStore().ensure_session()
        # Orphans after a local crash are never claimable by the legacy engine.
        key = self.scheduler.add_job(name, cron, kind, payload, timezone=timezone,
            timeout=timeout, max_runs=max_runs, execution_backend='temporal')
        with closing(self.scheduler.connect()) as conn, conn:
            conn.execute('UPDATE scheduled_jobs SET is_enabled=0 WHERE id=?', (key,))
            conn.execute('UPDATE scheduled_jobs SET context=? WHERE id=?',(canonical(scope),key))
            conn.execute('INSERT INTO temporal_schedule_bindings(job_id,address,namespace,queue,overlap,enabled,updated) VALUES(?,?,?,?,?,?,?)',
                (key,settings.address,settings.namespace,self.queue,overlap,int(not paused),time.time()))
        return key

    def change(self, key, action, **values):
        if current_role() != 'admin':
            raise PermissionError('Schedule management requires admin')
        row = self.get(key)
        from .schedule_migration import editable
        editable(self,row)
        if action not in {'update','pause','resume','cancel'}:
            raise ValueError('Unknown Schedule operation')
        if values and (action != 'update' or set(values)-{'cron','timezone','max_runs','timeout','overlap'}):
            raise ValueError('Unsupported Schedule update')
        if row['cancelled']:
            if action == 'cancel':
                self.cancel_tasks(key)
                return
            raise PermissionError('Cancelled Schedule cannot be revived')
        if action in {'resume','update'}:
            self.authority(row)
        cron, zone = values.get('cron',row['cron_expression']), values.get('timezone',row['timezone'])
        maximum, timeout = values.get('max_runs',row['max_runs']), values.get('timeout',row['timeout'])
        overlap = values.get('overlap',row['overlap'])
        validate_job(row['name'],cron,row['job_type'],json.loads(row['job_payload']),zone,timeout,maximum)
        if maximum < row['run_count'] or (action == 'resume' and maximum <= row['run_count']):
            raise ValueError('Schedule run limit already reached')
        if overlap not in {'SKIP','BUFFER_ONE'} or (row['job_type']=='watch' and overlap!='SKIP'):
            raise ValueError('Unsupported overlap policy')
        with closing(self.scheduler.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            current = self._row(key,conn)
            if current['revision'] != row['revision'] or current['run_count'] != row['run_count']:
                raise ValueError('Schedule changed; reload before updating')
            enabled = 0 if action in {'pause','cancel'} else 1 if action=='resume' else row['is_enabled']
            conn.execute('UPDATE scheduled_jobs SET cron_expression=?,timezone=?,max_runs=?,timeout=?,next_run_at=? WHERE id=?',
                (cron,zone,maximum,timeout,next_time(cron,zone),key))
            conn.execute('UPDATE temporal_schedule_bindings SET overlap=?,enabled=?,cancelled=?,revision=revision+1,updated=? WHERE job_id=?',
                (overlap,enabled,int(action=='cancel'),time.time(),key))
        if action == 'cancel':
            with closing(self.scheduler.connect()) as conn,conn:
                conn.execute("INSERT OR IGNORE INTO schedule_notification_delivery SELECT id,'cancelled',NULL,0,NULL,NULL FROM schedule_notifications WHERE job_id=?",(key,))
            self.cancel_tasks(key)

    def remote_id(self, row):
        return self.queue + '/' + row['id']

    def definition(self, row):
        from temporalio.client import (Schedule, ScheduleActionStartWorkflow, ScheduleSpec,
            SchedulePolicy, ScheduleState, ScheduleOverlapPolicy)
        # Temporal normalizes smaller values to ten seconds. With five-field cron,
        # this admits at most one recent firing; historical backfill is not exposed.
        return Schedule(
            ScheduleActionStartWorkflow(WORKFLOW, {'schedule_id':row['id']},
                id=self.remote_id(row), task_queue=self.queue,
                execution_timeout=timedelta(seconds=row['timeout']+60)),
            ScheduleSpec(cron_expressions=[row['cron_expression']],time_zone_name=row['timezone'],
                start_at=datetime.fromisoformat(row['start_at']) if row.get('start_at') else None),
            SchedulePolicy(overlap=ScheduleOverlapPolicy[row['overlap']],
                catchup_window=timedelta(seconds=10),pause_on_failure=True),
            ScheduleState(paused=not bool(row['is_enabled']),note='PythonAI owner-scoped Schedule'))

    @contextmanager
    def rpc_lock(self,key,kind='rpc'):
        from .references import process_stamp
        key=key+':'+kind
        token = uuid.uuid4().hex
        with closing(self.scheduler.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT pid,process FROM temporal_schedule_rpc_locks WHERE job_id=?',(key,)).fetchone()
            if old and process_stamp(old['pid'])==old['process']:
                raise RuntimeError('Schedule RPC is already in progress')
            conn.execute('INSERT OR REPLACE INTO temporal_schedule_rpc_locks VALUES(?,?,?,?)',
                (key,token,os.getpid(),process_stamp(os.getpid())))
        try:
            yield
        finally:
            with closing(self.scheduler.connect()) as conn,conn:
                conn.execute('DELETE FROM temporal_schedule_rpc_locks WHERE job_id=? AND token=?',(key,token))

    async def sync(self,key,client=None):
        with self.rpc_lock(key):
            with closing(self.scheduler.connect()) as conn:
                cancelled=self._row(key,conn)['cancelled']
            # A regular CRUD RPC cannot starve occurrence polling. Cancellation
            # additionally drains local metadata before erasure can proceed.
            with self.rpc_lock(key,'metadata') if cancelled else nullcontext():
                await self._sync(key,client)

    async def _sync(self, key, client=None):
        from .client import connect
        from temporalio.client import ScheduleAlreadyRunningError, ScheduleUpdate
        from temporalio.service import RPCError,RPCStatusCode
        with closing(self.scheduler.connect()) as conn:
            row = self._row(key,conn)
        settings = Settings.load()
        if (row['address'],row['namespace'],row['queue']) != (settings.address,settings.namespace,self.queue):
            raise PermissionError('Schedule connection/host configuration changed')
        client = client or await connect(settings)
        if client.namespace != row['namespace']:
            raise PermissionError('Wrong Schedule namespace')
        handle = client.get_schedule_handle(self.remote_id(row))
        if row['cancelled'] or row['retired']:
            if row['cancelled']:await asyncio.to_thread(self.cancel_tasks,key)
            try:
                await handle.delete(rpc_timeout=RPC_TIMEOUT)
            except RPCError as error:
                if error.status != RPCStatusCode.NOT_FOUND:
                    raise
            # Delete is verified, rather than inferring success from a lost reply.
            try:
                await handle.describe(rpc_timeout=RPC_TIMEOUT)
            except RPCError as error:
                if error.status != RPCStatusCode.NOT_FOUND:
                    raise
            else:
                raise RuntimeError('Schedule deletion has not become visible')
        else:
            if row['is_enabled']:
                self.authority(row)
            definition = self.definition(row)
            # New schedules always start paused, even if the create reply is lost.
            initial = replace(definition,state=replace(definition.state,paused=True))
            try:
                await client.create_schedule(self.remote_id(row),initial,rpc_timeout=RPC_TIMEOUT)
            except ScheduleAlreadyRunningError:
                pass
            def update(value):
                saved = value.description.schedule.action
                if (saved.workflow != WORKFLOW or saved.task_queue != self.queue
                        or saved.id != self.remote_id(row)):
                    raise PermissionError('Remote Schedule action changed')
                return ScheduleUpdate(schedule=definition)
            await handle.update(update,rpc_timeout=RPC_TIMEOUT)
        with closing(self.scheduler.connect()) as conn, conn:
            conn.execute('UPDATE temporal_schedule_bindings SET applied=?,remote_deleted=?,updated=? WHERE job_id=? AND revision=?',
                (row['revision'],int(bool(row['cancelled'] or row['retired'])),time.time(),key,row['revision']))

    def cancel_tasks(self,key):
        with closing(self.scheduler.connect()) as conn,conn:
            row = self._row(key,conn)
            ids = [r[0] for r in conn.execute('SELECT submission_id FROM temporal_schedule_occurrences WHERE job_id=? AND submission_id IS NOT NULL',(key,))]
            conn.execute("INSERT OR IGNORE INTO schedule_notification_delivery SELECT id,'cancelled',NULL,0,NULL,NULL FROM schedule_notifications WHERE job_id=?",(key,))
        for submission in ids:
            try:
                self.tasks.cancel(submission,row['owner'])
            except KeyError:
                pass

    def stop(self,key,occurrence_id):
        with self.rpc_lock(key,'metadata'):
            return self._stop(key,occurrence_id)

    def _stop(self,key,occurrence_id):
        with closing(self.scheduler.connect()) as conn:
            row = self._row(key,conn)
            occurrence = conn.execute('SELECT * FROM temporal_schedule_occurrences WHERE id=? AND job_id=?',(occurrence_id,key)).fetchone()
        if occurrence is None or occurrence['state'] not in {'reserved','running'}:
            return
        if occurrence['submission_id']:
            try:
                self.tasks.cancel(occurrence['submission_id'],row['owner'])
            except KeyError:
                pass
        self.finish(row,dict(occurrence),'cancelled','','Occurrence stopped; inspect before rescheduling')

    def prepare(self, key, workflow_id):
        with self.rpc_lock(key,'metadata'):
            return self._prepare(key,workflow_id)

    def _prepare(self, key, workflow_id):
        if not isinstance(workflow_id,str) or len(workflow_id)>1024:
            raise ValueError('Invalid occurrence')
        with closing(self.scheduler.connect()) as conn:
            row = self._row(key,conn)
        if not workflow_id.startswith(self.remote_id(row)+'-'):
            raise PermissionError('Occurrence does not belong to Schedule')
        scope = self.authority(row)
        from .admission import new_backend
        if new_backend(row['owner'],'temporal') != 'temporal':
            raise PermissionError('Schedule trial admission stopped')
        token = digest(workflow_id)
        task = 'schedule-'+token
        with closing(self.scheduler.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = self._row(key,conn)
            old = conn.execute('SELECT * FROM temporal_schedule_occurrences WHERE id=?',(token,)).fetchone()
            from .schedule_migration import admit_native
            if not admit_native(conn,row,workflow_id,token,bool(old)):
                return {'done':True}
            if row['cancelled']:
                raise PermissionError('Schedule cancelled')
            if not old:
                if not row['is_enabled'] or row['run_count']>=row['max_runs']:
                    return {'done':True}
                if conn.execute('SELECT count(*) FROM temporal_schedule_occurrences').fetchone()[0]>=10000:
                    raise ValueError('Schedule occurrence capacity reached')
                conn.execute("INSERT INTO temporal_schedule_occurrences(id,job_id,workflow_id,task_id,state,deadline,updated) VALUES(?,?,?,?,'reserved',?,?)",
                    (token,key,workflow_id,task,time.time()+row['timeout'],time.time()))
                conn.execute('UPDATE scheduled_jobs SET run_count=run_count+1 WHERE id=?',(key,))
                if row['run_count']+1>=row['max_runs']:
                    conn.execute('UPDATE temporal_schedule_bindings SET enabled=0,revision=revision+1 WHERE job_id=?',(key,))
            deadline=conn.execute('SELECT deadline FROM temporal_schedule_occurrences WHERE id=?',(token,)).fetchone()[0]
        # Deterministic task identity closes a crash between the two databases.
        existing = self.tasks.for_task(row['owner'],task)
        if existing is None:
            if self.tasks.bind(row['owner'],task,'temporal') != 'temporal':
                raise PermissionError('Occurrence backend refused')
            from agent_execution.budgets import TaskBudget
            TaskBudget(row['owner'],task,self.tasks.path)
            with closing(self.tasks.tasks.connect()) as conn,conn:
                conn.execute('BEGIN IMMEDIATE')
                if not conn.execute('SELECT 1 FROM temporal_submissions WHERE owner=? AND task_id=?',(row['owner'],task)).fetchone():
                    conn.execute('UPDATE task_budgets SET deadline=MIN(deadline,?) WHERE owner=? AND id=?',(deadline,row['owner'],task))
            prompt = json.loads(row['job_payload'])['prompt']
            messages = [{'role':'system','content':'Execute this read-only scheduled task using the permitted tools.'},
                        {'role':'user','content':prompt}]
            if row['job_type']=='watch':
                messages[0]['content'] += '\nReturn only JSON {"state":"waiting|completed|needs_input","summary":"meaningful observation"}.'
            settings = replace(Settings.load(),engine='bridge',file_writes=False)
            existing = self.tasks.submit(row['owner'],task,messages,scope,row['owner_role'],settings)
        with closing(self.scheduler.connect()) as conn,conn:
            conn.execute("UPDATE temporal_schedule_occurrences SET submission_id=?,state='running',updated=? WHERE id=? AND state='reserved'",
                (existing['id'],time.time(),token))
        # No task payloads cross the Workflow History boundary.
        return {'done':False,'occurrence_id':token,'submission_id':existing['id']}

    def poll(self, key, occurrence_id):
        with self.rpc_lock(key,'metadata'):
            return self._poll(key,occurrence_id)

    def _poll(self, key, occurrence_id):
        with closing(self.scheduler.connect()) as conn:
            row = self._row(key,conn)
            occurrence = conn.execute('SELECT * FROM temporal_schedule_occurrences WHERE id=? AND job_id=?',(occurrence_id,key)).fetchone()
        if occurrence is None:
            raise KeyError('Unknown occurrence')
        if row['cancelled']:
            self.cancel_tasks(key)
            return {'done':True}
        # Permission loss also stops an already admitted task before the next poll.
        try:
            self.authority(row)
        except PermissionError:
            self.tasks.cancel(occurrence['submission_id'],row['owner'])
            self.finish(row,dict(occurrence),'needs_input','','Owner/workspace is no longer authorized')
            return {'done':True}
        result = self.tasks.get(occurrence['submission_id'],row['owner'])
        if result['state'] not in {'completed','failed','cancelled','uncertain','erasing'}:
            return {'done':False}
        state, text, error = result['state'], '', ''
        if result['result']:
            text = str(json.loads(result['result']).get('reply',''))[:65536]
        if row['job_type']=='watch' and state=='completed':
            try:
                value = json.loads(text)
                if set(value)!={'state','summary'} or value['state'] not in {'waiting','completed','needs_input'} or not isinstance(value['summary'],str):
                    raise ValueError()
                state,text = value['state'],value['summary']
            except (ValueError,TypeError):
                state,error = 'needs_input','Invalid watch observation'
        self.finish(row,dict(occurrence),state,text,error)
        return {'done':True}

    def finish(self,row,occurrence,state,text,error=''):
        with closing(self.scheduler.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            current = self._row(row['id'],conn)
            if not conn.execute("UPDATE temporal_schedule_occurrences SET state=?,updated=? WHERE id=? AND state IN ('reserved','running')",
                (state,time.time(),occurrence['id'])).rowcount:
                return
            if current['cancelled']:
                return
            if current['job_type']=='watch' and state=='waiting' and current['run_count']>=current['max_runs']:
                state,error = 'needs_input','Watch run limit reached without completion'
                conn.execute('UPDATE temporal_schedule_occurrences SET state=? WHERE id=?',(state,occurrence['id']))
            evidence = (error+'\n'+text).strip()[:65536]
            fingerprint = digest([state,evidence])
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_schedule_migrations'").fetchone() and conn.execute(
                    'SELECT 1 FROM temporal_schedule_migrations WHERE job_id=?',(row['id'],)).fetchone():
                # A migrated job retains the legacy observation digest across both engines.
                import hashlib
                observation=text+('\n'+error[:1000] if state in {'failed','needs_input','uncertain'} else '')
                fingerprint=hashlib.sha256((state+'\n'+observation).encode()).hexdigest()
            if fingerprint!=current['last_digest'] and (current['last_digest'] is not None or state!='waiting'):
                self.scheduler.notify(conn,current,state,evidence)
            stop = state not in {'completed','waiting'} or (current['job_type']=='watch' and state=='completed') or current['run_count']>=current['max_runs']
            if stop and conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_schedule_migrations'").fetchone():
                conn.execute('UPDATE temporal_schedule_migrations SET restore_enabled=0 WHERE job_id=?',(row['id'],))
            history = (json.loads(current['history'])+[{'state':state,'summary':text[:4096]}])[-4:]
            conn.execute('UPDATE scheduled_jobs SET last_digest=?,history=? WHERE id=?',
                (fingerprint,canonical(history),row['id']))
            conn.execute('UPDATE temporal_schedule_bindings SET enabled=?,revision=revision+1,updated=? WHERE job_id=?',(int(current['is_enabled'] and not stop),time.time(),row['id']))


async def reconcile(schedules,client):
    """Bounded pending CRUD; offline intents stay durable."""
    deadline=time.monotonic()+10
    with closing(schedules.scheduler.connect()) as conn:
        keys = [r[0] for r in conn.execute("""SELECT job_id FROM temporal_schedule_bindings
            WHERE revision!=applied ORDER BY updated LIMIT 20""")]
    for key in keys:
        if time.monotonic()>=deadline:
            break
        try:
            await schedules.sync(key,client)
        except Exception as error:
            with closing(schedules.scheduler.connect()) as conn,conn:
                conn.execute('UPDATE temporal_schedule_bindings SET updated=? WHERE job_id=?',(time.time(),key))
            print(canonical({'temporal':'schedule_pending','error_type':type(error).__name__}),flush=True)
    from temporalio.client import WorkflowExecutionStatus
    from temporalio.service import RPCError,RPCStatusCode
    with closing(schedules.scheduler.connect()) as conn:
        active = [dict(r) for r in conn.execute("SELECT * FROM temporal_schedule_occurrences WHERE state IN ('reserved','running') ORDER BY updated LIMIT 20")]
    for occurrence in active:
        if time.monotonic()>=deadline:
            break
        try:
            with closing(schedules.scheduler.connect()) as conn:
                row = schedules._row(occurrence['job_id'],conn)
            if (row['address'],row['namespace'],row['queue']) != (Settings.load().address,client.namespace,schedules.queue):
                continue
            try:
                status = (await client.get_workflow_handle(occurrence['workflow_id']).describe(rpc_timeout=RPC_TIMEOUT)).status
            except RPCError as error:
                if error.status!=RPCStatusCode.NOT_FOUND:
                    raise
                status = None
            if status!=WorkflowExecutionStatus.RUNNING:
                await asyncio.to_thread(schedules.stop,occurrence['job_id'],occurrence['id'])
        except Exception as error:
            print(canonical({'temporal':'schedule_occurrence_pending','error_type':type(error).__name__}),flush=True)
        finally:
            with closing(schedules.scheduler.connect()) as conn,conn:
                conn.execute('UPDATE temporal_schedule_occurrences SET updated=? WHERE id=?',(time.time(),occurrence['id']))


def task_pending(conn,submission_id):
    """Retention must wait until the Schedule consumed its task result."""
    from .retention import tables
    if 'temporal_schedule_host' not in tables(conn):
        return False
    row=conn.execute('SELECT path FROM temporal_schedule_host WHERE id=1').fetchone()
    if not row:
        return False
    try:
        with closing(sqlite3.connect(Path(row[0]).as_uri()+'?mode=ro',uri=True,timeout=2)) as schedules:
            return schedules.execute("SELECT 1 FROM temporal_schedule_occurrences WHERE submission_id=? AND state IN ('reserved','running')",(submission_id,)).fetchone() is not None
    except sqlite3.Error:
        return True  # Missing/locked registry is not evidence that notification completed.
