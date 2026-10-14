"""Human-only commands. Never register this dispatcher as a model tool."""
import json
import shlex
from permissions import current_role
from .store import LifecycleStore


def human_command(text):
    parts=shlex.split(text)
    if not parts or parts[0] not in {'/workspace','/session-delete'}:return None
    store=LifecycleStore()
    if parts[0]=='/session-delete':
        if parts[1:]==['preview']:return json.dumps(store.preview('delete_session'),ensure_ascii=False)
        if len(parts)==3 and parts[1]=='confirm':return json.dumps(store.confirm(parts[2]),ensure_ascii=False)
        if len(parts)==3 and parts[1]=='status':
            with store.connect() as conn:
                store.plan(parts[2],conn)
                row=conn.execute('SELECT id,state,error FROM wl_operations WHERE id=?',(parts[2],)).fetchone()
            return json.dumps(dict(row) if row else {'state':'preview'},ensure_ascii=False)
        raise ValueError('Usage: /session-delete preview | confirm|status <plan-id>')
    if parts[1:]==['list']:return json.dumps(store.inventory(),ensure_ascii=False)
    if len(parts)==4 and parts[1]=='preview':return json.dumps(store.preview(parts[3],parts[2]),ensure_ascii=False)
    if len(parts)==3 and parts[1]=='request':return json.dumps(store.request(parts[2]),ensure_ascii=False)
    if len(parts)==3 and parts[1]=='confirm':return json.dumps(store.confirm(parts[2]),ensure_ascii=False)
    if len(parts)==4 and parts[1]=='execute':return json.dumps(store.execute(parts[2],parts[3]),ensure_ascii=False)
    if len(parts)==3 and parts[1]=='resume':
        from .operations import resume
        return json.dumps(resume(store,parts[2]),ensure_ascii=False)
    if len(parts)==3 and parts[1] in {'retain','attach'}:
        getattr(store,parts[1])(parts[2])
        return 'Directory reference updated.'
    if len(parts)==4 and parts[1] in {'create','adopt'}:
        if current_role()!='admin':raise PermissionError('Directory creation/adoption requires admin')
        return store.create(parts[2],parts[3],adopt=parts[1]=='adopt')
    raise ValueError('Usage: /workspace list | create|adopt <relative-name> temporary|retained | attach|retain <directory-id> | preview <directory-id> archive|restore')
