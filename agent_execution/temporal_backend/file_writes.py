"""Approval-bound same-host file writes. No SDK, model, or automatic replay."""
from contextlib import closing, ExitStack
import hashlib
import io
import json
import os
from pathlib import Path, PureWindowsPath
import stat
import time
import uuid

from .contracts import READ_TOOLS, STEP_TOOLS, FILE_WRITE_TOOLS, APPROVAL_WORKFLOW_NAME, HUMAN_WORKFLOW_NAME
from .store import canonical, digest

VERSION = 1
MAX_BYTES = 2*1024*1024


def capabilities(scope, workflow):
    flag = scope.get('file_writes')
    if flag is not None and (type(flag) is not int or flag != VERSION or workflow != APPROVAL_WORKFLOW_NAME):
        raise PermissionError('Invalid persisted file-write policy')
    base = STEP_TOOLS if workflow in {HUMAN_WORKFLOW_NAME, APPROVAL_WORKFLOW_NAME} else READ_TOOLS
    return base | FILE_WRITE_TOOLS if flag == VERSION else base


def install(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS temporal_file_writes(
        submission_id TEXT NOT NULL, operation_id TEXT NOT NULL, call_id TEXT NOT NULL,
        name TEXT NOT NULL, arguments_digest TEXT NOT NULL, plan TEXT NOT NULL,
        observation TEXT, updated REAL NOT NULL, PRIMARY KEY(submission_id,operation_id))""")


def relative(value):
    if (not isinstance(value,str) or not 1 <= len(value) <= 1024 or '\x00' in value
            or PureWindowsPath(value).drive or PureWindowsPath(value).root or PureWindowsPath(value).is_reserved() or Path(value).is_absolute()):
        raise ValueError('File writes require a workspace-relative path')
    parts = value.replace('\\','/').split('/')
    if len(parts)>32 or any(not p or p in {'.','..'} or ':' in p or p.startswith('.') or p.endswith(('.', ' ')) or PureWindowsPath(p).is_reserved() for p in parts):
        raise ValueError('Unsafe or private write path')
    return Path(*parts)


def root():
    from base import workspace_root
    value = workspace_root()
    if value.is_symlink() or value.is_junction():
        raise PermissionError('Linked write workspace')
    return value


def state(path,directory=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {'kind':'missing'}
    if stat.S_ISLNK(info.st_mode) or path.is_junction():
        raise PermissionError('Linked write target or ancestor')
    identity = {'device':info.st_dev,'inode':info.st_ino}
    if stat.S_ISDIR(info.st_mode):
        return {'kind':'directory',**identity}
    if directory:
        raise PermissionError('Write ancestor is not a directory')
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>MAX_BYTES:
        raise PermissionError('Write target must be a bounded, unlinked regular file')
    with path.open('rb') as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev,opened.st_ino,opened.st_size,opened.st_nlink)!=(info.st_dev,info.st_ino,info.st_size,info.st_nlink):
            raise PermissionError('Write target changed before reading')
        data = source.read(MAX_BYTES+1)
    after = path.lstat()
    if len(data)>MAX_BYTES or (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):
        raise PermissionError('Write target changed while reading')
    return {'kind':'file',**identity,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),
            'mtime':info.st_mtime_ns,'mode':stat.S_IMODE(info.st_mode)}


def snapshot(paths):
    base = root()
    names = {'.'}
    for name in paths:
        target = relative(name)
        names.add(target.as_posix())
        names.update(p.as_posix() for p in target.parents if p.as_posix()!='.')
    result = {}
    for name in sorted(names,key=lambda n:len(Path(n).parts)):
        result[name] = state(base/name,directory=name not in paths)
        if name not in paths and result[name]['kind'] not in {'missing','directory'}:
            raise PermissionError('Write ancestor is not a directory')
    return result


def byte_content(text):
    if not isinstance(text,str):
        raise ValueError('File content must be text')
    data = text.replace('\n',os.linesep).encode('utf-8')
    if len(data)>MAX_BYTES:
        raise ValueError('File write exceeds 2 MiB')
    return data


def request(name,arguments,before=None):
    if name=='create_directory':
        if set(arguments)!={'paths'} or not isinstance(arguments['paths'],list) or not 1<=len(arguments['paths'])<=100:
            raise ValueError('Directory write requires 1..100 paths')
        paths = [relative(p).as_posix() for p in arguments['paths']]
        if len({os.path.normcase(p) for p in paths})!=len(paths):
            raise ValueError('Duplicate write path')
        return paths,None
    fields = {'path','content'} if name=='write_file' else {'path','old_str','new_str'}
    if name not in FILE_WRITE_TOOLS or set(arguments)!=fields:
        raise ValueError('Unsupported file-write arguments')
    target = relative(arguments['path'])
    if len(target.parts)<2:
        raise ValueError('Place files in an independent program directory')
    if name=='write_file':
        data = byte_content(arguments['content'])
    else:
        if not isinstance(arguments['old_str'],str) or not isinstance(arguments['new_str'],str):
            raise ValueError('Edit requires text arguments')
        path = root()/target
        if path.exists():
            with path.open('rb') as source:
                raw = source.read(MAX_BYTES+1)
            if len(raw)>MAX_BYTES:
                raise ValueError('Edit input exceeds 2 MiB')
            expected = (before or {}).get(target.as_posix())
            if expected is not None and (expected.get('kind')!='file' or expected.get('sha256')!=hashlib.sha256(raw).hexdigest()):
                raise PermissionError('Edit source differs from approved precondition')
            original = raw.decode('utf-8').replace('\r\n','\n').replace('\r','\n')
            if arguments['old_str'] not in original:
                raise ValueError('Edit precondition old_str not found')
            data = byte_content(original.replace(arguments['old_str'],arguments['new_str'],1))
        elif arguments['old_str']=='':
            data = byte_content(arguments['new_str'])
        else:
            raise ValueError('Edit file does not exist')
    return [target.as_posix()],data


def prepare(ledger,operation,call_id,name,arguments):
    ledger._step(operation)
    if not isinstance(call_id,str) or not 1<=len(call_id)<=256:
        raise ValueError('Invalid write tool-call ID')
    if not isinstance(arguments,dict):
        raise ValueError('File-write arguments must be an object')
    if name=='create_directory' and (not isinstance(arguments.get('paths'),list) or not 1<=len(arguments['paths'])<=100):
        raise ValueError('Directory write requires 1..100 paths')
    row = ledger.store.get(ledger.key)
    if name not in FILE_WRITE_TOOLS:
        return None
    if name not in capabilities(json.loads(row['context']),ledger.store.workflow_type(row)):
        raise PermissionError('File-write capability is not bound')
    from agent_execution.policy import current_context
    from permissions import current_role,get_user_role,check_tool_permission
    context = current_context()
    if (current_role()!='admin' or not check_tool_permission(name) or name not in context.allowed_tools
            or (context.identity!='local' and get_user_role(context.identity)!='admin')):
        raise PermissionError('Current role or write capability changed before planning')
    with closing(ledger.store.tasks.connect()) as conn:
        install(conn)
        old = conn.execute('SELECT * FROM temporal_file_writes WHERE submission_id=? AND operation_id=?',
                           (ledger.key,operation)).fetchone()
    if old:
        if (old['name'],old['call_id'],old['arguments_digest'])!=(name,call_id,digest(arguments)):
            raise PermissionError('Write operation parameters changed')
        return json.loads(old['plan'])
    # Check every ancestor before reading edit content, including link aliases.
    if name=='create_directory':
        preliminary = [relative(p).as_posix() for p in arguments.get('paths',[])]
    else:
        preliminary = [relative(arguments.get('path')).as_posix()]
    from agent_execution.workspace_lifecycle import LifecycleStore
    for path in preliminary:
        for parent in reversed((root()/path).parents):
            if parent==root() or root() in parent.parents:
                state(parent,directory=True)
        LifecycleStore().assert_writable(root()/path)
    before = snapshot(preliminary)
    paths,data = request(name,arguments,before)
    if before!=snapshot(paths):
        raise PermissionError('Write precondition changed')
    for path in paths:
        allowed = {'missing','directory'} if name=='create_directory' else {'missing','file'}
        if before[path]['kind'] not in allowed:
            raise ValueError('Write target has the wrong type')
        if before[path]['kind']=='file' and not before[path]['mode'] & stat.S_IWUSR:
            raise PermissionError('Write target is read-only')
    expected = {p:({'kind':'directory'} if data is None else {'kind':'file','bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}) for p in paths}
    plan = {'version':VERSION,'paths':paths,'before':before,'expected':expected}
    if len(canonical(plan).encode())>64*1024:
        raise ValueError('Write plan exceeds 64 KiB')
    with closing(ledger.store.tasks.connect()) as conn,conn:
        conn.execute('BEGIN IMMEDIATE')
        ledger._scope(conn)
        ledger._capacity(conn,len(canonical(plan).encode())+2048)
        install(conn)
        conn.execute('INSERT OR IGNORE INTO temporal_file_writes VALUES(?,?,?,?,?,?,NULL,?)',
                     (ledger.key,operation,call_id,name,digest(arguments),canonical(plan),time.time()))
        saved = conn.execute('SELECT * FROM temporal_file_writes WHERE submission_id=? AND operation_id=?',
                             (ledger.key,operation)).fetchone()
        if saved['plan']!=canonical(plan) or saved['arguments_digest']!=digest(arguments):
            raise PermissionError('Concurrent write plan changed')
    return plan


def for_cursor(ledger,snapshot):
    from agent_execution.turn_control.continuation import assistant_frontier
    message,finished = assistant_frontier(snapshot['checkpoint']['messages'])
    call = next((c for c in message.get('tool_calls') or [] if c['id'] not in finished),None)
    if not call or call['function']['name'] not in FILE_WRITE_TOOLS:
        return None
    operation = f"model:{snapshot['checkpoint']['iteration']}:tool:{call['id']}"
    return prepare(ledger,operation,call['id'],call['function']['name'],json.loads(call['function']['arguments']))


def load(ledger,operation,name,arguments):
    with closing(ledger.store.tasks.connect()) as conn:
        item = conn.execute('SELECT * FROM temporal_file_writes WHERE submission_id=? AND operation_id=?',
                            (ledger.key,operation)).fetchone()
    if not item or item['name']!=name or item['arguments_digest']!=digest(arguments):
        raise PermissionError('No exact approved write plan')
    return json.loads(item['plan'])


def verify(plan):
    if plan['version']!=VERSION or snapshot(plan['paths'])!=plan['before']:
        raise PermissionError('Approved write precondition changed')


def execute_bound(name,arguments,function):
    from .operations import _tool_operation
    operation = _tool_operation.get()
    if operation is None or name not in FILE_WRITE_TOOLS:
        from .operations import _ledger
        if name in FILE_WRITE_TOOLS and _ledger.get() is not None:
            raise PermissionError('File write requires an approved ledger operation')
        return function(**arguments)
    ledger,step = operation
    row = ledger.store.get(ledger.key)
    from permissions import current_role
    if current_role()!='admin' or json.loads(row['context']).get('file_writes')!=VERSION:
        raise PermissionError('File write requires the bound admin policy')
    plan = load(ledger,step,name,arguments)
    from .approval_gate import assert_consumed
    assert_consumed(ledger,step,name,arguments,plan)
    verify(plan)
    paths,data = request(name,arguments,plan['before'])
    if {p:({'kind':'directory'} if data is None else {'kind':'file','bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}) for p in paths}!=plan['expected']:
        raise PermissionError('Write content differs from approved digest')
    from agent_execution.workspace_lifecycle import prepare_write, program_build, LifecycleStore
    from agent_execution.budgets import active_budget
    created = set()
    stage = None
    with ExitStack() as stack:
        for p in paths:
            target = root()/p
            key = prepare_write(target/'.directory-admission' if name=='create_directory' else target)
            if key:
                stack.enter_context(program_build(LifecycleStore(),key))
                created.add(Path(p).parts[0])
        # Existing ancestors/targets cannot be silently adopted after approval.
        current = snapshot(paths)
        for p,before in plan['before'].items():
            if current[p]!=before and not (p in created and before['kind']=='missing' and current[p]['kind']=='directory'):
                raise PermissionError('Write target changed during lifecycle admission')
        expected_before = current
        made = set()
        def mkdir(path):
            if path==root():
                return
            name_in_root = path.relative_to(root()).as_posix()
            if path.exists():
                if state(path)['kind']!='directory':
                    raise PermissionError('Write ancestor is not a directory')
                if expected_before.get(name_in_root,{}).get('kind')=='missing' and name_in_root not in made and name_in_root not in created:
                    raise PermissionError('Missing directory was concurrently created')
                return
            mkdir(path.parent)
            path.mkdir()
            made.add(name_in_root)
        if data is not None:
            target = root()/paths[0]
            mkdir(target.parent)
            stage = target.parent/('.pythonai-tp17-'+uuid.uuid4().hex+'.tmp')
            stack.callback(lambda: stage.unlink(missing_ok=True) if stage is not None else None)
            with stage.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if plan['before'][paths[0]]['kind']=='file':
                stage.chmod(plan['before'][paths[0]]['mode'])
            # Parent directories created by this operation are now bound too.
            after_parents = snapshot(paths)
            for p,before in expected_before.items():
                if (before['kind']!='missing' or p in paths or (p not in made and p not in created)) and after_parents[p]!=before:
                    raise PermissionError('Existing write target changed while staging')
            expected_before = after_parents
        try:
            with closing(ledger.store.tasks.connect()) as conn,conn:
                conn.execute('BEGIN IMMEDIATE')
                ledger._scope(conn)
                active_budget().check()
                from agent_execution.policy import current_context
                from permissions import get_user_role
                if current_context().identity!='local' and get_user_role(current_context().identity)!='admin':
                    raise PermissionError('Admin role revoked before file commit')
                if snapshot(paths)!=expected_before:
                    raise PermissionError('Write precondition changed before commit')
                ledger._capacity(conn,len(canonical(expected_before).encode())+len(paths)*512)
                if data is None:
                    for p in paths:
                        mkdir(root()/p)
                else:
                    os.replace(stage,root()/paths[0])
                    stage = None
                observed = snapshot(paths)
                for p,expected in plan['expected'].items():
                    if any(observed[p].get(k)!=v for k,v in expected.items()):
                        raise PermissionError('Actual file write differs from approved result')
                ledger._capacity(conn,len(canonical(observed).encode()))
                conn.execute('UPDATE temporal_file_writes SET observation=?,updated=? WHERE submission_id=? AND operation_id=?',
                             (canonical(observed),time.time(),ledger.key,step))
        finally:
            if stage is not None:
                stage.unlink(missing_ok=True)
    return {'success':True,'path':str(root()/paths[0]) if data is not None else None,
            'directories':[str(root()/p) for p in paths] if data is None else [],
            'action':name,'evidence':{'kind':'temporal_file_write','operation':step,
                                    'plan_digest':digest(plan),'observed':observed}}
