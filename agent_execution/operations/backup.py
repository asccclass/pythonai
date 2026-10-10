"""Consistent per-database SQLite snapshots; restore only into a new destination."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import time


def connect_readonly(path):return sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def inspect_database(path):
    path=Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_size>256*1024*1024:raise ValueError('Database missing, redirected or exceeds 256 MiB')
    with closing(connect_readonly(path)) as conn:
        if conn.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Database integrity check failed')
        schema=conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        version=conn.execute('PRAGMA user_version').fetchone()[0]
    return {'bytes':path.stat().st_size,'user_version':version,'schema_digest':hashlib.sha256(json.dumps(schema).encode()).hexdigest(),'tables':[row[0] for row in schema]}


def backup(databases,destination):
    if not 1<=len(databases)<=32:raise ValueError('Expected 1..32 databases')
    root=Path(destination).resolve()
    if root.exists():raise FileExistsError('Backup destination must be new')
    sources=[Path(path).resolve() for path in databases]
    for source in sources:inspect_database(source)
    root.mkdir(parents=True);entries=[]
    for number,source in enumerate(sources):
        target=root/(str(number)+'.sqlite');deadline=time.monotonic()+10
        def progress(*args):
            if time.monotonic()>deadline:raise TimeoutError('SQLite backup deadline exceeded')
        with closing(connect_readonly(source)) as original,closing(sqlite3.connect(target)) as copy:
            original.backup(copy,pages=128,progress=progress,sleep=.05)
        evidence=inspect_database(target)
        entries.append({'file':target.name,'source':str(source),'sha256':sha(target),**evidence})
    manifest={'format':1,'created':time.time(),'consistency':'per-database; quiesce workers for cross-database consistency','databases':entries}
    (root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return manifest


def verify(directory):
    root=Path(directory).resolve();manifest_path=root/'manifest.json'
    if manifest_path.is_symlink() or manifest_path.stat().st_size>65536:raise ValueError('Invalid backup manifest')
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('format')!=1 or not isinstance(manifest.get('databases'),list) or not 1<=len(manifest['databases'])<=32:raise ValueError('Unknown backup format')
    names=set()
    for entry in manifest['databases']:
        name=entry['file']
        if not isinstance(name,str) or Path(name).name!=name or not name.endswith('.sqlite') or name in names:raise ValueError('Backup path/collision rejected')
        names.add(name);target=root/name
        if target.is_symlink() or not target.resolve().is_relative_to(root):raise ValueError('Backup redirect rejected')
        evidence=inspect_database(target)
        if sha(target)!=entry['sha256'] or evidence['schema_digest']!=entry['schema_digest']:raise ValueError('Backup content changed')
    return manifest


def restore(directory,destination):
    manifest=verify(directory);source=Path(directory).resolve();root=Path(destination).resolve()
    if root.exists():raise FileExistsError('Restore target must be a new directory; existing databases are never overwritten')
    root.mkdir(parents=True)
    for entry in manifest['databases']:
        target=root/entry['file']
        with target.open('xb') as output,(source/entry['file']).open('rb') as original:shutil.copyfileobj(original,output)
        inspect_database(target)
    (root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'restored':len(manifest['databases']),'destination':str(root),'databases':[str(root/entry['file']) for entry in manifest['databases']]}


def migrate(kind,path,destination,offline=False):
    if not offline:raise ValueError('Migration requires workers stopped and explicit offline=True')
    constructors={
        'memory':('memory','MemoryStore'),'tasks':('agent_execution.tasks','TaskStore'),
        'scheduler':('agent_execution.scheduling.store','SchedulerStore'),
        'approvals':('agent_execution.approvals','ApprovalStore'),
        'extensions':('agent_execution.extensions.store','ExtensionStore'),
        'improvement':('agent_execution.improvement.store','ImprovementStore'),
        'trace':('agent_execution.operations.tracing','TraceStore')}
    if kind not in constructors:raise ValueError('Unknown database migration kind')
    before=inspect_database(path);backup([path],destination)
    import importlib
    module,name=constructors[kind];getattr(importlib.import_module(module),name)(path)
    return {'before':before,'after':inspect_database(path),'backup':str(Path(destination).resolve())}
