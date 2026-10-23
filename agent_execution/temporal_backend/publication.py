"""Local durable events/finalization. SDK objects and content never enter History."""
from contextlib import closing, contextmanager
from contextvars import ContextVar
import json
import sqlite3
from pathlib import Path
import time

from agent_execution.delivery import CapacityError, positive_limit
from .store import canonical, digest

_runtime = ContextVar('temporal_publication_runtime', default=None)
VERSION = 1


@contextmanager
def runtime_scope(runtime, episode):
    token = _runtime.set((runtime, episode))
    try:
        yield
    finally:
        _runtime.reset(token)


def manifest():
    value = _runtime.get()
    if value is None:
        return None
    from memory import MemoryStore, memory_db_path
    from agent_execution.policy import current_context
    runtime, episode = value
    memory = runtime.memory
    if memory is not None and not isinstance(memory, MemoryStore):
        return None
    if memory is not None and memory.db_path.resolve()!=memory_db_path().resolve():
        raise PermissionError('Runtime memory does not match the current identity/configuration')
    if type(episode) is not int or episode <= 0:
        memory = None
        episode = None
    from .runtime_state import wake_reviews
    wake_reviews(runtime)
    context = current_context()
    delivery = context.event_delivery
    if context.source=='communication' and context.communication_db:
        from agent_execution.tasks import task_owner
        delivery = {'kind':'communication','path':str(context.communication_db.resolve()),'owner':task_owner(context),'ref':context.task_id}
    return {'version': VERSION, 'episode': episode,
            'memory_db': str(memory.db_path.resolve()) if memory is not None else None,
            'review': memory is not None and runtime.memory_worker is not None,
            'delivery': delivery}


def binding(row):
    value = json.loads(row['context']).get('publication')
    if value is None:
        return None
    if not isinstance(value,dict) or set(value)!={'version','episode','memory_db','review','delivery'} or type(value['version']) is not int or value['version']!=VERSION:
        raise ValueError('Unsupported publication version')
    if type(value['review']) is not bool or (value['episode'] is not None and (type(value['episode']) is not int or value['episode']<=0)):
        raise ValueError('Invalid publication episode')
    path = value['memory_db']
    if path is not None and (not isinstance(path,str) or len(path)>4096 or not Path(path).is_absolute() or value['episode'] is None):
        raise ValueError('Invalid publication memory reference')
    target = value['delivery']
    if target is not None and (not isinstance(target,dict) or set(target) not in ({'path','owner','ref'},{'kind','path','owner','ref'}) or
        any(not isinstance(target[key],str) or not 1<=len(target[key])<=4096 for key in target) or not Path(target['path']).is_absolute()):
        raise ValueError('Invalid publication delivery reference')
    return value


def install(conn):
    # Do not use executescript: final_intent shares the caller's transaction.
    conn.execute("CREATE TABLE IF NOT EXISTS temporal_events(submission_id TEXT NOT NULL, seq INTEGER NOT NULL, event_key TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, delivery TEXT NOT NULL DEFAULT 'pending', PRIMARY KEY(submission_id,seq), UNIQUE(submission_id,event_key))")
    conn.execute('CREATE TABLE IF NOT EXISTS temporal_finalizations(submission_id TEXT PRIMARY KEY, version INTEGER NOT NULL, state TEXT NOT NULL)')


def final_intent(conn, row):
    if binding(row) is not None:
        install(conn)
        conn.execute("INSERT OR IGNORE INTO temporal_finalizations VALUES(?,?,'pending')", (row['id'],VERSION))


