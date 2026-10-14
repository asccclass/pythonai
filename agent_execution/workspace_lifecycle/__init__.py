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
    if len(relative.parts) > 1 and not (root / relative.parts[0]).exists():
        store.create(relative.parts[0], inferred_purpose())
