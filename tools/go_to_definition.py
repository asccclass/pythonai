import json
from agent_execution import policy_tool
from agent_execution.code_index.navigation import navigate

@policy_tool('go_to_definition')
def go_to_definition(file_path: str,line: int,column: int) -> str:
    return json.dumps(navigate(file_path,line,column),ensure_ascii=False)
