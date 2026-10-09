"""Owner-bound Git worktrees and recoverable, explicitly selected delivery."""
from contextlib import closing, contextmanager
import hashlib
import itertools
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import uuid
import zipfile

from .process import run_process


@contextmanager
def task_workspace(owner,task):
    from .policy import current_context,execution_scope
    from .tasks import _active
    from base import workspace_root
    context=current_context()
    enabled=os.environ.get('AGENT_TASK_WORKTREE','0').lower()
    if enabled not in {'0','1','false','true'}:raise ValueError('AGENT_TASK_WORKTREE must be 0 or 1')
    active=_active.get()
    binding=active[0].workspace_binding(owner,task) if active else None
    if enabled in {'0','false'} and not binding:
        yield;return
    repository=Path(binding['repository']) if binding else context.repository_workspace or workspace_root()
    store=WorktreeStore(repository,binding['storage'] if binding else None)
    if active and not binding:
        binding=active[0].bind_workspace(owner,task,active[3],store.repository,store.storage)
        store=WorktreeStore(binding['repository'],binding['storage'])
    path=store.create(owner,task)
    with execution_scope(workspace=path,repository_workspace=Path(repository).resolve()):yield


def human_worktree_command(text,tasks,owner,context):
    """Only human transports may call this; never a model tool."""
    from .approvals import ApprovalStore,ApprovalRequired
    from permissions import current_role
    from base import workspace_root
    parts=text.split()
    if len(parts) not in {3,4}:raise ValueError('Usage: /worktree <task_id> preview|archive|restore [approval_id]')
    key,operation=parts[1:3]
    row=tasks.get(owner,key)
    binding=tasks.workspace_binding(owner,key)
    store=WorktreeStore(binding['repository'],binding['storage']) if binding else WorktreeStore(context.repository_workspace or workspace_root())
    if operation=='preview':return json.dumps(store.preview(owner,key),ensure_ascii=False)
    if operation not in {'archive','restore'}:raise ValueError('Unknown worktree operation')
    if current_role()!='admin':raise PermissionError('Worktree mutations require admin role')
    if row['state'] not in {'completed','failed','cancelled'}:
        raise ValueError('Finish or cancel the task before changing its worktree')
    if row['lease_until'] and row['lease_until']>time.time():raise ValueError('Task still has an active lease')
    # Approval binds the selected task, repository and operation, not a broad cleanup.
    arguments={'task_id':key,'repository':str(store.repository),'operation':operation}
    action=f'{context.project}:worktree:{key}'
    approvals=ApprovalStore()
    if len(parts)==3:
        raise ApprovalRequired(approvals.request(context.identity,context.session,action,arguments))
    approvals.consume(parts[3],context.identity,context.session,action,arguments)
    result=store.archive(owner,key) if operation=='archive' else store.restore(owner,key)
    return json.dumps({'operation':operation,'task':key,'path':str(result)},ensure_ascii=False)


def rollback_files(repository,files):
    """Caller obtains one-use approval; archive selection before discarding tracked edits."""
    selected=selected_files(repository,files)
    env={**os.environ,'GIT_LITERAL_PATHSPECS':'1'}
    tracked=set(git(repository,'ls-tree','-r','--name-only','-z','HEAD','--',*selected,env=env).split('\x00'))
    if any(name not in tracked for name in selected):
        raise ValueError('Rollback accepts tracked files only; untracked files are retained')
    default=Path(os.environ.get('LOCALAPPDATA') or Path.home()/'.local'/'share')/'PythonAI'/'rollback'
    storage=Path(os.environ.get('AGENT_ROLLBACK_ROOT') or default).resolve()
    root=Path(repository).resolve()
    if storage==root or root in storage.parents:raise ValueError('Rollback archive must be outside repository')
    archive=storage/uuid.uuid4().hex
    archive.mkdir(parents=True)
    snapshot_files(root,archive/'files.zip',selected)
    # Preserve selected staged versions as well as working-copy bytes.
    patch=git(root,'diff','--cached','--binary','--',*selected,env=env)
    (archive/'index.patch').write_text(patch,encoding='utf-8')
    (archive/'metadata.json').write_text(json.dumps({'repository':str(root),'head':git(root,'rev-parse','HEAD').strip(),'files':selected}),encoding='utf-8')
    git(root,'restore','--source=HEAD','--staged','--worktree','--',*selected,env=env)
    return {'files':selected,'archive':str(archive)}


