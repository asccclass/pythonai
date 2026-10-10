import json
from agent_execution import policy_tool
from agent_execution.code_index.navigation import navigate

@policy_tool('find_references')
def find_references(file_path: str,line: int,column: int) -> str:
    return json.dumps(navigate(file_path,line,column,references=True),ensure_ascii=False)
