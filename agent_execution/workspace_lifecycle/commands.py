"""Human-only commands. Never register this dispatcher as a model tool."""
import json
import shlex
from contextlib import closing
from permissions import current_role
from .store import LifecycleStore


def human_command(text,runtime=None):
    if text.strip().split(maxsplit=1)[0:1] not in [['/workspace'],['/session-delete']]:return None
    parts=shlex.split(text)
    if not parts or parts[0] not in {'/workspace','/session-delete'}:return None
    store=LifecycleStore()
    if parts[0]=='/session-delete':
        from agent_execution.policy import current_context
        if current_context().source not in {'local','websocket','api','communication'} or (current_context().source=='communication' and current_context().communication_db is None):
            raise PermissionError('Session deletion requires a registered human transport')
        if parts[1:]==['preview']:return json.dumps(store.preview('delete_session'),ensure_ascii=False)
        if len(parts)==3 and parts[1]=='confirm':
            if store.plan(parts[2])['action']!='delete_session':raise ValueError('Not a session deletion')
            result=store.confirm(parts[2])
            if result['state']=='completed':
                from .erasure import finalize
                from agent_execution.delivery import EventStore
                result=finalize(store,parts[2],runtime,EventStore())
            return json.dumps(result,ensure_ascii=False)
        if len(parts)==3 and parts[1] in {'status','resume'}:
            plan=store.plan(parts[2])
            if plan['action']!='delete_session':raise ValueError('Not a session deletion')
            if parts[1]=='resume':
                from .operations import resume
                resume(store,parts[2])
            with closing(store.connect()) as conn:
                row=conn.execute('SELECT id,state,error FROM wl_operations WHERE id=?',(parts[2],)).fetchone()
            if row and row['state']=='completed':
                from .erasure import finalize
                from agent_execution.delivery import EventStore
                return json.dumps(finalize(store,parts[2],runtime,EventStore()),ensure_ascii=False)
            return json.dumps({'operation_id':parts[2],**(dict(row) if row else {'state':'preview'})},ensure_ascii=False)
        raise ValueError('Usage: /session-delete preview | confirm|status|resume <plan-id>')
    if parts[1:]==['list']:return json.dumps(store.inventory(),ensure_ascii=False)
    if len(parts)==3 and parts[1]=='recover-build':
        store.recover_build(parts[2])
        return 'Interrupted build marked failed; existing files retained.'
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
