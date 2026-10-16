from __future__ import annotations

import os
import io
import tarfile
import shutil
import subprocess
import uuid
from pathlib import Path

from agent_execution.process import run_process


def is_docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                              capture_output=True, text=True, timeout=10).returncode == 0
    except Exception:
        return False


class SandboxExecution:
    """Docker-only sandbox; never silently execute on the host."""
    IMAGE = 'python:3.12-slim'

    EXCLUDED = {".git", "venv", ".venv", "__pycache__", "sandbox", "temp", "models",
                ".agents", ".pytest_cache", "node_modules", "memory", "communication",
                ".ssh", ".aws", ".azure", ".kube", "secrets", "credentials"}
    MAX_FILE_BYTES = 10 * 1024 * 1024
    MAX_SNAPSHOT_BYTES = 100 * 1024 * 1024

    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root.resolve()
        self.has_docker = is_docker_available()
        self.sandbox_id = uuid.uuid4().hex
        self.container_name = f"pythonai-{self.sandbox_id}"
        self.sandbox_dir = self.workspace_root / "sandbox" / self.sandbox_id
        self.snapshot_dir = self.sandbox_dir / "workspace"
        self.artifacts_dir = self.sandbox_dir / "artifacts"
        self.ready = False

    @staticmethod
    def is_secret(path: Path) -> bool:
        name = path.name.lower()
        return (name == "envfile" or name.startswith(".env") or name.endswith((".pem", ".key", ".p12", ".pfx"))
                or "secret" in name or "credential" in name or "token" in name)

    def setup(self):
        if not self.has_docker:
            raise RuntimeError("Docker is unavailable; sandbox execution refused. Use explicit host mode only when authorized.")
        # Reject redirected storage before copying any source files.
        sandbox_parent = (self.workspace_root / "sandbox").resolve()
        if not sandbox_parent.is_relative_to(self.workspace_root):
            raise PermissionError("Sandbox storage is outside workspace")
        self.snapshot_dir.mkdir(parents=True)
        self.artifacts_dir.mkdir()
        total_bytes = 0
        try:
            for directory, names, files in os.walk(self.workspace_root, followlinks=False):
                parent = Path(directory)
                names[:] = [name for name in names if name not in self.EXCLUDED
                            and not self.is_secret(parent / name)
                            and not (parent / name).is_symlink()
                            and (parent / name).resolve().is_relative_to(self.workspace_root)]
                for name in files:
                    source = parent / name
                    if source.is_symlink() or not source.resolve().is_relative_to(self.workspace_root) or self.is_secret(source):
                        continue
                    size = source.stat().st_size
                    if size > self.MAX_FILE_BYTES:
                        continue
                    total_bytes += size
                    if total_bytes > self.MAX_SNAPSHOT_BYTES:
                        raise ValueError("Sandbox snapshot exceeds 100 MiB limit")
                    target = self.snapshot_dir / source.relative_to(self.workspace_root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
            self.ready = True
            print(f"[Sandbox] Docker isolated snapshot ready: {self.sandbox_id}")
        except BaseException:
            self.cleanup()
            raise

    def _container_argument(self, value: str) -> str:
        if value.startswith("-") and "=" in value:
            flag, path = value.split("=", 1)
            return flag + "=" + self._container_argument(path)
        path = Path(value)
        if path.is_absolute():
            resolved = path.resolve()
            if not resolved.is_relative_to(self.workspace_root):
                raise PermissionError("Command path is outside sandbox workspace")
            relative = resolved.relative_to(self.workspace_root)
            if any(part in self.EXCLUDED for part in relative.parts) or self.is_secret(path):
                raise PermissionError("Command references an excluded sandbox asset")
            return "/workspace/" + relative.as_posix()
        return value

    def run(self, command: list[str], cwd: Path | None = None, **kwargs) -> subprocess.CompletedProcess:
        if not self.has_docker or not self.ready:
            return subprocess.CompletedProcess(command, 1, "", "Sandbox was not initialized; execution refused")
        try:
            target_cwd = (cwd or self.workspace_root).resolve()
            if not target_cwd.is_relative_to(self.workspace_root):
                raise PermissionError("Sandbox cwd must be inside workspace")
            relative = target_cwd.relative_to(self.workspace_root)
            if any(part in self.EXCLUDED for part in relative.parts):
                raise PermissionError("Sandbox cwd references excluded storage")
            docker_cmd = [
                "docker", "run", "--detach", "--name", self.container_name,
                "--network", "none", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--pids-limit", "64",
                "--memory", "256m", "--memory-swap", "256m", "--cpus", "1", "--user", "65534:65534",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
                "--mount", f"type=bind,source={self.snapshot_dir},target=/workspace,readonly",
                "--tmpfs", "/artifacts:rw,noexec,nosuid,size=32m,mode=1777",
                "--env", "PYTHONDONTWRITEBYTECODE=1",
                self.IMAGE, "sleep", "infinity",
            ]
            # Host/provider credentials are never forwarded into the container.
            kwargs.pop("env", None)
            started = run_process(docker_cmd, **kwargs)
            if started.returncode != 0:
                return started
            execute = ["docker", "exec", "-w", "/workspace/" + relative.as_posix(), self.container_name]
            result = run_process(execute + [self._container_argument(item) for item in command], **kwargs)
            if result.returncode == 0:
                self._collect_artifacts()
            return result
        except Exception as error:
            return subprocess.CompletedProcess(command, 1, "", str(error))
        finally:
            self._remove_container()

    def _remove_container(self):
        if self.has_docker:
            # Cleanup must run even after the task's cancellation token was set.
            result = run_process(["docker", "rm", "-f", self.container_name], capture_output=True,
                                 text=True, timeout=20, check_cancelled=lambda:False)
            if result.returncode != 0 and "No such container" not in result.stderr:
                raise RuntimeError(f"Sandbox cleanup failed for {self.container_name}: {result.stderr}")

    def _collect_artifacts(self):
        # All container output storage is a quota-limited tmpfs, never a writable host mount.
        archive = run_process(["docker", "exec", self.container_name, "tar", "-C", "/artifacts", "-cf", "-", "."],
                              text=False, timeout=5, max_output_bytes=40 * 1024 * 1024)
        if archive.returncode:
            raise RuntimeError("Cannot collect sandbox artifacts")
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
            members = tar.getmembers()
            if len(members) > 4096:
                raise ValueError("Too many sandbox artifacts")
            for member in members:
                target = (self.artifacts_dir / member.name).resolve()
                if not target.is_relative_to(self.artifacts_dir.resolve()):
                    raise PermissionError("Artifact archive path traversal")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile() and member.size <= self.MAX_FILE_BYTES and not self.is_secret(target):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(member) as source:
                        target.write_bytes(source.read())
                else:
                    raise PermissionError("Artifact must be a regular non-sensitive file within size limit")

    def export_artifacts(self, destination: Path):
        """Export only explicitly requested artifacts, without overwriting host files."""
        target = destination.resolve()
        if not target.is_relative_to(self.workspace_root) or target.is_relative_to(self.sandbox_dir):
            raise PermissionError("Artifact destination must be inside workspace and outside sandbox storage")
        target.mkdir(parents=True, exist_ok=True)
        plan = []
        for source in self.artifacts_dir.rglob("*"):
            if source.is_symlink() or not source.resolve().is_relative_to(self.artifacts_dir.resolve()):
                raise PermissionError("Artifact symlink is not allowed")
            if source.is_file():
                if self.is_secret(source) or source.stat().st_size > self.MAX_FILE_BYTES:
                    raise PermissionError("Artifact is sensitive or exceeds size limit")
                output = (target / source.relative_to(self.artifacts_dir)).resolve()
                if not output.is_relative_to(target):
                    raise PermissionError("Artifact destination symlink escapes export directory")
                if output.exists():
                    raise FileExistsError(f"Artifact already exists: {output}")
                plan.append((source, output))
        for source, output in plan:
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, output)

    def cleanup(self):
        self.ready = False
        # This directory is created by this execution, and resolved before recursive removal.
        target = self.sandbox_dir.resolve()
        expected_parent = (self.workspace_root / "sandbox").resolve()
        if not expected_parent.is_relative_to(self.workspace_root) or target.parent != expected_parent:
            raise PermissionError("Unsafe sandbox cleanup path")
        if target.exists():
            shutil.rmtree(target)
