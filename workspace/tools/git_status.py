import subprocess
import os

def git_status() -> str:
    """Returns the current git status."""
    try:
        # Check if it's a git repository
        subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], check=True, capture_output=True, cwd=os.getcwd())
    except subprocess.CalledProcessError:
        return "Error: Current directory is not a git repository."

    try:
        result = subprocess.run(["git", "status"], capture_output=True, text=True, encoding="utf-8", cwd=os.getcwd())
        return result.stdout
    except Exception as e:
        return f"Error executing git status: {e}"
