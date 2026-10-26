from agent_execution import policy_tool
from agent_execution.coordinator import Coordinator,scope_owner,start_coordinator
from agent_execution.results import ToolResult
from agent_execution.policy import current_context


@policy_tool('invoke_subagent')
def invoke_subagent(persona: str,task: str,dependencies: list[str] | None = None):
    store=Coordinator()
    key=store.submit(persona,task,dependencies)
    evidence={'agent_id':key,'status':'queued'}
    node=store.get(scope_owner(),key)
    if current_context().coordinator_node is None and not node['payload'].get('temporal_graph'):
        start_coordinator(store.path)
    if node['payload'].get('temporal_graph'):
        evidence.update(backend='temporal',graph_id=node['payload']['temporal_graph'])
    return ToolResult(True,output='Agent queued: '+key,evidence=evidence)
