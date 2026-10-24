"""Trusted per-job migration intents, UTC occurrence fences and restartable cutover."""
from contextlib import closing
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import sqlite3
import time
from zoneinfo import ZoneInfo
from croniter import croniter

from agent_execution.policy import current_context,ExecutionContext,execution_scope
from agent_execution.tasks import task_owner,assert_owner_active
from agent_execution.scheduling.store import scheduler_db_path,validate_job
from permissions import current_role,get_user_role
from .store import canonical,digest
from .config import Settings

FIELDS=('id','name','description','cron_expression','job_type','job_payload','owner','context',
    'owner_role','owner_identity','timezone','timeout','max_runs','run_count','retry_count',
    'next_run_at','is_enabled','execution_backend','last_digest','history')

def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

def utc(value):
    result=datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else value
    if not isinstance(result,datetime) or result.tzinfo is None:
        raise ValueError('An aware UTC occurrence time is required')
    return result.astimezone(timezone.utc)

def slot(value):
    return utc(value).isoformat()

def times(expression,zone,after,count=10):
    """Match wall-clock cron, reject gaps, and retain both distinct UTC folds."""
    if type(count) is not int or not 1<=count<=100:
        raise ValueError('Expected 1..100 occurrences')
    after=utc(after)
    zone=ZoneInfo(zone)
    # Include the wall-clock interval preceding a repeated hour, then sort UTC.
    start=after.astimezone(zone).replace(tzinfo=None)-timedelta(hours=26)
    iterator=croniter(expression,start)
    found=set()
    # Bound scans while covering repeated hours even for minute schedules.
    for _ in range(10000):
        wall=iterator.get_next(datetime)
        for fold in (0,1):
            aware=wall.replace(tzinfo=zone,fold=fold)
            instant=aware.astimezone(timezone.utc)
            if instant>after and instant.astimezone(zone).replace(tzinfo=None)==wall:
                found.add(instant)
        ordered=sorted(found)
        if len(ordered)>=count and wall.replace(tzinfo=zone).astimezone(timezone.utc)>ordered[count-1]+timedelta(hours=26):
            return [t.isoformat() for t in ordered[:count]]
    raise ValueError('Cron preview exceeds bounded occurrence scan')

def snapshot(row):
    return {k:row[k] for k in FIELDS}

def authorize(row,task_path=None):
    context=current_context()
    if current_role()!='admin' or row['owner']!=task_owner(context):
        raise PermissionError('Schedule is not in this admin owner scope')
    scope=json.loads(row['context'])
    saved=ExecutionContext(**scope)
    if task_owner(saved)!=row['owner'] or saved.source not in {'local','api','websocket','communication'}:
        raise PermissionError('Untrusted schedule context')
    if context.coordinator_owner or context.lifecycle_owner or saved.coordinator_owner or saved.lifecycle_owner:
        raise PermissionError('Delegated schedules cannot migrate')
    if row['owner_role']!='admin' or (saved.identity=='local' and saved.source!='local'):
        raise PermissionError('Invalid schedule authority')
    if saved.identity!='local' and get_user_role(saved.identity)!='admin':
        raise PermissionError('Schedule owner is no longer authorized')
    from agent_execution.sessions import project_workspace
    with execution_scope(workspace=None,repository_workspace=None):
        expected=project_workspace(saved.project).resolve()
    if Path(scope['workspace']).resolve()!=expected:
        raise PermissionError('Schedule workspace changed')
    if task_path is None:
        root=Path(os.environ.get('LOCALAPPDATA') or Path.home()/'.local'/'share')/'PythonAI'
        task_path=Path(os.environ.get('AGENT_TASK_DB') or root/'tasks.db')
    if Path(task_path).exists():
        with closing(sqlite3.connect(Path(task_path).resolve().as_uri()+'?mode=ro',uri=True)) as conn:
            assert_owner_active(conn,row['owner'])
            if 'temporal_schedule_host' in tables(conn):
                path=conn.execute('SELECT path FROM temporal_schedule_host WHERE id=1').fetchone()[0]
                if path!=str(scheduler_db_path().resolve()):
                    raise PermissionError('Schedule database configuration changed')

