from agent_execution import policy_tool, resolve_path
import os
from pathlib import Path

@policy_tool("go_to_definition")
def go_to_definition(file_path: str, line: int, column: int) -> str:
    try:
        import jedi
    except ImportError:
        return "Error: jedi not installed"

    path = resolve_path(file_path)
        
    if not path.exists():
        return f"Error: File {path} not found"

    try:
        script = jedi.Script(path=str(path))
        defs = script.goto(line=line, column=column)
        if not defs:
            return "No definition found."
            
        results = []
        for d in defs:
            module_path = d.module_path or path
            results.append(f"{module_path}:{d.line}:{d.column} - {d.description}")
        return "\n".join(results)
    except Exception as e:
        return f"Error finding definition: {e}"