def git(repository, *arguments, env=None):
    result = run_process(['git', '-c', 'core.quotepath=false', *arguments],
        cwd=repository, capture_output=True, text=True, encoding='utf-8', env=env)
    if result.returncode:
        raise RuntimeError(f'Git failed ({result.returncode}): {result.stderr or result.stdout}')
    return result.stdout


def selected_files(repository, files):
    root = Path(repository).resolve()
    if not isinstance(files, list) or not files or len(files)>256:
        raise ValueError('Select 1..256 individual files; broad staging/rollback is disabled')
    selected=[]
    for value in files:
        if not isinstance(value,str) or not value or '\x00' in value:
            raise ValueError('Invalid file selection')
        target=(root/value).resolve()
        if root not in target.parents or '.git' in target.relative_to(root).parts or target.is_dir():
            raise ValueError('Selection must be individual files inside this workspace')
        name=target.relative_to(root).as_posix()
        if name not in selected:selected.append(name)
    return selected


def commit_files(repository, message, files):
    """Use an independent index; unrelated staged content never enters the commit."""
    selected=selected_files(repository,files)
    if not isinstance(message,str) or not message.strip():raise ValueError('Commit message required')
    with tempfile.TemporaryDirectory(prefix='pythonai-index-') as directory:
        index=Path(directory)/'index'
        env={**os.environ,'GIT_INDEX_FILE':str(index),'GIT_LITERAL_PATHSPECS':'1'}
        git(repository,'read-tree','HEAD',env=env)
        git(repository,'add','--',*selected,env=env)
        if not git(repository,'diff','--cached','--name-only',env=env).strip():
            raise ValueError('Selected files have no changes')
        git(repository,'diff','--cached','--check',env=env)
        # Repository hooks are trusted arbitrary executables and can mutate the
        # temporary index; a scoped commit therefore disables them explicitly.
        empty_hooks=Path(directory)/'hooks';empty_hooks.mkdir()
        git(repository,'-c',f'core.hooksPath={empty_hooks}','commit','-m',message,env=env)
    # Synchronize only the committed paths in the ordinary index.
    git(repository,'reset','-q','HEAD','--',*selected,env={**os.environ,'GIT_LITERAL_PATHSPECS':'1'})
    return {'commit':git(repository,'rev-parse','HEAD').strip(),'files':selected}


def snapshot_files(root, destination, selected=None):
    """Bounded archive, including ignored files; reject links instead of following them."""
    root=Path(root).resolve()
    total=0
    manifest=[]
    missing=[root/name for name in git(root,'ls-files','-z').split('\x00') if name and not (root/name).exists()] if selected is None else []
    candidates=[root/name for name in selected] if selected is not None else itertools.chain(root.rglob('*'),missing)
    with zipfile.ZipFile(destination,'x',compression=zipfile.ZIP_DEFLATED) as archive:
        for path in candidates:
            relative=path.relative_to(root)
            if '.git' in relative.parts:
                if relative.parts[0]!='.git':raise ValueError('Embedded repositories require manual preservation')
                continue
            if relative.as_posix()=='.pythonai-archive-manifest.json':raise ValueError('Reserved archive manifest path')
            if path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction()):
                raise ValueError('Cannot archive symbolic links or junctions safely')
            if not path.exists():
                manifest.append({'path':relative.as_posix(),'missing':True});continue
            if path.is_dir():continue
            if root not in path.resolve().parents:raise ValueError('Archive path escapes workspace')
            size=path.stat().st_size
            total+=size
            if size>64*1024*1024 or total>256*1024*1024 or len(manifest)>=10000:
                raise ValueError('Archive exceeds file/count/size limits; workspace retained')
            data=path.read_bytes()
            if len(data)!=size:raise ValueError('File changed during archive; retry when idle')
            archive.writestr(relative.as_posix(),data)
            manifest.append({'path':relative.as_posix(),'sha256':hashlib.sha256(data).hexdigest(),'size':size})
        archive.writestr('.pythonai-archive-manifest.json',json.dumps(manifest,ensure_ascii=False))
    return manifest


