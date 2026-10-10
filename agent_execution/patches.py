"""Owner-bound patch proposals; exact bytes are checked before any file changes."""
from contextlib import closing
import difflib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid

from .policy import current_context,resolve_path
from .tasks import TaskStore,task_owner


def digest(data):return hashlib.sha256(data).hexdigest()


def artifact_owner():
    context=current_context()
    return context.coordinator_owner or task_owner(context)


def bounded_path(root,name):
    if not isinstance(name,str) or not name or ':' in name or Path(name).is_absolute() or Path(name).drive or '..' in Path(name).parts or any(part.rstrip(' .').casefold()=='.git' for part in Path(name).parts):
        raise ValueError('Patch requires a relative file inside the workspace')
    path=root/name
    if root not in path.resolve().parents:raise ValueError('Patch path escapes workspace')
    for part in (path,*path.parents):
        if part==root:break
        if part.is_symlink() or (hasattr(part,'is_junction') and part.is_junction()):raise ValueError('Patch links are unsupported')
    if path.exists() and not path.is_file():raise ValueError('Patch requires regular files')
    return path


def read_bytes(path):
    if not path.exists():return None
    if path.stat().st_size>8*1024*1024:raise ValueError('Patch file exceeds 8 MiB')
    data=path.read_bytes()
    if len(data)>8*1024*1024:raise ValueError('Patch file exceeds 8 MiB')
    data.decode('utf-8')
    return data


class ArtifactStore:
    def __init__(self):
        self.tasks=TaskStore();self.path=self.tasks.path
        with closing(self.connect()) as conn,conn:
            conn.execute('CREATE TABLE IF NOT EXISTS delivery_artifacts (id TEXT PRIMARY KEY,owner TEXT NOT NULL,task TEXT,kind TEXT NOT NULL,workspace TEXT NOT NULL,state TEXT NOT NULL,payload TEXT NOT NULL,updated REAL NOT NULL)')

    def connect(self):
        conn=sqlite3.connect(self.path,timeout=10);conn.row_factory=sqlite3.Row;return conn

    @staticmethod
    def capacity(conn,extra):
        count,size=conn.execute('SELECT COUNT(*),COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM delivery_artifacts').fetchone()
        if size+extra>64*1024*1024:raise ValueError('Delivery artifact capacity exceeded')

    def add(self,kind,payload,state='prepared'):
        context=current_context();key=uuid.uuid4().hex;data=json.dumps(payload,ensure_ascii=False,allow_nan=False)
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE');self.capacity(conn,len(data.encode()))
            if conn.execute('SELECT COUNT(*) FROM delivery_artifacts').fetchone()[0]>=10000:raise ValueError('Delivery artifact count exceeded')
            conn.execute('INSERT INTO delivery_artifacts VALUES(?,?,?,?,?,?,?,?)',(key,artifact_owner(),context.task_id,kind,str(resolve_path('.')),state,data,time.time()))
        return key

    def get(self,key,kind=None,strict_task=True):
        with closing(self.connect()) as conn:row=conn.execute('SELECT * FROM delivery_artifacts WHERE id=? AND owner=?',(key,artifact_owner())).fetchone()
        if row is None:raise KeyError('Artifact not found in this identity/session/project')
        if kind and row['kind']!=kind:raise ValueError('Wrong artifact kind')
        if strict_task and (row['task']!=current_context().task_id or row['workspace']!=str(resolve_path('.'))):raise ValueError('Artifact belongs to another task/workspace')
        return {**dict(row),'payload':json.loads(row['payload'])}

    def update(self,row,state,payload=None,expected=None):
        data=json.dumps(payload if payload is not None else row['payload'],ensure_ascii=False,allow_nan=False)
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            old=conn.execute('SELECT length(CAST(payload AS BLOB)) FROM delivery_artifacts WHERE id=? AND owner=?',(row['id'],row['owner'])).fetchone()
            self.capacity(conn,len(data.encode())-(old[0] if old else 0))
            sql='UPDATE delivery_artifacts SET state=?,payload=?,updated=? WHERE id=? AND owner=?'
            args=[state,data,time.time(),row['id'],row['owner']]
            if expected is not None:sql+=' AND state=?';args.append(expected)
            if conn.execute(sql,args).rowcount!=1:raise RuntimeError('Artifact state changed or lease lost')


