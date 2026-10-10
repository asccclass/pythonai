import json
from agent_execution import policy_tool
from agent_execution.code_index import CodeIndex

@policy_tool('refresh_code_index')
def refresh_code_index() -> str:
    return json.dumps(CodeIndex().refresh(),ensure_ascii=False)
