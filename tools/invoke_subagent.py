from contextvars import copy_context
from agent_execution import policy_tool
import threading
import uuid
import json
import traceback
from pathlib import Path

from base import resolve_workspace_path

SUBAGENT_STATE_DIR = resolve_workspace_path("subagents")
SUBAGENT_STATE_DIR.mkdir(parents=True, exist_ok=True)

PERSONAS = {
    "planner": "You are a Manager/Planner Agent. Your job is to break down complex tasks into a detailed Markdown execution plan. Output a clear step-by-step checklist. Do not write code. Just plan.",
    "coder": "You are a Coder Worker Agent. Your job is to receive a specific task, use tools to write or modify code, and finish. Focus ONLY on executing the code changes. Do not plan.",
    "reviewer": "You are a Reviewer Agent. Your job is to review the code changes made by the coder, run tests, and report any bugs, security issues, or deviations from the plan."
}

def subagent_worker(agent_id: str, persona: str, task: str):
    state_file = resolve_workspace_path(f"subagents/{agent_id}.json")
    
    try:
        from server import run_agent, build_system_prompt
        base_prompt = build_system_prompt()
        persona_prompt = PERSONAS.get(persona, PERSONAS["coder"])
        
        system_prompt = (
            f"{base_prompt}\n\n"
            "=================================\n"
            "MULTI-AGENT DIRECTIVE:\n"
            f"{persona_prompt}\n"
            "Focus ONLY on your role. When you are done, summarize the results and stop calling tools.\n"
            "================================="
        )
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Task:\n{task}"}
        ]
        
        from agent_execution.budgets import child_scope
        from agent_execution.policy import execution_scope, current_context
        from agent_execution.tasks import durable_invoke
        with child_scope(), execution_scope(source='subagent',task_id=f'subagent:{agent_id}'):
            def invoke(saved):
                return run_agent(messages,max_retries=3,resume_state=saved,
                    check_cancelled=current_context().check_cancelled)
            result = durable_invoke(invoke,messages,None)
        
        state_file.write_text(json.dumps({
            "status": "completed",
            "result": result
        }, ensure_ascii=False), encoding="utf-8")
        
    except Exception as e:
        error_msg = f"Subagent crashed: {e}\n{traceback.format_exc()}"
        state_file.write_text(json.dumps({
            "status": "failed",
            "result": error_msg
        }, ensure_ascii=False), encoding="utf-8")


@policy_tool("invoke_subagent")
def invoke_subagent(persona: str, task: str) -> str:
    """Spawns a subagent asynchronously and returns its task ID."""
    
    if persona not in PERSONAS:
        return f"Error: persona must be one of {list(PERSONAS.keys())}"
    from agent_execution.budgets import active_budget, _depth
    budget=active_budget()
    if budget is not None:
        budget.reserve_child(_depth.get())
        
    agent_id = str(uuid.uuid4())[:8]
    state_file = resolve_workspace_path(f"subagents/{agent_id}.json")
    
    state_file.parent.mkdir(parents=True, exist_ok=True)
    # Initialize state as running
    state_file.write_text(json.dumps({
        "status": "running",
        "result": None
    }, ensure_ascii=False), encoding="utf-8")
    
    # Start thread
    context = copy_context()
    thread = threading.Thread(target=context.run, args=(subagent_worker, agent_id, persona, task))
    thread.daemon = True
    thread.start()
    
    return f"Successfully spawned '{persona}' subagent in the background.\nAgent ID: {agent_id}\nUse 'check_subagent' tool to poll its status and get the result."
