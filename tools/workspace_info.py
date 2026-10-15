"""Read-only model access to owned directory metadata and archive previews."""
from agent_execution import policy_tool
from agent_execution.workspace_lifecycle import LifecycleStore


@policy_tool('workspace_info')
def workspace_info(action='list',directory_id=None):
    store=LifecycleStore()
    if action=='list':return store.inventory()
    if action not in {'preview_archive','preview_restore'}:raise ValueError('Read-only workspace action required')
    result=store.preview('archive' if action=='preview_archive' else 'restore',directory_id)
    return {**result,'next_human_command':'/workspace confirm '+result['plan_id']}
