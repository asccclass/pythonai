from agent_execution.process import run_process
from agent_execution.policy import policy_tool, git_workspace
import subprocess
import os

@policy_tool("git_commit")
def git_commit(message: str, add_all: bool = False, create_branch: str = "") -> str:
    """Commits changes, optionally creating a branch or adding all files."""
    repository = git_workspace()
    try:
        run_process(["git", "rev-parse", "--is-inside-work-tree"], check=True, capture_output=True, cwd=repository)
    except subprocess.CalledProcessError:
        return "Error: Current directory is not a git repository."

    output = []
    
    if create_branch:
        res = run_process(["git", "checkout", "-b", create_branch], capture_output=True, text=True, cwd=repository)
        if res.returncode != 0:
            return f"Error creating branch: {res.stderr}"
        output.append(f"Switched to new branch '{create_branch}'")
        
    if add_all:
        res = run_process(["git", "add", "."], capture_output=True, text=True, cwd=repository)
        if res.returncode != 0:
            return f"Error adding files: {res.stderr}"
        output.append("Added all untracked and modified files.")
        
    cmd = ["git", "commit", "-m", message]
    res = run_process(cmd, capture_output=True, text=True, cwd=repository)
    
    if res.returncode == 0:
        output.append(f"Successfully committed: {message}")
        output.append(res.stdout.strip())
        return "\n".join(output)
    else:
        return f"Commit failed:\n{res.stdout}\n{res.stderr}"
