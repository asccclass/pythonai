from agent_execution.process import run_process
from agent_execution import policy_tool, resolve_path
import subprocess
import os
import json
from pathlib import Path
from base import validate_command_paths

@policy_tool("env_manager")
def env_manager(action: str, project_path: str, packages: list[str] = None) -> str:
    """
    Manage virtual environments and project dependencies.
    """
    if packages is None:
        packages = []
        
    validate_command_paths(packages)
    project_dir = resolve_path(project_path)
    if not project_dir.exists():
        return f"Error: Project directory {project_dir} does not exist."
        
    try:
        if action == "create_venv":
            return _create_venv(project_dir)
        elif action == "install_pip":
            return _install_pip(project_dir, packages)
        elif action == "install_npm":
            return _install_npm(project_dir, packages)
        else:
            return f"Error: Unknown action '{action}'."
    except Exception as e:
        return f"Env Manager Error: {str(e)}"

def _create_venv(project_dir: Path) -> str:
    venv_dir = resolve_path(project_dir / "venv")
    if venv_dir.exists():
        return f"Virtual environment already exists at {venv_dir}"
        
    result = run_process(["python", "-m", "venv", "venv"], cwd=project_dir, capture_output=True, text=True)
    if result.returncode != 0:
        return f"Failed to create venv:\n{result.stderr}"
    return "Successfully created Python virtual environment in 'venv' folder."

def _install_pip(project_dir: Path, packages: list[str]) -> str:
    if not packages:
        return "No packages to install."
        
    # Check if venv exists
    venv_python = project_dir / "venv" / "Scripts" / "python.exe" if os.name == 'nt' else project_dir / "venv" / "bin" / "python"
    
    resolve_path(venv_python)
    cmd = [str(venv_python) if venv_python.exists() else "python", "-m", "pip", "install"] + packages
    
    result = run_process(cmd, cwd=project_dir, capture_output=True, text=True)
    if result.returncode != 0:
        return f"Failed to install pip packages:\n{result.stderr}"
        
    # Update requirements.txt
    req_file = resolve_path(project_dir / "requirements.txt")
    existing = set()
    if req_file.exists():
        with open(req_file, "r", encoding="utf-8") as f:
            existing = set(line.strip() for line in f if line.strip())
            
    for pkg in packages:
        # Simplistic parsing (assuming package names without versions for easy append)
        # Real env manager would use `pip freeze` and resolve conflicts.
        base_pkg = pkg.split("==")[0].split(">=")[0].split("<=")[0]
        if not any(base_pkg in line for line in existing):
            existing.add(pkg)
            
    with open(req_file, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(existing)) + "\n")
        
    return f"Successfully installed {packages} and updated requirements.txt\nLog:\n{result.stdout}"

def _install_npm(project_dir: Path, packages: list[str]) -> str:
    if not packages:
        # Just npm install the existing package.json
        cmd = ["npm", "install"]
    else:
        cmd = ["npm", "install"] + packages
        
    # On Windows, npm must be called via shell or as npm.cmd
    executable = "npm.cmd" if os.name == "nt" else "npm"
    cmd[0] = executable
    
    result = run_process(cmd, cwd=project_dir, capture_output=True, text=True)
    if result.returncode != 0:
        return f"Failed to install npm packages:\n{result.stderr}"
        
    return f"Successfully installed npm packages.\nLog:\n{result.stdout}"
