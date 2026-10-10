from contextlib import closing,contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from .redaction import redact

_span=ContextVar('operation_span',default=None)


class TraceStore:
    def __init__(self,path=None):
        self.path=Path(path or os.environ.get('AGENT_TRACE_DB') or Path(os.environ.get('LOCALAPPDATA',Path.home()))/'PythonAI'/'traces.db')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self.connect()) as conn,conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS spans(id TEXT PRIMARY KEY,owner TEXT,trace_id TEXT,parent_span TEXT,
                task_id TEXT,parent_task TEXT,episode INTEGER,node TEXT,kind TEXT,name TEXT,status TEXT,started REAL,
                elapsed REAL,metadata TEXT)''')
            conn.execute('CREATE INDEX IF NOT EXISTS spans_owner_trace ON spans(owner,trace_id)')
    def connect(self):
        conn=sqlite3.connect(self.path,timeout=2);conn.row_factory=sqlite3.Row;return conn
    def begin(self,kind,name,metadata=None):
        from ..policy import current_context
        from ..tasks import task_owner
        from ..budgets import active_budget
        scope=current_context();budget=active_budget();parent=_span.get();owner=scope.coordinator_owner or task_owner(scope)
        root=budget.key if budget else scope.task_id
        trace=hashlib.sha256((owner+'\n'+root).encode()).hexdigest()[:32] if root else parent['trace_id'] if parent else uuid.uuid4().hex
        key=uuid.uuid4().hex;record={'id':key,'trace_id':trace,'owner':owner,'started':time.time()}
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM spans WHERE started<?',(time.time()-7*86400,))
            count=conn.execute('SELECT count(*) FROM spans').fetchone()[0]
            if count>=20000:conn.execute('DELETE FROM spans WHERE id IN (SELECT id FROM spans ORDER BY started LIMIT 500)')
            data=json.dumps(redact(metadata or {}),allow_nan=False)
            if len(data.encode())>8192:data=json.dumps({'truncated':True})
            conn.execute('INSERT INTO spans VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(key,owner,trace,parent['id'] if parent else None,
                scope.task_id,root if root!=scope.task_id else None,scope.episode_id,scope.coordinator_node,kind,str(name)[:128],'running',record['started'],None,data))
        return record
    def finish(self,record,status,metadata=None):
        with closing(self.connect()) as conn,conn:
            data=json.dumps(redact(metadata or {}),allow_nan=False)
            if len(data.encode())>8192:data=json.dumps({'truncated':True})
            conn.execute('UPDATE spans SET status=?,elapsed=?,metadata=? WHERE id=? AND owner=?',(status,max(0,time.time()-record['started']),data,record['id'],record['owner']))
    def list(self,trace_id=None):
        from ..policy import current_context
        from ..tasks import task_owner
        owner=current_context().coordinator_owner or task_owner(current_context())
        with closing(self.connect()) as conn:
            rows=conn.execute('SELECT * FROM spans WHERE owner=?'+(' AND trace_id=?' if trace_id else '')+' ORDER BY started DESC LIMIT 1000',([owner,trace_id] if trace_id else [owner])).fetchall()
        return [{**dict(row),'metadata':json.loads(row['metadata'])} for row in rows]
    def summary(self):
        rows=self.list();completed=[row for row in rows if row['elapsed'] is not None]
        return {'window':'latest 1000 owner spans / 7 day retention','count':len(rows),
            'failed':sum(row['status']=='failed' for row in rows),'unfinished':sum(row['status']=='running' for row in rows),
            'latency_seconds':sum(row['elapsed'] for row in completed),'model_requests':sum(row['kind']=='model' for row in rows),
            'tokens':sum(row['metadata'].get('tokens',0) for row in rows),'known_cost_usd':sum(row['metadata'].get('cost_usd',0) for row in rows),
            'unknown_cost_spans':sum(row['metadata'].get('cost_known') is False for row in rows),
            'retry_count':sum(row['metadata'].get('retry',False) for row in rows)}


@contextmanager
def span(kind,name,metadata=None):
    record=None;store=None;token=None;result=dict(metadata or {});status='completed'
    if os.environ.get('AGENT_TRACE_ENABLED','1')=='1':
        try:store=TraceStore();record=store.begin(kind,name,metadata);token=_span.set(record)
        except Exception:pass # An observation failure must not repeat or block a real operation.
    try:yield result
    except BaseException as error:status='failed';result['error_type']=type(error).__name__;raise
    finally:
        if result.get('success') is False:status='failed'
        if record:
            try:store.finish(record,status,result)
            except Exception:pass
        if token is not None:_span.reset(token)


def traced(kind,name=None):
    def decorate(function):
        @wraps(function)
        def call(*args,**kwargs):
            from ..budgets import active_budget
            budget=active_budget();before=None
            if kind=='model' and budget:
                try:before=budget.snapshot()
                except Exception:pass
            operation=name or (str(args[0] if args else kwargs.get('name',function.__name__)) if kind=='tool' else function.__name__)
            with span(kind,operation) as evidence:
                try:
                    result=function(*args,**kwargs)
                    if kind=='tool':
                        try:
                            from ..results import normalize_result
                            evidence['success']=normalize_result(result,operation).success
                        except Exception:pass
                    return result
                finally:
                    if kind in {'model','audio'}:
                        evidence['accounting']='budget charged/reserved; streaming reconciliation is authoritative in task_budgets'
                        evidence['cost_known']=bool(kind=='model' and budget and budget.rates.get(kwargs.get('model')))
                        if before:
                            try:
                                after=budget.snapshot();evidence.update(tokens=after['tokens']-before['tokens'],cost_usd=after['cost']-before['cost'])
                            except Exception:pass
        return call
    return decorate
