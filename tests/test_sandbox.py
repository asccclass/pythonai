import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import base
from sandbox import SandboxExecution, is_docker_available


class SandboxExecutionTests(unittest.TestCase):
    def test_cleanup_uses_bounded_tree_runner_even_after_task_cancellation(self):
        from agent_execution import execution_scope
        import subprocess
        with tempfile.TemporaryDirectory() as directory, patch('sandbox.is_docker_available',return_value=True):
            sandbox=SandboxExecution(Path(directory))
            with execution_scope(check_cancelled=lambda:True), patch('sandbox.run_process',return_value=subprocess.CompletedProcess([],0,'','')) as run:
                sandbox._remove_container()
            self.assertEqual(run.call_args.args[0],['docker','rm','-f',sandbox.container_name])
            self.assertEqual(run.call_args.kwargs['timeout'],20)
            self.assertFalse(run.call_args.kwargs['check_cancelled']())

    def test_is_docker_available_returns_false_when_command_fails(self):
        with patch("shutil.which", return_value="/usr/bin/docker"):
            with patch("subprocess.run", side_effect=Exception("daemon down")):
                self.assertFalse(is_docker_available())

    def test_missing_docker_refuses_setup_and_execution(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch("sandbox.is_docker_available", return_value=False):
            sandbox = SandboxExecution(Path(temp_dir))
            with self.assertRaisesRegex(RuntimeError, "execution refused"):
                sandbox.setup()
            with patch("sandbox.run_process") as run:
                result = sandbox.run(["python", "-c", "print('unsafe')"])
            self.assertNotEqual(result.returncode, 0)
            run.assert_not_called()
            sandbox.cleanup()

    def test_snapshot_excludes_secrets_and_heavy_files(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch("sandbox.is_docker_available", return_value=True):
            root = Path(temp_dir)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("print('hello')", encoding="utf-8")
            (root / "src" / ".env").write_text("SECRET=private", encoding="utf-8")
            (root / "credential.json").write_text("private", encoding="utf-8")
            (root / "large.bin").write_bytes(b"x" * (11 * 1024 * 1024))
            sandbox = SandboxExecution(root)
            sandbox.setup()
            try:
                self.assertTrue((sandbox.snapshot_dir / "src" / "app.py").exists())
                self.assertFalse((sandbox.snapshot_dir / "src" / ".env").exists())
                self.assertFalse((sandbox.snapshot_dir / "credential.json").exists())
                self.assertFalse((sandbox.snapshot_dir / "large.bin").exists())
            finally:
                sandbox.cleanup()
            self.assertFalse(sandbox.sandbox_dir.exists())

    def test_base_run_command_uses_sandbox_when_env_enabled(self):
        mock_sandbox = MagicMock()
        mock_sandbox.run.return_value = MagicMock(returncode=0, stdout="sandboxed", stderr="")

        with (
            patch.dict("os.environ", {"AGENT_USE_SANDBOX": "1"}),
            patch("base.get_command_guard") as mock_guard,
            patch("sandbox.SandboxExecution", return_value=mock_sandbox),
        ):
            decision = base.GuardDecision(needs_confirmation=False)
            mock_guard.return_value.assess_command.return_value = decision
            res = base.run_command(["echo", "test"])

        self.assertEqual(res.stdout, "sandboxed")
        mock_sandbox.setup.assert_called_once()
        mock_sandbox.run.assert_called_once()
        mock_sandbox.cleanup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
