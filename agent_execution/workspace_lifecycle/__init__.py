"""Owner-bound workspace directories. Human transports own destructive decisions."""
from .store import LifecycleStore


def inferred_purpose():
    import re
    from agent_execution.tasks import _active
    active = _active.get()
    goal = active[0].get(active[1], active[2])['goal'] if active else ''
    return 'temporary' if re.search(r'暫存|臨時|試作|temporary|scratch', goal, re.I) else 'retained'


def prepare_write(path):
    """Register newly created program directories; never adopt existing files implicitly."""
    from base import workspace_root
    root = workspace_root()
    relative = path.relative_to(root)
    store = LifecycleStore()
    store.assert_writable(path)
    if store.containing_directory(path):return None
    if len(relative.parts) > 1 and not (root / relative.parts[0]).exists():
        return store.create(relative.parts[0], inferred_purpose())


from contextlib import contextmanager


@contextmanager
def program_build(store,key):
    store.build_status(key,'building')
    outcome={'success':True}
    try:
        yield outcome
    except BaseException:
        store.build_status(key,'failed')
        raise
    else:store.build_status(key,'ready' if outcome['success'] else 'failed')


@contextmanager
def program_directory(relative):
    """Explicit managed destination for Skill/host output; existing unknown data stays unknown."""
    from contextlib import closing
    from base import workspace_root,resolve_workspace_path
    from agent_execution.policy import execution_scope
    from .store import principal
    root=workspace_root();target=resolve_workspace_path(relative)
    from .paths import directory
    directory(root,str(target.relative_to(root)))
    store=LifecycleStore()
    existing=store.containing_directory(target)
    if existing:
        key=existing;store.assert_writable(target)
    elif target.exists():
        with closing(store.connect()) as conn:
            row=conn.execute('SELECT d.id FROM wl_directories d JOIN wl_roots r ON r.id=d.root WHERE r.path=? AND d.relative=? AND d.principal=? AND d.state=\'active\'',(str(root),str(target.relative_to(root)),principal())).fetchone()
        if row is None:raise PermissionError('Existing program directory is unmanaged; explicit adoption required')
        key=row[0];store.assert_writable(target)
    else:key=store.create(str(target.relative_to(root)),inferred_purpose())
    with program_build(store,key) as outcome,execution_scope(workspace=target):yield outcome
