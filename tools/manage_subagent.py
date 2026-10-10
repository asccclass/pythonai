import json
from agent_execution import policy_tool
from agent_execution.coordinator import Coordinator,scope_owner,wait_node,merge_node,start_coordinator
from agent_execution.results import ToolResult
from agent_execution.policy import current_context


@policy_tool('manage_subagent')
def manage_subagent(agent_id: str,action: str,task: str = '',seconds: float = 0):
    store=Coordinator();owner=scope_owner();node=store.get(owner,agent_id)
    if action=='wait':
        value=wait_node(store,owner,agent_id,seconds)
        output={key:value[key] for key in ('id','state','result','reviewed_by')}
    elif action=='cancel':store.cancel(owner,agent_id);output={'agent_id':agent_id,'cancellation_requested':True}
    elif action=='followup':
        key=store.submit(node['persona'],task,previous=node)
        if current_context().coordinator_node is None:start_coordinator(store.path)
        output={'agent_id':key,'previous':agent_id}
    elif action=='merge':output=merge_node(store,owner,agent_id)
    else:raise ValueError('Unknown subagent action')
    return ToolResult(True,json.dumps(output,ensure_ascii=False),evidence=output)
