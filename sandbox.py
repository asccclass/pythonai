import subprocess
import shutil
import uuid
import os
from pathlib import Path

def is_docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        res = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=3)
        return res.returncode == 0
    except Exception:
        return False

class SandboxExecution:
    """
    Provides an isolated execution environment for commands.
    Uses Docker if available. If not, uses an isolated temporary folder fallback.
    """
    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root
        self.has_docker = is_docker_available()
        self.sandbox_id = str(uuid.uuid4())[:8]
        self.sandbox_dir = self.workspace_root / "sandbox" / self.sandbox_id
        
    def setup(self):
        if not self.has_docker:
            # Fallback: create isolated directory and sync workspace files
            self.sandbox_dir.mkdir(parents=True, exist_ok=True)
            # Basic sync of files (excluding heavy directories for speed and memory)
            excluded = {
                ".git", "venv", "__pycache__", "sandbox", "temp",
                "models", ".agents", "tests", ".pytest_cache",
                "node_modules", "papers", "workspace"
            }
            for item in self.workspace_root.iterdir():
                if item.name not in excluded:
                    if item.is_dir():
                        shutil.copytree(
                            item,
                            self.sandbox_dir / item.name,
                            ignore=shutil.ignore_patterns("*.safetensors", "*.bin", "*.pt", "*.onnx")
                        )
                    elif item.stat().st_size < 10 * 1024 * 1024:
                        shutil.copy2(item, self.sandbox_dir / item.name)
            print(f"[Sandbox] Docker not available. Created fallback isolated environment at {self.sandbox_dir}")
        else:
            print("[Sandbox] Docker available. Using containerized execution.")
            
    def run(self, command: list[str], cwd: Path | None = None, **kwargs) -> subprocess.CompletedProcess:
        try:
            if self.has_docker:
                # Resolve relative cwd
                rel_cwd = ""
                if cwd and cwd.is_relative_to(self.workspace_root) and cwd != self.workspace_root:
                    rel_cwd = "/" + cwd.relative_to(self.workspace_root).as_posix()
                
                docker_cmd = [
                    "docker", "run", "--rm",
                    "-v", f"{self.workspace_root.absolute()}:/workspace",
                    "-w", f"/workspace{rel_cwd}",
                    "python:3.12-slim"
                ] + command
                
                # strip env if passed since docker doesn't take dict env this way directly, but let's just pass kwargs safely
                kwargs.pop("env", None)
                return subprocess.run(docker_cmd, capture_output=True, text=True, **kwargs)
            else:
                # Fallback: run inside the isolated sandbox_dir
                target_cwd = self.sandbox_dir
                if cwd and cwd.is_relative_to(self.workspace_root):
                    target_cwd = self.sandbox_dir / cwd.relative_to(self.workspace_root)
                
                print(f"[Sandbox] Running Fallback Command in {target_cwd}")
                # Ensure the command is executed with shell=True for windows if it's a script
                shell = os.name == 'nt'
                
                # allow overriding kwargs
                if 'shell' not in kwargs:
                    kwargs['shell'] = shell
                    
                return subprocess.run(command, cwd=target_cwd, capture_output=True, text=True, **kwargs)
        except Exception as e:
            return subprocess.CompletedProcess(args=command, returncode=1, stdout="", stderr=str(e))
            
    def cleanup(self):
        if not self.has_docker and self.sandbox_dir.exists():
            try:
                # Note: Windows might hold file locks, ignore_errors is safer
                shutil.rmtree(self.sandbox_dir, ignore_errors=True)
                print(f"[Sandbox] Fallback environment cleaned up.")
            except Exception as e:
                print(f"[Sandbox] Failed to cleanup sandbox: {e}")