def prepare_patch(changes):
    root=resolve_path('.')
    if not isinstance(changes,list) or not 1<=len(changes)<=32:raise ValueError('Patch requires 1..32 changes')
    records=[];seen=set();total=0;diff=[]
    for change in changes:
        if not isinstance(change,dict) or set(change)!={'path','before_sha256','content'}:raise ValueError('Patch change requires path, before_sha256 and content')
        path=bounded_path(root,change['path']);name=path.relative_to(root).as_posix()
        if os.path.normcase(str(path)) in seen:raise ValueError('Duplicate patch path')
        seen.add(os.path.normcase(str(path)));old=read_bytes(path)
        expected=change['before_sha256']
        if expected!=(digest(old) if old is not None else None):raise ValueError('Patch conflict: '+name)
        content=change['content']
        if content is not None and not isinstance(content,str):raise ValueError('Patch content must be UTF-8 text or null for deletion')
        new=content.encode('utf-8') if content is not None else None
        if old is None and new is None:raise ValueError('Cannot delete a missing file')
        if old==new:raise ValueError('Patch has no change: '+name)
        total+=len(old or b'')+len(new or b'')
        if len(new or b'')>8*1024*1024 or total>16*1024*1024:raise ValueError('Patch exceeds 8 MiB/file or 16 MiB total')
        mode=stat.S_IMODE(path.stat().st_mode) if old is not None else 0o644
        records.append({'path':name,'before':old.decode('utf-8') if old is not None else None,'after':content,'before_sha256':expected,'mode':mode})
        diff.append('diff --git a/'+name+' b/'+name+'\n')
        if old is None:diff.append('new file\n')
        if new is None:diff.append('deleted file\n')
        diff.extend(difflib.unified_diff((old or b'').decode('utf-8').splitlines(True),(content or '').splitlines(True),fromfile='/dev/null' if old is None else 'a/'+name,tofile='/dev/null' if new is None else 'b/'+name))
    preview=''.join(diff)
    if len(preview.encode())>2*1024*1024:raise ValueError('Patch diff exceeds 2 MiB; split the proposal')
    payload={'changes':records,'diff':preview,'digest':digest(json.dumps(records,sort_keys=True,ensure_ascii=False).encode())}
    key=ArtifactStore().add('patch',payload)
    return {'patch_id':key,'state':'prepared','diff':preview,'digest':payload['digest']}


def replace_bytes(path,data,mode,token):
    if data is None:path.unlink();return
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.pythonai-'+token)
    try:
        temporary.write_bytes(data);temporary.chmod(mode);os.replace(temporary,path)
    finally:
        if temporary.exists():temporary.unlink()


def apply_patch(key):
    store=ArtifactStore();row=store.get(key,'patch');root=resolve_path('.')
    if row['state']=='applied':return {'patch_id':key,'state':'applied','reused':True,'digest':row['payload']['digest']}
    if row['state']!='prepared':raise ValueError('Interrupted or failed patch needs human inspection; create a fresh proposal')
    changes=row['payload']['changes'];prepared=[]
    for change in changes:
        path=bounded_path(root,change['path']);old=read_bytes(path)
        if (digest(old) if old is not None else None)!=change['before_sha256']:raise ValueError('Patch conflict: '+change['path'])
        prepared.append((change,path,old,change['after'].encode('utf-8') if change['after'] is not None else None))
    store.update(row,'applying',expected='prepared');applied=[]
    try:
        for change,path,old,new in prepared:
            from .budgets import active_budget
            budget=active_budget()
            if budget:budget.check()
            if current_context().check_cancelled and current_context().check_cancelled():raise InterruptedError('Patch cancelled')
            if read_bytes(bounded_path(root,change['path']))!=old:raise ValueError('Patch conflict during apply: '+change['path'])
            replace_bytes(path,new,change['mode'],key);applied.append((change,path,old,new))
        store.update(row,'applied',expected='applying')
    except Exception:
        for change,path,old,new in reversed(applied):
            # Preserve concurrent edits; do not follow a replaced link during recovery.
            try:
                path=bounded_path(root,change['path'])
                if read_bytes(path)==new:replace_bytes(path,old,change['mode'],key)
            except Exception:pass
        store.update(row,'needs_resolution',expected='applying')
        raise
    return {'patch_id':key,'state':'applied','digest':row['payload']['digest'],'files':[item['path'] for item in changes]}
