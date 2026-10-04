import subprocess
import os

def git_diff(staged: bool = False, file_path: str = "") -> str:
    """Returns the git diff."""
    try:
        subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], check=True, capture_output=True, cwd=os.getcwd())
    except subprocess.CalledProcessError:
        return "Error: Current directory is not a git repository."

    cmd = ["git", "diff"]
    if staged:
        cmd.append("--cached")
    if file_path:
        cmd.extend(["--", file_path])
        
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", cwd=os.getcwd())
        if result.stdout is None or not result.stdout.strip():
            return "No differences found."
            
        # Truncate if diff is insanely large
        diff_text = result.stdout
        if len(diff_text) > 10000:
            diff_text = diff_text[:10000] + "\n\n... [DIFF TRUNCATED - TOO LARGE] ..."
            
        return diff_text
    except Exception as e:
        return f"Error executing git diff: {e}"
