import json
from agent_execution import policy_tool
from agent_execution.patches import prepare_patch
from agent_execution.results import ToolResult


@policy_tool('prepare_project_patch')
def prepare_project_patch(changes: list):
    value=prepare_patch(changes)
    return ToolResult(True,json.dumps(value,ensure_ascii=False),evidence=value)
