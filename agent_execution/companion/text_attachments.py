"""Keep uploaded text intact on disk instead of pinning it all in model context."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re

from agent_execution.workspace_lifecycle import LifecycleStore
from agent_execution.workspace_lifecycle.store import session_key

REFERENCE_PREFIX = '[PythonAI uploaded text reference]\n'
INLINE_BYTES = 1024


def traditional_translation(goal):
    return bool(re.search(r'翻[譯译]|translate|translation', goal, re.I)
                and re.search(r'繁[體体]|traditional\s+chinese|zh[-_](?:tw|hant)', goal, re.I))


def stage_text(filename, text):
    from base import workspace_root
    root = workspace_root()
    data = text.encode('utf-8')
    digest = hashlib.sha256(data).hexdigest()
    key = hashlib.sha256((session_key()+'\0'+filename+'\0'+digest).encode()).hexdigest()[:24]
    relative = 'uploaded-text-' + key
    store = LifecycleStore()
    with closing(store.connect()) as conn:
        row = conn.execute("SELECT d.id FROM wl_directories d JOIN wl_roots r ON r.id=d.root "
                           "WHERE r.path=? AND d.relative=? AND d.state='active'", (str(root),relative)).fetchone()
    directory_id = store.create(relative, 'temporary') if row is None else row[0]
    target = root/relative/'source.txt'
    store.assert_access(target, True)
    if not target.exists():
        try:
            with target.open('xb') as stream:
                stream.write(data)
        except FileExistsError:
            pass
    if target.is_symlink() or target.read_bytes() != data:
        raise PermissionError('Uploaded source changed; upload again')
    from agent_execution.policy import current_context
    if current_context().task_id:
        with closing(store.connect()) as conn, conn:
            session = store.ensure_session(conn)
            conn.execute('INSERT OR IGNORE INTO companion_attachment_refs VALUES(?,?,?)',
                         (session,current_context().task_id,directory_id))
    reference = {'name': filename, 'path': target.relative_to(root).as_posix(),
                 'directory_id':directory_id,
                 'sha256': digest, 'bytes': len(data), 'characters': len(text),
                 'read_with': {'tool':'read_file','offset':0,'limit':1024}}
    return {'type':'text', 'text':REFERENCE_PREFIX + json.dumps(reference,ensure_ascii=False)}


def references(messages):
    message = next((m for m in reversed(messages) if m.get('role')=='user'), {})
    parts = message.get('content')
    if not isinstance(parts,list) or len(parts)<2:
        return '', []
    goal = parts[0].get('text', '')
    result = []
    for part in parts[1:]:
        text = part.get('text','')
        if part.get('type')!='text' or not text.startswith(REFERENCE_PREFIX):
            return goal, []
        value = json.loads(text[len(REFERENCE_PREFIX):])
        if not isinstance(value,dict):
            raise ValueError('Invalid uploaded text reference')
        result.append(value)
    return goal, result


def read_reference(reference):
    from agent_execution.workspace_lifecycle.paths import identity
    store = LifecycleStore()
    row = store.get(reference['directory_id'])
    if row['state']!='active' or row['purpose']!='temporary' or not row['relative'].startswith('uploaded-text-'):
        raise PermissionError('Uploaded source is not a registered temporary attachment')
    if reference['path']!=(Path(row['relative'])/'source.txt').as_posix():
        raise PermissionError('Uploaded source reference does not match its registered directory')
    with closing(store.connect()) as conn:
        if not conn.execute('SELECT 1 FROM wl_refs WHERE directory=? AND session=?',
                            (row['id'],session_key())).fetchone():
            raise PermissionError('Uploaded source belongs to another conversation')
    target = store.locate(row)/'source.txt'
    store.assert_access(target)
    identity(target)
    if type(reference['bytes']) is not int or not 0<=reference['bytes']<=32*1024*1024:
        raise ValueError('Uploaded text size is invalid')
    with target.open('rb') as stream:
        data = stream.read(reference['bytes']+1)
    if len(data)!=reference['bytes'] or hashlib.sha256(data).hexdigest()!=reference['sha256']:
        raise ValueError('Uploaded source changed; upload again')
    text = data.decode('utf-8')
    if len(text)!=reference['characters']:
        raise ValueError('Uploaded source length changed')
    return text
