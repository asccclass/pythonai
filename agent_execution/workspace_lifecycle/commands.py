"""Human-only commands. Never register this dispatcher as a model tool."""
import json
import shlex
from permissions import current_role
from .store import LifecycleStore


def human_command(text):
    parts=shlex.split(text)
    if not parts or parts[0] != '/workspace':return None
    store=LifecycleStore()
    if parts[1:]==['list']:return json.dumps(store.inventory(),ensure_ascii=False)
    if len(parts)==4 and parts[1]=='preview':return json.dumps(store.preview(parts[3],parts[2]),ensure_ascii=False)
    if len(parts)==3 and parts[1] in {'retain','attach'}:
        getattr(store,parts[1])(parts[2])
        return 'Directory reference updated.'
    if len(parts)==4 and parts[1] in {'create','adopt'}:
        if current_role()!='admin':raise PermissionError('Directory creation/adoption requires admin')
        return store.create(parts[2],parts[3],adopt=parts[1]=='adopt')
    raise ValueError('Usage: /workspace list | create|adopt <relative-name> temporary|retained | attach|retain <directory-id> | preview <directory-id> archive|restore')
