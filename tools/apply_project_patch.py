import json
from agent_execution import policy_tool
from agent_execution.patches import apply_patch
from agent_execution.results import ToolResult


@policy_tool('apply_project_patch')
def apply_project_patch(patch_id: str):
    value=apply_patch(patch_id)
    return ToolResult(True,json.dumps(value,ensure_ascii=False),evidence=value,side_effects={'status':'observed','patch_id':patch_id})
