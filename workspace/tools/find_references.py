import os
from pathlib import Path

def find_references(file_path: str, line: int, column: int) -> str:
    try:
        import jedi
    except ImportError:
        return "Error: jedi not installed"

    path = Path(file_path)
    if not path.is_absolute():
        path = Path(os.getcwd()) / path
        
    if not path.exists():
        return f"Error: File {path} not found"

    try:
        script = jedi.Script(path=str(path))
        refs = script.get_references(line=line, column=column)
        if not refs:
            return "No references found."
            
        results = []
        for r in refs:
            module_path = r.module_path or path
            try:
                line_code = r.get_line_code().strip()
            except:
                line_code = ""
            results.append(f"{module_path}:{r.line}:{r.column} - {line_code}")
        return "\n".join(results)
    except Exception as e:
        return f"Error finding references: {e}"
