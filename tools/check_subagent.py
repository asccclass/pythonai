import json
from pathlib import Path

from base import resolve_workspace_path

SUBAGENT_STATE_DIR = resolve_workspace_path("subagents")

def check_subagent(agent_id: str) -> str:
    """Checks the status and result of an asynchronously spawned subagent."""
    
    state_file = SUBAGENT_STATE_DIR / f"{agent_id}.json"
    
    if not state_file.exists():
        return f"Error: Agent ID '{agent_id}' not found."
        
    try:
        content = state_file.read_text(encoding="utf-8")
        data = json.loads(content)
        
        status = data.get("status")
        result = data.get("result")
        
        if status == "running":
            return f"Subagent '{agent_id}' is still running. Please poll again later."
        elif status == "completed":
            return f"Subagent '{agent_id}' COMPLETED successfully. Result:\n\n{result}"
        elif status == "failed":
            return f"Subagent '{agent_id}' FAILED. Error:\n\n{result}"
        else:
            return f"Subagent '{agent_id}' has unknown status '{status}'."
            
    except Exception as e:
        return f"Error reading subagent state: {e}"
