import os

content = ""
with open('communication_worker.py', 'r', encoding='utf-8') as f:
    content = f.read()

old_runner = '''def agent_runtime_command_runner(runtime: AgentRuntime) -> CommandRunner:
    def run(command: AgentCommand) -> str:
        with auto_approve_command_runs():
            return run_agent_turn(command.text, runtime).reply

    return run'''

new_runner = '''def agent_runtime_command_runner(runtime: AgentRuntime) -> CommandRunner:
    def run(command: AgentCommand) -> str:
        from permissions import get_user_role, set_current_role
        role = get_user_role(f"{command.platform}:{command.sender_id}")
        with auto_approve_command_runs(), set_current_role(role):
            return run_agent_turn(command.text, runtime).reply

    return run'''

content = content.replace(old_runner, new_runner)

with open('communication_worker.py', 'w', encoding='utf-8') as f:
    f.write(content)

content = ""
with open('base.py', 'r', encoding='utf-8') as f:
    content = f.read()

old_run_tool = '''def run_tool(tool_call):
    name = tool_call.function.name'''
    
new_run_tool = '''def run_tool(tool_call):
    name = tool_call.function.name
    try:
        from permissions import check_tool_permission
        if not check_tool_permission(name):
            return f"Error: Permission denied. Your role cannot execute {name}."
    except ImportError:
        pass'''

content = content.replace(old_run_tool, new_run_tool)

old_run_tool_ctx = '''def run_tool_with_context(tool_call, memory=None, episode_id: int | None = None):
    name = tool_call.function.name'''
    
new_run_tool_ctx = '''def run_tool_with_context(tool_call, memory=None, episode_id: int | None = None):
    name = tool_call.function.name
    try:
        from permissions import check_tool_permission
        if not check_tool_permission(name):
            return f"Error: Permission denied. Your role cannot execute {name}."
    except ImportError:
        pass'''

content = content.replace(old_run_tool_ctx, new_run_tool_ctx)

with open('base.py', 'w', encoding='utf-8') as f:
    f.write(content)