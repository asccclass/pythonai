from agent_execution import policy_tool
from agent_execution.coordinator import Coordinator,scope_owner,start_coordinator
from agent_execution.results import ToolResult
from agent_execution.policy import current_context


@policy_tool('invoke_subagent')
def invoke_subagent(persona: str,task: str,dependencies: list[str] | None = None):
    store=Coordinator()
    key=store.submit(persona,task,dependencies)
    if current_context().coordinator_node is None:start_coordinator(store.path)
    return ToolResult(True,output='Agent queued: '+key,evidence={'agent_id':key,'status':'queued'})
