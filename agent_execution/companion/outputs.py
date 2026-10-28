"""Conversation-owned file delivery, independent of model claims or local paths."""
import base64
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import stat
import uuid

from agent_execution.policy import current_context
from agent_execution.workspace_lifecycle import LifecycleStore
from agent_execution.workspace_lifecycle.paths import identity, snapshot
from agent_execution.workspace_lifecycle.store import session_key

MAX_BYTES = 64 * 1024 * 1024
CHUNK_BYTES = 256 * 1024
DOCUMENT_SUFFIXES = {'.txt', '.md', '.csv', '.tsv', '.json', '.xml', '.html', '.pdf',
                     '.docx', '.xlsx', '.pptx', '.png', '.jpg', '.jpeg', '.zip'}


def initialize(store):
    with closing(store.connect()) as conn, conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS companion_outputs (
            id TEXT PRIMARY KEY, session TEXT NOT NULL, task TEXT, root TEXT NOT NULL,
            path TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL,
            sha256 TEXT NOT NULL, identity TEXT NOT NULL, state TEXT NOT NULL,
            UNIQUE(root,path))''')
    from .turn_cleanup import initialize as initialize_cleanup
    initialize_cleanup(store)


def checked_path(row):
    root = Path(row['root'])
    path = root / row['path']
    if not path.resolve().is_relative_to(root.resolve()):
        raise PermissionError('Output is outside workspace')
    for parent in [root, *list(path.relative_to(root).parents)[:-1][::-1]]:
        identity(parent if parent.is_absolute() else root / parent)
    ident = identity(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PermissionError('Output must be a regular, unlinked file')
    if json.dumps(ident) != row['identity']:
        raise PermissionError('Output file was replaced')
    return path


def public(row):
    return {k: row[k] for k in ('id', 'name', 'size', 'sha256')}


def requested_destination(path):
    """An explicitly named output can replace a previous unregistered delivery."""
    from agent_execution.tasks import _active
    active = _active.get()
    if not active:
        return False
    goal = active[0].get(active[1], active[2])['goal']
    verb = r'(?:寫入|另存(?:為|成)?|輸出(?:至|到|為)?|儲存(?:至|到|為)?|存成|write\s+(?:to|into)|save\s+(?:as|to))'
    destination = r'\s*(?:檔案|file)?\s*[`\"\x27]?(?:[^\s`\"\x27，。；;]*[\\/])?'
    ending = r'(?=$|[\s`\"\x27，。；;]|中|裡|內)'
    return bool(re.search(verb + destination + re.escape(path.name) + ending, goal, re.I))


def register(path, newly_created):
    """Only new documents and this conversation's previous outputs are adopted."""
    context = current_context()
    if context.source != 'websocket' or path.suffix.lower() not in DOCUMENT_SUFFIXES:
        return None
    from base import workspace_root
    root = workspace_root()
    relative = path.relative_to(root).as_posix()
    store = LifecycleStore()
    initialize(store)
    managed = store.containing_directory(path)
    if managed and store.get(managed)['purpose'] == 'retained':
        return None  # Program resources keep their existing retention policy.
    with closing(store.connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        session = store.ensure_session(conn)
        existing = conn.execute("SELECT * FROM companion_outputs WHERE root=? AND path=? AND state='active'",
                                (str(root), relative)).fetchone()
        if existing and existing['session'] != session:
            raise PermissionError('Output belongs to another conversation; use a different filename')
        if not newly_created and not existing and not requested_destination(path):
            return None
        info = path.stat()
        if info.st_size > MAX_BYTES:
            raise ValueError('Downloadable output exceeds 64 MiB')
        row = dict(id=existing['id'] if existing else uuid.uuid4().hex, session=session,
                   task=context.task_id, root=str(root), path=relative, name=path.name,
                   size=info.st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                   identity=json.dumps(identity(path)), state='active')
        checked_path(row)
        conn.execute('INSERT OR REPLACE INTO companion_outputs VALUES(:id,:session,:task,:root,:path,:name,:size,:sha256,:identity,:state)', row)
    return public(row)


def assert_writable(path):
    if current_context().source != 'websocket':
        return
    from base import workspace_root
    store = LifecycleStore()
    initialize(store)
    with closing(store.connect()) as conn:
        row = conn.execute("SELECT session,state FROM companion_outputs WHERE root=? AND path=? AND state IN ('active','deleting')",
                           (str(workspace_root()), path.relative_to(workspace_root()).as_posix())).fetchone()
        if row and row['session'] != session_key():
            raise PermissionError('Output belongs to another conversation; use a different filename')
        if row and row['state']=='deleting':
            raise PermissionError('Output file cleanup is in progress')


def rows(store, conn, task=None, include_deleting=False):
    query = "SELECT * FROM companion_outputs WHERE session=? AND " + ("state IN ('active','deleting')" if include_deleting else "state='active'")
    args = [session_key()]
    if task is not None:
        query += ' AND task=?'
        args.append(task)
    return [dict(row) for row in conn.execute(query, args)]


def available(task=None):
    store = LifecycleStore()
    initialize(store)
    with closing(store.connect()) as conn:
        result = []
        for row in rows(store, conn, task):
            try:
                path = checked_path(row)
                if path.stat().st_size == row['size'] and hashlib.sha256(path.read_bytes()).hexdigest() == row['sha256']:
                    result.append(public(row))
            except (OSError, ValueError):
                continue
        return result[:64]


def read_chunk(key, offset):
    if type(offset) is not int or offset < 0:
        raise ValueError('Invalid download offset')
    store = LifecycleStore()
    initialize(store)
    with closing(store.connect()) as conn, conn:
        store.ensure_session(conn)
        row = conn.execute("SELECT * FROM companion_outputs WHERE id=? AND session=? AND state='active'",
                           (key, session_key())).fetchone()
        if row is None:
            raise PermissionError('File does not belong to this conversation')
        row = dict(row)
        conn.commit()
        path = checked_path(row)
        store.assert_access(path)
        if offset > row['size'] or path.stat().st_size != row['size']:
            raise ValueError('Output changed; regenerate the file')
        with path.open('rb') as stream:
            stream.seek(offset)
            data = stream.read(CHUNK_BYTES)
        return {**public(row), 'offset': offset, 'data': base64.b64encode(data).decode('ascii'),
                'eof': offset + len(data) == row['size']}


def preview(store, conn, task=None):
    result = []
    for row in rows(store, conn,task,include_deleting=True):
        try:
            path = checked_path(row)
            manifest = snapshot(path)
            result.append({**row, 'digest': manifest['digest'], 'missing': False})
        except FileNotFoundError:
            result.append({**row, 'missing': True})
    return result


def purge(store, items):
    from agent_execution.workspace_lifecycle.native import PinnedTree
    for row in items:
        try:
            path = checked_path(row)
            if row['missing']:
                raise PermissionError('A missing output was recreated after preview')
            with PinnedTree(path) as locked:
                if locked.digest != row['digest']:
                    raise PermissionError('Output changed since deletion preview')
                locked.purge()
        except FileNotFoundError:
            pass  # Manual deletion is already the requested end state.
        with closing(store.connect()) as conn, conn:
            conn.execute("UPDATE companion_outputs SET state='deleted' WHERE id=? AND session=?",
                         (row['id'], session_key()))
