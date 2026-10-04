import os

content = ""
with open('base.py', 'r', encoding='utf-8') as f:
    content = f.read()

old_run_tool_ctx = '''    try:
        from permissions import check_tool_permission
        if not check_tool_permission(name):
            return f"Error: Permission denied. Your role cannot execute {name}."
    except ImportError:
        pass'''
        
new_run_tool_ctx = '''    try:
        from permissions import check_tool_permission, current_role
        if not check_tool_permission(name):
            error_msg = f"Error: Permission denied. Role '{current_role()}' cannot execute {name}."
            print(f"AUDIT: {error_msg}")
            if memory and episode_id:
                try:
                    from agent_runtime import log_episode_event
                    log_episode_event(memory, episode_id, "permission_denied", content=error_msg, metadata={"tool": name, "role": current_role()})
                except Exception:
                    pass
            return error_msg
    except ImportError:
        pass'''

content = content.replace(old_run_tool_ctx, new_run_tool_ctx)

with open('base.py', 'w', encoding='utf-8') as f:
    f.write(content)