def admission(row):
    from .admission import new_backend
    if new_backend(row['owner'],'temporal')!='temporal':
        raise PermissionError('Owner is outside the Temporal trial')

def supported(row):
    payload=json.loads(row['job_payload'])
    validate_job(row['name'],row['cron_expression'],row['job_type'],payload,row['timezone'],row['timeout'],row['max_runs'],row.get('description') or '')
    if row['job_type'] not in {'agent','watch'} or set(payload)!={'prompt'} or not isinstance(payload['prompt'],str) or not 1<=len(payload['prompt'])<=8192:
        raise ValueError('Only the TP19 read-only agent/watch prompt contract can migrate')
    if row['retry_count']:
        raise ValueError('Drain the legacy retry before migration')
    if row['run_count']>=row['max_runs']:
        raise ValueError('Schedule run limit is exhausted')

def idle_legacy(conn,key):
    if conn.execute('SELECT 1 FROM scheduled_job_locks WHERE job_id=? AND expires_at>?',(key,time.time())).fetchone():
        raise RuntimeError('Legacy lease is active; drain it before migration')
    if conn.execute("SELECT 1 FROM scheduled_job_runs WHERE job_id=? AND status IN ('running','uncertain')",(key,)).fetchone():
        raise RuntimeError('Legacy run is active or uncertain; inspect before migration')

def preview(key,*,at=None):
    """Read-only, no schema, directories, SDK or service needed."""
    path=scheduler_db_path().resolve()
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as conn:
        conn.row_factory=sqlite3.Row
        row=conn.execute('SELECT * FROM scheduled_jobs WHERE id=?',(key,)).fetchone()
        if row is None:raise KeyError('Schedule not found')
        row=dict(row)
        row.setdefault('execution_backend','sqlite')
        authorize(row)
        supported(row)
        if row['execution_backend']!='sqlite':raise ValueError('Expected a SQLite schedule')
        idle_legacy(conn,key)
        value=snapshot(row)
        base=utc(at) if at is not None else datetime.now(timezone.utc)+timedelta(minutes=2)
        return {'id':key,'source_digest':digest(value),'owner_hash':digest(row['owner']),
            'timezone':row['timezone'],'run_count':row['run_count'],'max_runs':row['max_runs'],
            'old_next_run':row['next_run_at'],'base':base.isoformat(),
            'next_10':times(row['cron_expression'],row['timezone'],base),
            'backfill':'disabled','overlap':'SKIP','payload_digest':digest(json.loads(row['job_payload']))}

