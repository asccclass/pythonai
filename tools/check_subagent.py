import json
from agent_execution import policy_tool
from agent_execution.coordinator import Coordinator,scope_owner
from agent_execution.results import ToolResult


@policy_tool('check_subagent')
def check_subagent(agent_id: str):
    node=Coordinator().get(scope_owner(),agent_id)
    data={key:node[key] for key in ('id','parent','persona','state','dependencies','review_of','reviewed_by','deadline','result')}
    return ToolResult(True,output=json.dumps(data,ensure_ascii=False),evidence={'agent_id':agent_id,'state':node['state']})
