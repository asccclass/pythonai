import subprocess
import os

def git_rollback() -> str:
    """Rollbacks all uncommitted changes (git reset --hard HEAD and git clean -fd)."""
    try:
        subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], check=True, capture_output=True, cwd=os.getcwd())
    except subprocess.CalledProcessError:
        return "Error: Current directory is not a git repository."
        
    output = []
    
    res1 = subprocess.run(["git", "reset", "--hard", "HEAD"], capture_output=True, text=True, cwd=os.getcwd())
    output.append(res1.stdout.strip() or res1.stderr.strip())
    
    res2 = subprocess.run(["git", "clean", "-fd"], capture_output=True, text=True, cwd=os.getcwd())
    if res2.stdout.strip():
        output.append(res2.stdout.strip())
        
    return "\n".join(output)