def install(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS temporal_schedule_migrations(
            job_id TEXT PRIMARY KEY,state TEXT NOT NULL,snapshot TEXT NOT NULL,source_digest TEXT NOT NULL,
            barrier TEXT NOT NULL,fence_at TEXT,next_run TEXT,generation INTEGER NOT NULL,restore_enabled INTEGER NOT NULL,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS schedule_occurrence_fences(
            job_id TEXT NOT NULL,slot TEXT NOT NULL,engine TEXT NOT NULL,reference TEXT NOT NULL,
            PRIMARY KEY(job_id,slot));
    """)

def migration(conn,key):
    if 'temporal_schedule_migrations' not in tables(conn):return None
    return conn.execute('SELECT * FROM temporal_schedule_migrations WHERE job_id=?',(key,)).fetchone()

def editable(schedules,row):
    with closing(schedules.scheduler.connect()) as conn:
        saved=migration(conn,row['id'])
        if saved and saved['state']!='temporal':
            raise PermissionError('Use the migration CLI to manage this transition or SQLite job')
    if row.get('retired'):
        raise PermissionError('Schedule has returned to SQLite')

def admit_native(conn,row,workflow_id,reference,existing):
    saved=migration(conn,row['id'])
    if row.get('retired'):return False
    if not saved:return True
    if existing:
        return saved['state'] in {'temporal','rolling_back'}
    if saved['state']!='temporal' or not row['is_enabled'] or row['run_count']>=row['max_runs']:
        return False
    scheduled=slot(workflow_id.rsplit('/',1)[-1][len(row['id'])+1:])
    if utc(scheduled)<utc(saved['barrier']):return False
    if conn.execute('SELECT count(*) FROM schedule_occurrence_fences').fetchone()[0]>=100000:
        raise ValueError('Occurrence fence capacity reached')
    return bool(conn.execute('INSERT OR IGNORE INTO schedule_occurrence_fences VALUES(?,?,?,?)',
        (row['id'],scheduled,'temporal',reference)).rowcount)

def admit_legacy(conn,row):
    saved=migration(conn,row['id'])
    if not saved:return True
    if saved['state'] not in {'sqlite','staged'}:return False
    when=slot(row['next_run_at'])
    if conn.execute('SELECT count(*) FROM schedule_occurrence_fences').fetchone()[0]>=100000:
        raise ValueError('Occurrence fence capacity reached')
    inserted=conn.execute('INSERT OR IGNORE INTO schedule_occurrence_fences VALUES(?,?,?,?)',(row['id'],when,'sqlite',when)).rowcount
    if not inserted:
        conn.execute('UPDATE scheduled_jobs SET is_enabled=0 WHERE id=?',(row['id'],))
    return bool(inserted)

class Migration:
    def __init__(self,schedules=None):
        if schedules is None:
            from .schedules import Schedules
            schedules=Schedules()
        self.schedules=schedules
        with closing(schedules.scheduler.connect()) as conn,conn:
            install(conn)

    def row(self,conn,key,*,operator=True):
        row=conn.execute('SELECT * FROM scheduled_jobs WHERE id=?',(key,)).fetchone()
        if row is None:raise KeyError('Schedule not found')
        row=dict(row)
        if operator:authorize(row,self.schedules.tasks.path)
        else:self.schedules.authority(self.schedules._row(key,conn))
        return row

    def stage(self,key,source_digest):
        # Original SQLite job remains claimable until the explicit cutover fence.
        s=self.schedules
        with closing(s.scheduler.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            row=self.row(conn,key)
            supported(row)
            admission(row)
            if row['execution_backend']!='sqlite' or digest(snapshot(row))!=source_digest:
                raise ValueError('Legacy schedule changed; preview again')
            idle_legacy(conn,key)
            old=migration(conn,key)
            if old and old['state']=='staged' and old['source_digest']==source_digest:return
            if old and old['state'] not in {'sqlite','staged'}:raise ValueError('Migration is already active')
            if old:self.schedules.configuration(s._row(key,conn))
            settings=Settings.load()
            barrier=(datetime.now(timezone.utc)+timedelta(minutes=2)).isoformat()
            conn.execute("""INSERT INTO temporal_schedule_bindings(job_id,address,namespace,queue,overlap,enabled,start_at,updated)
                VALUES(?,?,?,?,'SKIP',0,?,?) ON CONFLICT(job_id) DO UPDATE SET cancelled=0,retired=0,
                enabled=0,start_at=excluded.start_at,revision=revision+1,updated=excluded.updated""",
                (key,settings.address,settings.namespace,s.queue,barrier,time.time()))
            conn.execute("""INSERT INTO temporal_schedule_migrations VALUES(?,'staged',?,?,?,NULL,NULL,1,?,?)
                ON CONFLICT(job_id) DO UPDATE SET state='staged',snapshot=excluded.snapshot,source_digest=excluded.source_digest,
                barrier=excluded.barrier,fence_at=NULL,next_run=NULL,generation=generation+1,restore_enabled=excluded.restore_enabled,updated=excluded.updated""",
                (key,canonical(snapshot(row)),source_digest,barrier,row['is_enabled'],time.time()))

    def cutover(self,key):
        s=self.schedules
        with closing(s.scheduler.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            row=self.row(conn,key)
            saved=migration(conn,key)
            if not saved:raise ValueError('Stage the migration first')
            if saved['state'] in {'cutting_over','activating','temporal'}:return
            if saved['state']!='staged' or digest(snapshot(row))!=saved['source_digest']:
                raise ValueError('Legacy job changed; restage before cutover')
            idle_legacy(conn,key)
            supported(row)
            admission(row)
            # Strictly future start, no replay of an overdue legacy next_run.
            barrier=max(datetime.now(timezone.utc)+timedelta(minutes=1),utc(row['next_run_at']))
            conn.execute("UPDATE scheduled_jobs SET execution_backend='transition',is_enabled=0 WHERE id=?",(key,))
            conn.execute("UPDATE temporal_schedule_migrations SET state='cutting_over',barrier=?,fence_at=?,updated=? WHERE job_id=?",
                (barrier.isoformat(),datetime.now(timezone.utc).isoformat(),time.time(),key))
            conn.execute('UPDATE temporal_schedule_bindings SET enabled=0,start_at=?,revision=revision+1,updated=? WHERE job_id=?',
                (barrier.isoformat(),time.time(),key))

    def rollback(self,key):
        s=self.schedules
        with closing(s.scheduler.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            row=self.row(conn,key)
            saved=migration(conn,key)
            if not saved:raise ValueError('No migration exists')
            if saved['state'] in {'rolling_back','sqlite'}:return
            if saved['state'] not in {'staged','cutting_over','activating','temporal'}:raise ValueError('Invalid migration state')
            if saved['state']=='temporal' and not s._row(key,conn)['is_enabled']:
                conn.execute('UPDATE temporal_schedule_migrations SET restore_enabled=0 WHERE job_id=?',(key,))
            conn.execute("UPDATE scheduled_jobs SET execution_backend='transition',is_enabled=0 WHERE id=?",(key,))
            conn.execute("UPDATE temporal_schedule_migrations SET state='rolling_back',updated=? WHERE job_id=?",(time.time(),key))
            conn.execute('UPDATE temporal_schedule_bindings SET enabled=0,revision=revision+1,updated=? WHERE job_id=?',(time.time(),key))

    async def verify_times(self,key,client):
        from google.protobuf.timestamp_pb2 import Timestamp
        from temporalio.api.workflowservice.v1 import ListScheduleMatchingTimesRequest
        s=self.schedules
        with closing(s.scheduler.connect()) as conn:
            row=s._row(key,conn)
        base=utc(row['start_at'])
        expected=times(row['cron_expression'],row['timezone'],base)
        start,end=Timestamp(),Timestamp()
        start.FromDatetime(base)
        end.FromDatetime(utc(expected[-1])+timedelta(seconds=1))
        result=await client.workflow_service.list_schedule_matching_times(ListScheduleMatchingTimesRequest(
            namespace=row['namespace'],schedule_id=s.remote_id(row),start_time=start,end_time=end),
            timeout=timedelta(seconds=10))
        actual=[v.ToDatetime(tzinfo=timezone.utc).isoformat() for v in result.start_time if v.ToDatetime(tzinfo=timezone.utc)>base]
        if actual!=expected:raise RuntimeError('Temporal/legacy calendar mismatch; keep the job fenced')
        return expected

    async def apply(self,key,client=None):
        s=self.schedules
        from .client import connect
        client=client or await connect(Settings.load())
        with closing(s.scheduler.connect()) as conn:
            row=self.row(conn,key,operator=False)
            saved=dict(migration(conn,key) or {})
        if not saved:raise ValueError('No migration exists')
        state=saved['state']
        if state=='sqlite':return
        if state=='temporal':
            await s.sync(key,client)
            return
        if state=='activating':
            with closing(s.scheduler.connect()) as conn,conn:
                conn.execute('BEGIN IMMEDIATE')
                current=self.row(conn,key,operator=False)
                latest=migration(conn,key)
                if latest['state']!='activating':raise RuntimeError('Migration state changed')
                enabled=int(bool(latest['restore_enabled']) and current['run_count']<current['max_runs'])
                if enabled:admission(current)
                barrier=max(utc(latest['barrier']),datetime.now(timezone.utc)+timedelta(minutes=1))
                conn.execute('UPDATE temporal_schedule_migrations SET barrier=?,updated=? WHERE job_id=?',(barrier.isoformat(),time.time(),key))
                conn.execute('UPDATE temporal_schedule_bindings SET start_at=?,enabled=?,revision=revision+1,updated=? WHERE job_id=?',
                    (barrier.isoformat(),enabled,time.time(),key))
            await s.sync(key,client)
            with closing(s.scheduler.connect()) as conn,conn:
                conn.execute('BEGIN IMMEDIATE')
                self.row(conn,key,operator=False)
                latest=migration(conn,key)
                binding=s._row(key,conn)
                if latest['state']!='activating' or binding['applied']!=binding['revision']:
                    raise RuntimeError('Activation has not been reconciled')
                conn.execute("UPDATE temporal_schedule_migrations SET state='temporal',updated=? WHERE job_id=?",(time.time(),key))
            return
        await s.sync(key,client)  # Staged/transition schedules are always paused.
        if state in {'staged','cutting_over'}:
            await self.verify_times(key,client)
        if state=='staged':return
        if state=='cutting_over':
            with closing(s.scheduler.connect()) as conn,conn:
                conn.execute('BEGIN IMMEDIATE')
                current=self.row(conn,key,operator=False)
                admission(current)
                latest=migration(conn,key)
                if latest['state']!='cutting_over' or latest['generation']!=saved['generation']:
                    raise RuntimeError('Migration state changed')
                self.schedules.configuration(s._row(key,conn))
                conn.execute("UPDATE scheduled_jobs SET execution_backend='temporal',is_enabled=0 WHERE id=?",(key,))
                conn.execute("UPDATE temporal_schedule_migrations SET state='activating',updated=? WHERE job_id=?",(time.time(),key))
                conn.execute('UPDATE temporal_schedule_bindings SET enabled=?,revision=revision+1,updated=? WHERE job_id=?',
                    (int(bool(latest['restore_enabled']) and current['run_count']<current['max_runs']),time.time(),key))
            await self.apply(key,client)
            return
        if state!='rolling_back':raise ValueError('Unknown migration state')
        await self.drain(key,client)
        with closing(s.scheduler.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            latest=migration(conn,key)
            if latest['state']!='rolling_back':raise RuntimeError('Migration state changed')
            conn.execute('UPDATE temporal_schedule_bindings SET retired=1,revision=revision+1,updated=? WHERE job_id=?',(time.time(),key))
        await s.sync(key,client)  # Delete and verify NOT_FOUND before enabling SQLite.
        with closing(s.scheduler.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            latest=migration(conn,key)
            if latest['state']!='rolling_back':raise RuntimeError('Migration state changed')
            binding=s._row(key,conn)
            if not binding['remote_deleted'] or binding['applied']!=binding['revision']:
                raise RuntimeError('Schedule deletion has not been reconciled')
            original=json.loads(latest['snapshot'])
            current=self.row(conn,key,operator=False)
            last=conn.execute('SELECT MAX(slot) FROM schedule_occurrence_fences WHERE job_id=?',(key,)).fetchone()[0]
            after=max(datetime.now(timezone.utc),utc(last) if last else datetime.now(timezone.utc))
            due=times(original['cron_expression'],original['timezone'],after,1)[0]
            enabled=int(bool(latest['restore_enabled']) and current['run_count']<original['max_runs'])
            conn.execute("""UPDATE scheduled_jobs SET execution_backend='sqlite',is_enabled=?,next_run_at=?,
                cron_expression=?,timezone=?,timeout=?,max_runs=? WHERE id=?""",
                (enabled,due,original['cron_expression'],original['timezone'],original['timeout'],original['max_runs'],key))
            conn.execute("UPDATE temporal_schedule_migrations SET state='sqlite',next_run=?,updated=? WHERE job_id=?",(due,time.time(),key))

    async def drain(self,key,client):
        from temporalio.service import RPCError,RPCStatusCode
        from .references import blockers
        from .schedules import RPC_TIMEOUT
        s=self.schedules
        # Bindings/slots persist, so neither engine can forget an unknown occurrence.
        with closing(s.scheduler.connect()) as conn:
            row=s._row(key,conn)
            occurrences=[dict(r) for r in conn.execute('SELECT * FROM temporal_schedule_occurrences WHERE job_id=?',(key,))]
        for occurrence in occurrences:
            if occurrence['state'] in {'reserved','running','uncertain'}:
                raise RuntimeError('Temporal occurrence is active or uncertain; drain before rollback')
            if occurrence['submission_id']:
                try:
                    task=s.tasks.get(occurrence['submission_id'],row['owner'])
                except KeyError:
                    task=None  # TP18 verified erasure already removed this terminal task.
                if task:
                    if task['state'] not in {'completed','failed','cancelled'}:
                        raise RuntimeError('Task needs inspection before rollback')
                    with closing(s.tasks.tasks.connect()) as conn,conn:
                        if blockers(conn,task['id']):raise RuntimeError('Local task reference is active')
                        if 'operation_ledger' in tables(conn) and conn.execute(
                            "SELECT 1 FROM operation_ledger WHERE owner=? AND task_id=? AND state IN ('running','uncertain')",
                            (task['owner'],task['task_id'])).fetchone():
                            raise RuntimeError('Task operation needs inspection')
                    await self.closed(client,task['workflow_id'])
            await self.closed(client,occurrence['workflow_id'])
        try:
            description=await client.get_schedule_handle(s.remote_id(row)).describe(rpc_timeout=RPC_TIMEOUT)
        except RPCError as error:
            if error.status==RPCStatusCode.NOT_FOUND:return
            raise
        for action in description.info.running_actions:
            if not action.workflow_id.startswith(s.remote_id(row)+'-'):
                raise PermissionError('Unexpected Schedule running action')
            await self.closed(client,action.workflow_id)

    async def closed(self,client,workflow_id):
        from temporalio.client import WorkflowExecutionStatus
        from temporalio.service import RPCError,RPCStatusCode
        from .schedules import RPC_TIMEOUT
        try:
            if (await client.get_workflow_handle(workflow_id).describe(rpc_timeout=RPC_TIMEOUT)).status==WorkflowExecutionStatus.RUNNING:
                raise RuntimeError('Remote occurrence is still active')
        except RPCError as error:
            if error.status!=RPCStatusCode.NOT_FOUND:raise

async def reconcile(schedules,client):
    with closing(schedules.scheduler.connect()) as conn:
        if 'temporal_schedule_migrations' not in tables(conn):return
        keys=[r[0] for r in conn.execute("SELECT job_id FROM temporal_schedule_migrations WHERE state IN ('cutting_over','activating','rolling_back') ORDER BY updated LIMIT 10")]
    deadline=time.monotonic()+10
    for key in keys:
        if time.monotonic()>=deadline:break
        try:
            await Migration(schedules).apply(key,client)
        except Exception as error:
            with closing(schedules.scheduler.connect()) as conn,conn:
                conn.execute('UPDATE temporal_schedule_migrations SET updated=? WHERE job_id=?',(time.time(),key))
            print(canonical({'temporal':'schedule_migration_pending','error_type':type(error).__name__}),flush=True)
