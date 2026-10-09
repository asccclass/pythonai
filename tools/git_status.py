from agent_execution.process import run_process
from agent_execution.policy import policy_tool, git_workspace
import subprocess
import os

@policy_tool("git_status")
def git_status() -> str:
    """Returns the current git status."""
    repository = git_workspace()
    try:
        # Check if it's a git repository
        run_process(["git", "rev-parse", "--is-inside-work-tree"], check=True, capture_output=True, cwd=repository)
    except subprocess.CalledProcessError:
        return "Error: Current directory is not a git repository."

    try:
        result = run_process(["git", "status"], capture_output=True, text=True, encoding="utf-8", cwd=repository)
        return result.stdout
    except Exception as e:
        return f"Error executing git status: {e}"
