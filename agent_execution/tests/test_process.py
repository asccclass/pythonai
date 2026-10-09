import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_execution import execution_scope
from agent_execution.process import run_process, execution_mode
from sandbox import SandboxExecution


class ProcessTests(unittest.TestCase):
    def test_native_success_and_utf8_output(self):
        result = run_process([sys.executable, "-c", "print('你好')"], timeout=5,
                             env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "你好")

    def test_native_timeout(self):
        started = time.monotonic()
        result = run_process([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.3)
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertLess(time.monotonic() - started, 5)

    def test_native_cancel_propagates_from_execution_context(self):
        started = time.monotonic()
        with execution_scope(check_cancelled=lambda: time.monotonic() - started > 0.3):
            result = run_process([sys.executable, "-c", "import time; time.sleep(30)"], timeout=5)
        self.assertEqual(result.returncode, 130, result.stderr)

    def test_output_overflow_terminates_child(self):
        result = run_process([sys.executable, "-c", "print('x' * 100000)"], timeout=5,
                             max_output_bytes=1000)
        self.assertEqual(result.returncode, 125)
        self.assertLessEqual(len(result.stdout), 1000)

    def test_timeout_kills_spawned_descendant(self):
        code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); print(p.pid,flush=True); time.sleep(30)"
        result = run_process([sys.executable, "-c", code], timeout=1)
        self.assertEqual(result.returncode, 124, result.stderr)
        pid = int(result.stdout.strip())
        if os.name == "nt":
            from memory_worker import windows_pid_exists
            self.assertFalse(windows_pid_exists(pid))
        else:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_invalid_timeout_fails_before_launch(self):
        for timeout in (0, -1, float("nan"), float("inf")):
            with patch("subprocess.Popen") as launch, self.assertRaises(ValueError):
                run_process([sys.executable], timeout=timeout)
            launch.assert_not_called()

    def test_modes_are_explicit_and_legacy_flag_is_compatible(self):
        with patch.dict(os.environ, {"AGENT_EXECUTION_MODE": "container"}):
            self.assertEqual(execution_mode(), "container")
        with patch.dict(os.environ, {"AGENT_EXECUTION_MODE": "invalid"}), self.assertRaises(ValueError):
            execution_mode()
        with patch.dict(os.environ, {"AGENT_USE_SANDBOX": "1"}):
            mode = os.environ.pop("AGENT_EXECUTION_MODE", None)
            try:
                self.assertEqual(execution_mode(), "container")
            finally:
                if mode is not None:
                    os.environ["AGENT_EXECUTION_MODE"] = mode


class SandboxPolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        with patch("sandbox.is_docker_available", return_value=True):
            self.sandbox = SandboxExecution(self.root)
        self.sandbox.setup()
        self.addCleanup(self.sandbox.cleanup)

    def test_container_is_readonly_offline_and_resource_bounded(self):
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, "ok", "")
        with patch("sandbox.run_process", side_effect=run), \
             patch.object(self.sandbox, "_collect_artifacts"), \
             patch.object(self.sandbox, "_remove_container") as remove:
            result = self.sandbox.run(["python", "-c", "print(1)"], env={"API_KEY": "private"})
        self.assertEqual(result.returncode, 0)
        startup = calls[0][0]
        self.assertIn("--read-only", startup)
        self.assertEqual(startup[startup.index("--network") + 1], "none")
        self.assertIn("--memory", startup)
        self.assertIn("--memory-swap", startup)
        self.assertIn("--cpus", startup)
        self.assertIn("--pids-limit", startup)
        self.assertIn("/artifacts:rw,noexec,nosuid,size=32m,mode=1777", startup)
        self.assertNotIn("env", calls[0][1])
        mounts = [argument for argument in startup if argument.startswith("type=bind")]
        self.assertEqual(len(mounts), 1)
        self.assertIn(str(self.sandbox.snapshot_dir), mounts[0])
        self.assertTrue(mounts[0].endswith("readonly"))
        remove.assert_called_once()

    def test_container_cwd_cannot_use_trusted_external_assets(self):
        with patch("sandbox.run_process") as run, patch.object(self.sandbox, "_remove_container"):
            result = self.sandbox.run(["python"], cwd=self.root.parent)
        self.assertNotEqual(result.returncode, 0)
        run.assert_not_called()

    def test_artifact_tar_rejects_traversal_and_symlinks(self):
        for name, kind in (("../outside", tarfile.REGTYPE), ("link", tarfile.SYMTYPE)):
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w") as tar:
                member = tarfile.TarInfo(name)
                member.type = kind
                tar.addfile(member)
            response = subprocess.CompletedProcess([], 0, archive.getvalue(), b"")
            with patch("sandbox.run_process", return_value=response), self.assertRaises(PermissionError):
                self.sandbox._collect_artifacts()

    def test_artifact_export_does_not_overwrite_host_files(self):
        (self.sandbox.artifacts_dir / "report.txt").write_text("new", encoding="utf-8")
        destination = self.root / "results"
        destination.mkdir()
        (destination / "report.txt").write_text("user", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self.sandbox.export_artifacts(destination)
        self.assertEqual((destination / "report.txt").read_text(encoding="utf-8"), "user")

    def test_artifact_export_stays_in_workspace(self):
        with self.assertRaises(PermissionError):
            self.sandbox.export_artifacts(self.root.parent / "outside")