class WorktreeStore:
    def __init__(self, repository, storage=None):
        self.repository=Path(repository).resolve()
        if Path(git(self.repository,'rev-parse','--show-toplevel').strip()).resolve()!=self.repository:
            raise ValueError('Worktree source must be the exact repository root')
        default=Path(os.environ.get('LOCALAPPDATA') or Path.home()/'.local'/'share')/'PythonAI'/'worktrees'
        self.storage=Path(storage or os.environ.get('AGENT_WORKTREE_ROOT') or default).resolve()
        if self.storage==self.repository or self.repository in self.storage.parents:
            raise ValueError('Worktree storage must be outside the source repository')
        self.storage.mkdir(parents=True,exist_ok=True)
        self.database=self.storage/'registry.db'
        with closing(self.connect()) as conn,conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS worktrees (
                owner TEXT NOT NULL, task TEXT NOT NULL, repository TEXT NOT NULL,
                base TEXT NOT NULL, state TEXT NOT NULL, archive TEXT, updated REAL NOT NULL,
                PRIMARY KEY(owner,task,repository))''')

    def connect(self):
        conn=sqlite3.connect(self.database,timeout=60)
        conn.row_factory=sqlite3.Row
        return conn

    def location(self,owner,task):
        key=hashlib.sha256(json.dumps([owner,task,str(self.repository)],ensure_ascii=False).encode()).hexdigest()
        location=self.storage/key
        if location.resolve()!=location or location.parent!=self.storage:
            raise ValueError('Managed worktree path was replaced or escaped storage')
        return location

    def get(self,owner,task):
        with closing(self.connect()) as conn:
            row=conn.execute('SELECT * FROM worktrees WHERE owner=? AND task=? AND repository=?',
                (owner,task,str(self.repository))).fetchone()
        if row is None:raise KeyError(task)
        return {**dict(row),'path':str(self.location(owner,task))}

    def registered(self,path):
        records=git(self.repository,'worktree','list','--porcelain').splitlines()
        return any(line.startswith('worktree ') and Path(line[9:]).resolve()==path for line in records)

    def create(self,owner,task):
        if not isinstance(task,str) or not task or len(task)>256:raise ValueError('Invalid task ID')
        path=self.location(owner,task)
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT * FROM worktrees WHERE owner=? AND task=? AND repository=?',
                (owner,task,str(self.repository))).fetchone()
            if row and row['state']=='archived':raise ValueError('Archived task requires explicit restore')
            if row and self.registered(path):return path
            if path.exists():raise ValueError('Unregistered path exists; refusing to overwrite it')
            if conn.execute('SELECT COUNT(*) FROM worktrees WHERE state!=?',('archived',)).fetchone()[0]>=256:
                raise ValueError('Worktree capacity reached')
            base=git(self.repository,'rev-parse','HEAD').strip()
            git(self.repository,'worktree','add','--detach',str(path),base)
            conn.execute('INSERT OR REPLACE INTO worktrees VALUES(?,?,?,?,?,?,?)',
                (owner,task,str(self.repository),base,'ready',None,time.time()))
        return path

    def preview(self,owner,task):
        row=self.get(owner,task)
        if row['state']!='ready':raise ValueError('Worktree is not active')
        path=Path(row['path'])
        return {'base':row['base'],'head':git(path,'rev-parse','HEAD').strip(),
            'status':git(path,'status','--porcelain=v1'),
            'diff':git(path,'diff',row['base'],'--')}

    def restore(self,owner,task):
        row=self.get(owner,task)
        if row['state']!='archived':raise ValueError('Only archived worktrees can be restored')
        archive=Path(row['archive']).resolve()
        if archive.parent!=self.storage/'archives':raise ValueError('Archive is outside managed storage')
        metadata=json.loads((archive/'metadata.json').read_text(encoding='utf-8'))
        if any(metadata.get(name)!=value for name,value in {'owner':owner,'task':task,'repository':str(self.repository),'base':row['base']}.items()):
            raise ValueError('Archive identity mismatch')
        path=self.location(owner,task)
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            current=conn.execute('SELECT state FROM worktrees WHERE owner=? AND task=? AND repository=?',
                (owner,task,str(self.repository))).fetchone()
            if current['state']!='archived' or path.exists():raise ValueError('Restore target is no longer available')
            if conn.execute('SELECT COUNT(*) FROM worktrees WHERE state!=?',('archived',)).fetchone()[0]>=256:
                raise ValueError('Worktree capacity reached')
            git(self.repository,'bundle','verify',str(archive/'history.bundle'))
            git(self.repository,'fetch',str(archive/'history.bundle'),'HEAD')
            head=git(self.repository,'rev-parse','FETCH_HEAD').strip()
            if head!=metadata.get('head'):raise ValueError('Archive commit mismatch')
            git(self.repository,'worktree','add','--detach',str(path),head)
            with zipfile.ZipFile(archive/'files.zip') as zipped:
                manifest=json.loads(zipped.read('.pythonai-archive-manifest.json'))
                if not isinstance(manifest,list) or len(manifest)>10000:raise ValueError('Invalid archive manifest')
                total=0
                for entry in manifest:
                    names=selected_files(path,[entry['path']])
                    target=path/names[0]
                    if entry.get('missing'):
                        if target.exists():target.unlink()
                        continue
                    info=zipped.getinfo(entry['path'])
                    total+=info.file_size
                    if info.file_size>64*1024*1024 or total>256*1024*1024:raise ValueError('Archive too large')
                    data=zipped.read(info)
                    if hashlib.sha256(data).hexdigest()!=entry.get('sha256'):raise ValueError('Archive digest mismatch')
                    target.parent.mkdir(parents=True,exist_ok=True)
                    target.write_bytes(data)
            conn.execute('UPDATE worktrees SET state=?,updated=? WHERE owner=? AND task=? AND repository=?',
                ('ready',time.time(),owner,task,str(self.repository)))
        return path

    def archive(self,owner,task):
        """Caller must stop the task and obtain human authorization before this operation."""
        path=self.location(owner,task)
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT * FROM worktrees WHERE owner=? AND task=? AND repository=?',
                (owner,task,str(self.repository))).fetchone()
            if row is None:raise KeyError(task)
            if row['state']=='archived':return Path(row['archive'])
            if not self.registered(path):raise ValueError('Managed worktree is not registered')
            if git(path,'submodule','status').strip():raise ValueError('Submodule worktrees require manual preservation')
            objects=dict(line.split(': ',1) for line in git(path,'count-objects','-v').splitlines() if ': ' in line)
            if (int(objects.get('size',0))+int(objects.get('size-pack',0)))*1024>256*1024*1024:
                raise ValueError('Repository objects exceed archive limit; workspace retained')
            archive=self.storage/'archives'/uuid.uuid4().hex
            archive.mkdir(parents=True)
            initial_head=git(path,'rev-parse','HEAD').strip()
            status=git(path,'status','--porcelain=v1','--untracked-files=all','--ignored')
            manifest=snapshot_files(path,archive/'files.zip')
            git(path,'bundle','create',str(archive/'history.bundle'),'HEAD')
            git(path,'bundle','verify',str(archive/'history.bundle'))
            if (archive/'history.bundle').stat().st_size>256*1024*1024:
                raise ValueError('History bundle exceeds archive limit; workspace retained')
            head=git(path,'rev-parse','HEAD').strip()
            bundle_head=git(path,'bundle','list-heads',str(archive/'history.bundle'),'HEAD').split()[0]
            if head!=initial_head or bundle_head!=head:raise ValueError('Worktree HEAD changed during archive')
            metadata={'owner':owner,'task':task,'repository':str(self.repository),'base':row['base'],'head':head}
            (archive/'metadata.json').write_text(json.dumps(metadata,ensure_ascii=False),encoding='utf-8')
            if git(path,'status','--porcelain=v1','--untracked-files=all','--ignored')!=status:
                raise ValueError('Worktree changed during archive; workspace retained')
            for entry in manifest:
                item=path/entry['path']
                if entry.get('missing'):
                    if item.exists():raise ValueError('Worktree changed during archive')
                elif hashlib.sha256(item.read_bytes()).hexdigest()!=entry['sha256']:
                    raise ValueError('Worktree changed during archive')
            # Recheck the resolved target immediately before Git removes it.
            if self.location(owner,task)!=path:raise ValueError('Worktree path changed')
            if git(path,'rev-parse','HEAD').strip()!=head:raise ValueError('Worktree HEAD changed during archive')
            git(self.repository,'worktree','remove','--force',str(path))
            conn.execute('UPDATE worktrees SET state=?,archive=?,updated=? WHERE owner=? AND task=? AND repository=?',
                ('archived',str(archive),time.time(),owner,task,str(self.repository)))
        return archive