def append(store, row, key, kind, payload, *, terminal=False):
    from agent_execution.tasks import assert_owner_active
    from agent_execution.policy import current_context
    from agent_execution.tasks import task_owner
    if task_owner(current_context())!=row['owner']:
        raise PermissionError('Event is outside this owner scope')
    check = current_context().check_cancelled
    if not terminal and check and check():
        raise InterruptedError('Event fenced by cancellation')
    if not terminal:
        from agent_execution.budgets import active_budget
        budget = active_budget()
        if budget is not None:
            budget.check()
    if not isinstance(key,str) or not 1<=len(key)<=256:
        raise ValueError('Invalid event key')
    data = canonical({**payload,'task_id':row['task_id'],'temporal_event_id':digest([row['id'],key])})
    if len(data.encode()) > 2*1024*1024 or not isinstance(kind,str) or len(kind)>64:
        raise CapacityError('Temporal event frame exceeds limit')
    with closing(store.tasks.connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        assert_owner_active(conn,row['owner'])
        current = conn.execute('SELECT state,fence FROM temporal_submissions WHERE id=?',(row['id'],)).fetchone()
        task = conn.execute('SELECT state FROM tasks WHERE owner=? AND id=?',(row['owner'],row['task_id'])).fetchone()
        if (not current or not task or current['fence'] != row['fence'] or
            current['state'] not in ({'completed','failed','cancelled','uncertain'} if terminal else {'running'}) or (not terminal and task['state'] in {'cancelled','uncertain'})):
            raise PermissionError('Event fenced by task or execution')
        install(conn)
        old = conn.execute('SELECT kind,payload FROM temporal_events WHERE submission_id=? AND event_key=?',(row['id'],key)).fetchone()
        if old:
            if (old['kind'],old['payload']) != (kind,data):
                raise ValueError('Event key reused with different content')
            return
        count,size = conn.execute('SELECT count(*),COALESCE(sum(length(CAST(payload AS BLOB))+1024),0) FROM temporal_events').fetchone()
        if count >= positive_limit('AGENT_TEMPORAL_EVENT_LIMIT',100000) or size+len(data.encode())+1024 > positive_limit('AGENT_TEMPORAL_EVENT_BYTES',64*1024*1024):
            raise CapacityError('Temporal event journal full')
        seq = conn.execute('SELECT COALESCE(max(seq),0)+1 FROM temporal_events WHERE submission_id=?',(row['id'],)).fetchone()[0]
        target = binding(row).get('delivery')
        delivery = 'pending' if target and (target.get('kind','websocket')=='websocket' or kind=='end') else 'local'
        conn.execute('INSERT INTO temporal_events(submission_id,seq,event_key,kind,payload,delivery) VALUES(?,?,?,?,?,?)',(row['id'],seq,key,kind,data,delivery))


class EventWriter:
    def __init__(self, store, row, prefix):
        self.store,self.row,self.prefix = store,row,prefix
        self.index,self.parts,self.size,self.last = 0,[],0,time.monotonic()

    def flush(self):
        if self.parts:
            text = ''.join(self.parts)
            self.parts,self.size = [],0
            self.write('model.delta',{'text':text})

    def write(self, kind, payload):
        append(self.store,self.row,f'{self.prefix}:{self.index}',kind,payload)
        self.index += 1
        self.last = time.monotonic()

    def __call__(self, kind, payload):
        if kind == 'model.delta':
            value = payload.get('text','')
            if not isinstance(value,str):
                raise ValueError('Invalid model delta')
            for offset in range(0,len(value),2048):
                part = value[offset:offset+2048]
                self.parts.append(part)
                self.size += len(part)
                if self.size >= 2048 or time.monotonic()-self.last >= .1:
                    self.flush()
        else:
            self.flush()
            self.write(kind,payload)


def writer(store, row, prefix):
    return EventWriter(store,row,prefix) if binding(row) is not None else None


def events(store, row, after=0):
    from agent_execution.tasks import assert_owner_active, task_owner
    from agent_execution.policy import current_context
    if task_owner(current_context()) != row['owner']:
        raise PermissionError('Events are outside this task owner')
    with closing(store.tasks.connect()) as conn:
        assert_owner_active(conn,row['owner'])
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_events'").fetchone():
            return []
        return [dict(item) for item in conn.execute('SELECT * FROM temporal_events WHERE submission_id=? AND seq>? ORDER BY seq LIMIT 256',(row['id'],after))]


def relay(store, row, after=0):
    value = _runtime.get()
    callback = getattr(value[0],'event_callback',None) if value else None
    if callback is None:
        return after
    pending = events(store,row,after)
    if pending:
        from .references import reference
        with reference(store,row):
            for event in pending:
                if event['kind'] != 'end':
                    callback(event['kind'],json.loads(event['payload']))
                after = event['seq']
    return after


def end_payload(store, row):
    with closing(store.tasks.connect()) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_events'").fetchone():
            return None
        item = conn.execute("SELECT payload FROM temporal_events WHERE submission_id=? AND kind='end' ORDER BY seq DESC LIMIT 1",(row['id'],)).fetchone()
    return json.loads(item[0]) if item else None


def waiting(store, row):
    if binding(row) is None:
        return
    task = store.tasks.get(row['owner'],row['task_id'])
    if task['state'] == 'suspended':
        key = digest([task['phase'],task['checkpoint'].get('suspended_tool_call_id'),task['reply']])
        append(store,row,'waiting:'+key,'end',{'text':task['reply'],'suspended':True,'failed':False,
            'interrupted':False,'episode_id':binding(row)['episode'],'emotion':'neutral','action':'none'})


@contextmanager
def publication_scope(store, row):
    """Read-only authority for postprocessing, including fenced terminal statuses."""
    from agent_execution.policy import execution_scope, current_context
    from agent_execution.tasks import assert_owner_active, task_owner
    from agent_execution.sessions import project_workspace
    from agent_execution.workspace_lifecycle import LifecycleStore
    scope = json.loads(row['context'])
    if scope['identity']=='local' and scope['source']!='local':
        raise PermissionError('Invalid local publication authority')
    with execution_scope(workspace=None,repository_workspace=None):
        configured = project_workspace(scope['project']).resolve()
    if Path(scope['workspace']).resolve()!=configured:
        raise PermissionError('Publication workspace configuration changed')
    values = {key:scope[key] for key in ('source','identity','session','project','conversation')}
    with execution_scope(**values,task_id=row['task_id'],workspace=configured,
                         allowed_tools=frozenset(),check_cancelled=None):
        if task_owner(current_context()) != row['owner']:
            raise PermissionError('Invalid publication owner')
        with closing(store.tasks.connect()) as conn:
            assert_owner_active(conn,row['owner'])
        LifecycleStore().ensure_session()
        from .references import reference
        with reference(store,row):
            yield


def finalize(store, row):
    metadata = binding(row)
    if metadata is None or row['state'] not in {'completed','failed','cancelled','uncertain'}:
        return
    with closing(store.tasks.connect()) as conn:
        finished = conn.execute('SELECT version,state FROM temporal_finalizations WHERE submission_id=?',(row['id'],)).fetchone()
        if finished and finished['version'] != VERSION:
            raise ValueError('Unsupported finalization version')
        if finished and finished['state']=='completed':
            return
    with publication_scope(store,row):
        result = (json.loads(row['result']) if row['state'] in {'completed','failed'} else
            {'reply':'Task cancelled' if row['state']=='cancelled' else 'Error: Task execution uncertain; inspect status before recovery.'})
        if metadata['memory_db'] is not None:
            from memory import MemoryStore, memory_db_path
            path = Path(metadata['memory_db'])
            if path.resolve() != memory_db_path().resolve() or not path.is_file():
                raise PermissionError('Memory database configuration changed or removed')
            MemoryStore(path).finalize_temporal(digest([row['id'],VERSION]),metadata['episode'],
                result['reply'],'completed' if row['state']=='completed' else 'failed',metadata['review'] and row['state'] in {'completed','failed'})
        append(store,row,'final:'+str(VERSION),'end',{'text':result['reply'],'episode_id':metadata['episode'],
            'task_state':row['state'],'suspended':False,'failed':row['state'] in {'failed','uncertain'},'interrupted':row['state']=='cancelled','emotion':'neutral','action':'none'},terminal=True)
        with closing(store.tasks.connect()) as conn, conn:
            conn.execute("UPDATE temporal_finalizations SET state='completed' WHERE submission_id=? AND version=?",(row['id'],VERSION))


def publish(store, row):
    metadata = binding(row)
    target = metadata.get('delivery') if metadata else None
    if target is None:
        return
    from agent_execution.delivery import EventStore
    with publication_scope(store,row):
        if target.get('kind','websocket')=='communication':
            from .communication_publication import publish as publish_communication
            publish_communication(store,row,target)
            return
        if target.get('kind','websocket')!='websocket':
            raise ValueError('Unsupported publication transport')
        outbox = EventStore()
        if str(outbox.path.resolve()) != target['path']:
            raise PermissionError('Event database configuration changed')
        scope = json.loads(row['context'])
        prefix = scope['project']+':'
        if (scope['source'] != 'websocket' or not scope['identity'].startswith('websocket:') or
            not scope['session'].startswith(prefix) or target['ref'] != row['task_id'] or
            target['owner'] != EventStore.owner(scope['identity'][10:],scope['project'],scope['session'][len(prefix):])):
            raise PermissionError('Invalid event delivery scope')
        with closing(store.tasks.connect()) as conn, conn:
            if row['state'] in {'cancelled','uncertain'}:
                conn.execute("UPDATE temporal_events SET delivery='cancelled' WHERE submission_id=? AND delivery='pending' AND event_key!=?",(row['id'],'final:'+str(VERSION)))
            pending = conn.execute("SELECT * FROM temporal_events WHERE submission_id=? AND delivery='pending' ORDER BY seq LIMIT 256",(row['id'],)).fetchall()
        for item in pending:
            payload = json.loads(item['payload'])
            kind = {'model.delta':'chat.delta','end':'chat.end'}.get(item['kind'],item['kind'])
            outbox.append(target['owner'],{'v':1,'type':kind,'ref':target['ref'],
                'id':payload['temporal_event_id'],'ts':int(time.time()*1000),'payload':payload},event_key=payload['temporal_event_id'])
            with closing(store.tasks.connect()) as conn, conn:
                conn.execute("UPDATE temporal_events SET delivery='sent' WHERE submission_id=? AND seq=?",(row['id'],item['seq']))


def reconcile(store, settings, queues):
    with closing(store.tasks.connect()) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_events'").fetchone():
            return
        rows = conn.execute("SELECT * FROM temporal_submissions WHERE address=? AND namespace=? AND queue IN ("+
            ','.join('?' for _ in queues)+") AND state IN ('running','completed','failed','cancelled','uncertain') AND (EXISTS(SELECT 1 FROM temporal_finalizations f WHERE f.submission_id=temporal_submissions.id AND f.state='pending') OR EXISTS(SELECT 1 FROM temporal_events e WHERE e.submission_id=temporal_submissions.id AND e.delivery='pending')) ORDER BY updated LIMIT 100",(settings.address,settings.namespace,*sorted(queues))).fetchall()
    for item in rows:
        row = dict(item)
        if binding(row) is None:
            continue
        try:
            finalize(store,row)
            publish(store,row)
        except (OSError,sqlite3.Error,ValueError,PermissionError,CapacityError):
            continue
