from contextlib import closing
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
from croniter import croniter
from ..policy import current_context, policy_tool, resolve_path
from ..tasks import task_owner
from permissions import current_role


def now_utc(): return datetime.now(timezone.utc)
def scheduler_db_path():
    return Path(os.environ.get('SCHEDULER_DB_PATH', Path(__file__).resolve().parents[2]/'communication'/'scheduler.db'))


def natural_cron(text):
    if not isinstance(text,str): raise ValueError('Schedule must be text')
    match=re.fullmatch(r'(?:每天|every day at)\s*(\d{1,2}):(\d{2})',text.strip(),re.I)
    if match:
        hour,minute=map(int,match.groups())
        if hour<24 and minute<60:return f'{minute} {hour} * * *'
    match=re.fullmatch(r'(?:每\s*(\d+)\s*分鐘|every\s+(\d+)\s+minutes?)',text.strip(),re.I)
    if match:
        count=int(next(value for value in match.groups() if value))
        if 1<=count<=59:return f'*/{count} * * * *'
    raise ValueError('Supported schedules: 每天 HH:MM / every day at HH:MM / 每 N 分鐘 / every N minutes; otherwise supply cron')


def next_time(expression,zone,base=None):
    if not isinstance(expression,str) or len(expression)>128 or len(expression.split())!=5 or not croniter.is_valid(expression):
        raise ValueError('Expected a five-field cron expression')
    local=(base or now_utc()).astimezone(ZoneInfo(zone))
    return croniter(expression,local).get_next(datetime).astimezone(timezone.utc).isoformat()


def validate_job(name,expression,kind,payload,zone,timeout,max_runs,description=''):
    if not isinstance(name,str) or not 1<=len(name)<=200 or not isinstance(payload,dict) or not isinstance(description,str) or len(description)>2000:raise ValueError('Invalid name/payload')
    if kind not in {'command','script','agent','skill','watch','maintenance','maintenance.summary'}:raise ValueError('Unsupported job type')
    if type(timeout) not in {int,float} or not 1<=timeout<=300 or type(max_runs) is not int or not 1<=max_runs<=1000:raise ValueError('Invalid timeout/max_runs')
    serialized=json.dumps(payload,ensure_ascii=False,allow_nan=False)
    if len(serialized.encode())>65536:raise ValueError('Job payload exceeds 64 KiB')
    return serialized,next_time(expression,zone)


