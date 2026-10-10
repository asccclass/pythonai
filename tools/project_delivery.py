import json
from agent_execution import policy_tool
from agent_execution.results import ToolResult
from agent_execution.git_delivery import prepare_delivery,publish_delivery,inspect_delivery,merge_delivery,reconcile_delivery
from agent_execution.patches import ArtifactStore


@policy_tool('project_delivery')
def project_delivery(action: str,plan_id: str='',files: list[str] | None=None,branch: str='',title: str='',body: str='',previous_id: str=''):
    if action=='prepare':value=prepare_delivery(files,branch,title,body,previous_id)
    elif action=='publish':value=publish_delivery(plan_id)
    elif action=='inspect':value=inspect_delivery(plan_id)
    elif action=='merge':value=merge_delivery(plan_id)
    elif action=='reconcile':value=reconcile_delivery(plan_id)
    elif action=='status':value=ArtifactStore().get(plan_id,'delivery')
    else:raise ValueError('Unknown delivery action')
    return ToolResult(True,json.dumps(value,ensure_ascii=False),evidence=value,
        side_effects={'status':'observed' if action in {'publish','merge'} else 'none','operation':action})
