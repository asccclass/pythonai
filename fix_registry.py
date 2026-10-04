import json

with open("base.py", "r", encoding="utf-8") as f:
    content = f.read()

# Fix TOOLS dictionary mapping
old_tools_str = """TOOLS = {
    "read_file": read_file,
    "list_files": list_files,
    "write_file": write_file,
    "delete_file": delete_file,
    "fetch_url": fetch_url,
    "run_command": run_command,
    "run_skill": run_skill,
    "manage_memory": manage_memory,
    "ask_user": ask_user,
}"""

new_tools_str = """TOOLS = {
    "read_file": read_file,
    "list_files": list_files,
    "write_file": write_file,
    "delete_file": delete_file,
    "fetch_url": fetch_url,
    "run_command": run_command,
    "run_skill": run_skill,
    "manage_memory": manage_memory,
    "ask_user": ask_user,
    "edit_file": edit_file,
    "search_web": search_web,
    "browse_webpage": browse_webpage,
    "analyze_image": analyze_image,
    "execute_python_script": execute_python_script,
    "create_dynamic_tool": create_dynamic_tool
}"""

content = content.replace(old_tools_str, new_tools_str)

with open("base.py", "w", encoding="utf-8") as f:
    f.write(content)

print("TOOLS dictionary patched.")
