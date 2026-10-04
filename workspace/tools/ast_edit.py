import os
import sys
import subprocess
from pathlib import Path

def ast_edit(file_path: str, node_type: str, node_name: str, new_code: str) -> str:
    """
    node_type: 'function_definition' or 'class_definition'
    """
    try:
        import tree_sitter_python as tspython
        from tree_sitter import Language, Parser
    except ImportError:
        return "Error: tree_sitter or tree_sitter_python not installed"

    path = Path(file_path)
    if not path.is_absolute():
        path = Path(os.getcwd()) / path
        
    if not path.exists():
        return f"Error: File {path} not found"
        
    code_bytes = path.read_bytes()
    
    PY_LANGUAGE = Language(tspython.language())
    parser = Parser(PY_LANGUAGE)
    tree = parser.parse(code_bytes)
    
    def find_node(node):
        if node.type == node_type:
            # find identifier child
            for child in node.children:
                if child.type == 'identifier' and child.text.decode('utf8') == node_name:
                    return node
        for child in node.children:
            found = find_node(child)
            if found: return found
        return None
        
    target = find_node(tree.root_node)
    if not target:
        return f"Error: {node_type} named '{node_name}' not found in {path.name}."
        
    start_byte = target.start_byte
    end_byte = target.end_byte
    
    new_code_bytes = new_code.encode('utf8')
    modified_bytes = code_bytes[:start_byte] + new_code_bytes + code_bytes[end_byte:]
    
    # Save original to rollback if linter fails
    path.write_bytes(modified_bytes)
    
    # Run syntax check / linter using the current python executable
    try:
        # Check syntax first
        result = subprocess.run([sys.executable, '-m', 'py_compile', str(path)], capture_output=True, text=True)
        if result.returncode != 0:
            path.write_bytes(code_bytes)
            return f"Error: Syntax error introduced. Rollbacked changes.\n{result.stderr}"
            
        # Run flake8 for style/indent checks
        result = subprocess.run([sys.executable, '-m', 'flake8', str(path)], capture_output=True, text=True)
        if result.returncode != 0:
            # Maybe just warn if it's only style errors?
            # Or rollback? Let's rollback to force the agent to write perfect code
            path.write_bytes(code_bytes)
            return f"Error: Linter check failed. Rollbacked changes. Please fix these issues:\n{result.stdout}\n{result.stderr}"
            
    except Exception as e:
        path.write_bytes(code_bytes)
        return f"Error running linter: {e}"
        
    return f"Successfully replaced {node_type} '{node_name}' and passed all syntax & linter checks."
