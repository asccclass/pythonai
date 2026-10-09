from agent_execution.process import run_process
from agent_execution.policy import policy_tool, git_workspace
import subprocess
import os

@policy_tool("git_rollback")
def git_rollback() -> str:
    """Rollbacks all uncommitted changes (git reset --hard HEAD and git clean -fd)."""
    repository = git_workspace()
    try:
        run_process(["git", "rev-parse", "--is-inside-work-tree"], check=True, capture_output=True, cwd=repository)
    except subprocess.CalledProcessError:
        return "Error: Current directory is not a git repository."
        
    output = []
    
    res1 = run_process(["git", "reset", "--hard", "HEAD"], capture_output=True, text=True, cwd=repository)
    output.append(res1.stdout.strip() or res1.stderr.strip())
    
    res2 = run_process(["git", "clean", "-fd"], capture_output=True, text=True, cwd=repository)
    if res2.stdout.strip():
        output.append(res2.stdout.strip())
        
    return "\n".join(output)
