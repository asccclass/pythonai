import os
import sys
import subprocess
from pathlib import Path

# Module-level dictionary to track consecutive test failures for a given target
_failure_counts = {}
MAX_RETRIES = 5

def run_test_suite(test_target: str, framework: str = "pytest") -> str:
    global _failure_counts
    
    workspace_dir = Path(os.getcwd())
    target_path = workspace_dir / test_target
    
    if not target_path.exists():
        return f"Error: Test target '{test_target}' does not exist."
        
    cmd = []
    if framework.lower() == "pytest":
        cmd = [sys.executable, "-m", "pytest", str(target_path), "--no-header", "-v"]
    elif framework.lower() == "python":
        cmd = [sys.executable, str(target_path)]
    else:
        return f"Error: Unsupported test framework '{framework}'. Use 'pytest' or 'python'."
        
    try:
        # Run safely with timeout
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        output = f"Exit Code: {result.returncode}\n\n"
        
        if result.returncode == 0:
            # Reset failure count on success
            _failure_counts[test_target] = 0
            output += "✅ All Tests Passed!\n"
            output += result.stdout
            return output
            
        # Handle failure
        _failure_counts[test_target] = _failure_counts.get(test_target, 0) + 1
        
        if _failure_counts[test_target] >= MAX_RETRIES:
            # Reset so human can try again, but interrupt Agent
            _failure_counts[test_target] = 0
            return (f"⛔ HITL INTERRUPT: Max retries ({MAX_RETRIES}) reached for {test_target}.\n"
                    f"The tests are still failing after {MAX_RETRIES} attempts. "
                    f"You MUST stop trying to fix it yourself, explain the error to the user, and ask for human assistance.\n"
                    f"Final Output:\n{result.stdout[-1500:]}\n{result.stderr[-1500:]}")
            
        output += f"❌ Tests Failed! (Attempt {_failure_counts[test_target]}/{MAX_RETRIES})\n"
        output += "Please analyze the traceback below and use ast_edit or edit_file to fix the bug, then run this test again.\n\n"
        
        if result.stdout:
            output += "=== STDOUT (Last 2000 chars) ===\n" + result.stdout[-2000:] + "\n"
        if result.stderr:
            output += "=== STDERR (Last 2000 chars) ===\n" + result.stderr[-2000:] + "\n"
            
        return output
        
    except subprocess.TimeoutExpired:
        _failure_counts[test_target] = _failure_counts.get(test_target, 0) + 1
        return f"Error: Test execution timed out after 30 seconds. Infinite loop detected? (Attempt {_failure_counts[test_target]}/{MAX_RETRIES})"
    except Exception as e:
        return f"Error executing tests: {e}"