class SchedulerStore:
    def __init__(self,db_path=None):
        self.db_path=Path(db_path or scheduler_db_path());self.db_path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self.connect()) as conn,conn:
            conn.executescript('''CREATE TABLE IF NOT EXISTS scheduled_jobs (
              id TEXT PRIMARY KEY,name TEXT NOT NULL,description TEXT,cron_expression TEXT NOT NULL,
              job_type TEXT NOT NULL,job_payload TEXT NOT NULL,is_enabled BOOLEAN DEFAULT 1,
              created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,next_run_at TIMESTAMP);
              CREATE TABLE IF NOT EXISTS scheduled_job_runs (id TEXT PRIMARY KEY,job_id TEXT NOT NULL,status TEXT NOT NULL,
              started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,completed_at TIMESTAMP,output TEXT,error_message TEXT);
              CREATE TABLE IF NOT EXISTS scheduled_job_locks (job_id TEXT PRIMARY KEY,locked_by TEXT NOT NULL,
              locked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,expires_at TIMESTAMP NOT NULL);
              CREATE TABLE IF NOT EXISTS schedule_notifications (id TEXT PRIMARY KEY,job_id TEXT NOT NULL,owner TEXT NOT NULL,
              state TEXT NOT NULL,output TEXT NOT NULL,created REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS schedule_notification_delivery(id TEXT PRIMARY KEY,status TEXT NOT NULL,token TEXT,
              expires REAL,error_type TEXT,completed REAL);''')
            columns={row['name'] for row in conn.execute('PRAGMA table_info(scheduled_jobs)')}
            defaults={'owner_role':"TEXT NOT NULL DEFAULT 'admin'",'owner_identity':"TEXT NOT NULL DEFAULT 'local'",
                'owner':'TEXT','context':'TEXT','timezone':"TEXT NOT NULL DEFAULT 'UTC'",'timeout': 'REAL NOT NULL DEFAULT 60',
                'max_runs':'INTEGER NOT NULL DEFAULT 1000','run_count':'INTEGER NOT NULL DEFAULT 0',
                'retry_count':'INTEGER NOT NULL DEFAULT 0','last_digest':'TEXT','history':"TEXT NOT NULL DEFAULT '[]'"}
            for name,definition in defaults.items():
                if name not in columns:conn.execute(f'ALTER TABLE scheduled_jobs ADD COLUMN {name} {definition}')
            # Legacy remote jobs lack a trustworthy source/session: quarantine them.
            for row in conn.execute("SELECT id,owner_identity FROM scheduled_jobs WHERE owner IS NULL AND owner_identity!='local'").fetchall():
                identity=row['owner_identity']
                conn.execute('UPDATE scheduled_jobs SET owner=?,context=?,is_enabled=0 WHERE id=?',
                    (json.dumps(['legacy',identity,'default','unknown'],separators=(',',':')),
                     json.dumps({'source':'legacy','identity':identity,'project':'default','session':'unknown'}),row['id']))
            # Local CLI jobs retain local ownership.
            conn.execute('UPDATE scheduled_jobs SET owner=?,context=? WHERE owner IS NULL',
                (json.dumps(['local','local','default','local'],separators=(',',':')),
                 json.dumps({'source':'local','identity':'local','project':'default','session':'local'})))
            conn.execute("UPDATE scheduled_job_locks SET expires_at=CAST(strftime('%s',expires_at) AS REAL) WHERE typeof(expires_at)='text' AND expires_at LIKE '%-%'")

    def connect(self):
        if self.db_path.exists() and self.db_path.stat().st_size>64*1024*1024:raise ValueError('Scheduler DB exceeds 64 MiB')
        conn=sqlite3.connect(self.db_path,timeout=10);conn.row_factory=sqlite3.Row;return conn

    @policy_tool('schedule_job')
    def add_job(self,name,cron_expression,job_type,job_payload,description='',timezone='Asia/Taipei',timeout=60,max_runs=1000):
        serialized,due=validate_job(name,cron_expression,job_type,job_payload,timezone,timeout,max_runs,description)
        context=current_context();key=uuid.uuid4().hex
        scope={k:getattr(context,k) for k in ('source','identity','project','session','conversation')};scope['workspace']=str(resolve_path('.'))
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT count(*) FROM scheduled_jobs').fetchone()[0]>=1000:raise ValueError('Schedule capacity reached')
            conn.execute('INSERT INTO scheduled_jobs(id,name,description,cron_expression,job_type,job_payload,next_run_at,owner_role,owner_identity,owner,context,timezone,timeout,max_runs) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key,name,description,cron_expression,job_type,serialized,due,current_role(),context.identity,task_owner(context),json.dumps(scope),timezone,timeout,max_runs))
        return key

    def list_jobs(self):
        with closing(self.connect()) as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM scheduled_jobs WHERE owner=? ORDER BY created_at DESC LIMIT 1000',(task_owner(current_context()),))]

    def disable(self,key):
        with closing(self.connect()) as conn,conn:
            changed=conn.execute('UPDATE scheduled_jobs SET is_enabled=0 WHERE id=? AND owner=?',(key,task_owner(current_context()))).rowcount==1
            if changed:
                conn.execute('INSERT OR IGNORE INTO schedule_notification_delivery SELECT id,\'cancelled\',NULL,0,NULL,NULL FROM schedule_notifications WHERE job_id=? AND owner=?',
                    (key,task_owner(current_context())))
            return changed

    def notifications(self):
        with closing(self.connect()) as conn:
            return [dict(row) for row in conn.execute('''SELECT n.id,n.job_id,n.state,n.output,n.created,
                COALESCE(d.status,'pending') AS delivery,d.error_type FROM schedule_notifications n
                LEFT JOIN schedule_notification_delivery d ON d.id=n.id WHERE n.owner=? ORDER BY n.created DESC LIMIT 100''',(task_owner(current_context()),))]

    def get_due_jobs(self):
        with closing(self.connect()) as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM scheduled_jobs WHERE is_enabled=1 AND next_run_at<=? ORDER BY next_run_at LIMIT 100',(now_utc().isoformat(),))]

    def acquire_lock(self,job_id,locked_by,timeout_seconds=10):
        if type(timeout_seconds) is not int or not 1<=timeout_seconds<=300:raise ValueError('Invalid lease duration')
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE');now=time.time()
            row=conn.execute('SELECT * FROM scheduled_job_locks WHERE job_id=?',(job_id,)).fetchone()
            if row and float(row['expires_at'])>=now:return False
            conn.execute('INSERT INTO scheduled_job_locks VALUES(?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET locked_by=excluded.locked_by,locked_at=excluded.locked_at,expires_at=excluded.expires_at',
                (job_id,locked_by,now,now+timeout_seconds));return True

    def release_lock(self,job_id,locked_by=None):
        with closing(self.connect()) as conn,conn:
            if locked_by is None:conn.execute('DELETE FROM scheduled_job_locks WHERE job_id=?',(job_id,))
            else:conn.execute('DELETE FROM scheduled_job_locks WHERE job_id=? AND locked_by=?',(job_id,locked_by))

    def renew(self,key,token):
        with closing(self.connect()) as conn,conn:
            return conn.execute('UPDATE scheduled_job_locks SET expires_at=? WHERE job_id=? AND locked_by=? AND expires_at>?',
                (time.time()+10,key,token,time.time())).rowcount==1

    def alive(self,key,token):
        with closing(self.connect()) as conn:
            return conn.execute('SELECT 1 FROM scheduled_job_locks l JOIN scheduled_jobs j ON j.id=l.job_id WHERE j.id=? AND l.locked_by=? AND l.expires_at>? AND j.is_enabled=1',
                (key,token,time.time())).fetchone() is not None

    def claim_due(self,job,token):
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT * FROM scheduled_jobs WHERE id=? AND is_enabled=1 AND next_run_at=? AND next_run_at<=?',
                (job['id'],job['next_run_at'],now_utc().isoformat())).fetchone()
            if not row:return None
            lock=conn.execute('SELECT * FROM scheduled_job_locks WHERE job_id=?',(job['id'],)).fetchone()
            if lock and float(lock['expires_at'])>time.time():return None
            running=conn.execute("SELECT id FROM scheduled_job_runs WHERE job_id=? AND status='running'",(job['id'],)).fetchall()
            if running:
                conn.execute("UPDATE scheduled_job_runs SET status='uncertain',error_message='Worker lost; side effects require human review' WHERE job_id=? AND status='running'",(job['id'],))
                if row['job_type']!='watch' or row['retry_count']>=2:
                    conn.execute('UPDATE scheduled_jobs SET is_enabled=0 WHERE id=?',(job['id'],))
                    self.notify(conn,dict(row),'needs_input','Previous run is uncertain; inspect before creating a replacement job')
                    conn.execute('DELETE FROM scheduled_job_locks WHERE job_id=?',(job['id'],));return None
            if conn.execute('SELECT count(*) FROM scheduled_job_runs').fetchone()[0]>=10000:raise ValueError('Schedule run capacity reached')
            conn.execute('INSERT INTO scheduled_job_locks VALUES(?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET locked_by=excluded.locked_by,expires_at=excluded.expires_at',
                (job['id'],token,time.time(),time.time()+10))
            key=uuid.uuid4().hex
            conn.execute("INSERT INTO scheduled_job_runs(id,job_id,status) VALUES(?,?,'running')",(key,job['id']))
            if running:conn.execute('UPDATE scheduled_jobs SET retry_count=retry_count+1 WHERE id=?',(job['id'],))
            return key

    def notify(self,conn,job,state,output):
        if conn.execute('SELECT count(*) FROM schedule_notifications').fetchone()[0]>=10000:raise ValueError('Notification capacity reached')
        conn.execute('INSERT INTO schedule_notifications VALUES(?,?,?,?,?,?)',(uuid.uuid4().hex,job['id'],job['owner'],state,output[:65536],time.time()))

    def complete(self,job,token,run_id,state,output,error=''):
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute('SELECT 1 FROM scheduled_job_locks WHERE job_id=? AND locked_by=? AND expires_at>?',(job['id'],token,time.time())).fetchone():return False
            current=dict(conn.execute('SELECT * FROM scheduled_jobs WHERE id=?',(job['id'],)).fetchone())
            text=output[:65536]
            count=current['run_count']+1;retry=current['retry_count']
            safe_retry=current['job_type']=='watch' and state=='failed' and retry<2
            if current['job_type']=='watch':
                if state=='failed' and not safe_retry:
                    state='needs_input';error='Watch retry limit reached; inspect before rescheduling. '+error
                elif count>=current['max_runs'] and (state=='waiting' or safe_retry):
                    state='needs_input';error='Watch run limit reached without completion; inspect before rescheduling. '+error
                    safe_retry=False
            waiting=state=='waiting'
            # Failures with new evidence and terminal limit transitions are meaningful.
            evidence=text+('\n'+error[:1000] if state in {'failed','needs_input','uncertain'} else '')
            digest=hashlib.sha256((state+'\n'+evidence).encode()).hexdigest()
            disabled=count>=current['max_runs'] or state in {'needs_input','uncertain','cancelled'} or (current['job_type']=='watch' and state=='completed') or (state=='failed' and not safe_retry)
            due=(datetime.fromtimestamp(time.time()+30*(retry+1),timezone.utc).isoformat() if safe_retry
                 else next_time(current['cron_expression'],current['timezone']))
            if digest!=current['last_digest'] and (current['last_digest'] is not None or not waiting):
                # Platform messages truncate from the end; keep actionable errors first.
                notification=(error[:1000]+'\n'+text if error and state in {'failed','needs_input','uncertain'} else text)
                self.notify(conn,current,state,notification.strip())
            history=(json.loads(current['history'])+[{'state':state,'summary':text[:4096]}])[-4:]
            conn.execute('UPDATE scheduled_jobs SET next_run_at=?,run_count=?,retry_count=?,last_digest=?,history=?,is_enabled=? WHERE id=?',
                (due,count,retry+1 if safe_retry else 0,digest,json.dumps(history),int(current['is_enabled'] and not disabled),job['id']))
            conn.execute('UPDATE scheduled_job_runs SET status=?,completed_at=CURRENT_TIMESTAMP,output=?,error_message=? WHERE id=?',
                (state,text,error[:1000],run_id));conn.execute('DELETE FROM scheduled_job_locks WHERE job_id=? AND locked_by=?',(job['id'],token));return True

    # Compatibility methods for administrative diagnostics.
    def record_run_start(self,job_id):
        key=uuid.uuid4().hex
        with closing(self.connect()) as conn,conn:conn.execute("INSERT INTO scheduled_job_runs(id,job_id,status) VALUES(?,?,'running')",(key,job_id))
        return key
    def record_run_complete(self,run_id,status,output='',error_message=''):
        with closing(self.connect()) as conn,conn:conn.execute('UPDATE scheduled_job_runs SET status=?,output=?,error_message=?,completed_at=CURRENT_TIMESTAMP WHERE id=?',(status,output[:65536],error_message[:1000],run_id))
    def update_next_run(self,job_id,cron_expression):
        with closing(self.connect()) as conn,conn:
            row=conn.execute('SELECT timezone FROM scheduled_jobs WHERE id=?',(job_id,)).fetchone()
            conn.execute('UPDATE scheduled_jobs SET next_run_at=? WHERE id=?',(next_time(cron_expression,row['timezone']),job_id))
