import json

def patch_base_py():
    with open('base.py', 'r', encoding='utf-8') as f:
        content = f.read()

    # 1. Add import for manage_memory
    if 'from memory_tools import manage_memory' not in content:
        content = 'from memory_tools import manage_memory\n' + content

    # 2. Add manage_memory to TOOLS
    tools_dict_str = '''TOOLS = {
    "read_file": read_file,
    "list_files": list_files,
    "write_file": write_file,
    "delete_file": delete_file,
    "fetch_url": fetch_url,
    "run_command": run_command,
    "run_skill": run_skill,
    "manage_memory": manage_memory,
}'''
    
    # Replace old TOOLS dict
    old_tools_start = content.find('TOOLS = {')
    old_tools_end = content.find('}', old_tools_start) + 1
    content = content[:old_tools_start] + tools_dict_str + content[old_tools_end:]

    # 3. Add manage_memory schema to TOOLS_SCHEMAS
    schema = {
        "type": "function",
        "function": {
            "name": "manage_memory",
            "description": "Manage long-term semantic memories. Use this to correct, archive, supersede, or confirm memories when asked by the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "The action to perform: 'confirm', 'archive', 'contradict', or 'supersede'",
                        "enum": ["confirm", "archive", "contradict", "supersede"]
                    },
                    "memory_id": {
                        "type": "integer",
                        "description": "The ID of the memory to modify (e.g., 123)"
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": "Set to true only if the user has explicitly confirmed this high-risk action in the chat."
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason for the modification."
                    },
                    "superseded_by": {
                        "type": "integer",
                        "description": "ID of the new memory that supersedes this one (required for 'supersede' action)."
                    }
                },
                "required": ["action", "memory_id"]
            }
        }
    }
    
    schema_str = ",\n    " + json.dumps(schema, indent=4).replace('\n', '\n    ') + "\n]"
    content = content.replace('\n]', schema_str)

    with open('base.py', 'w', encoding='utf-8') as f:
        f.write(content)

if __name__ == '__main__':
    patch_base_py()