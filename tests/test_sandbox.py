import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import base
from sandbox import SandboxExecution, is_docker_available


class SandboxExecutionTests(unittest.TestCase):
    def test_is_docker_available_returns_false_when_command_fails(self):
        with patch("shutil.which", return_value="/usr/bin/docker"):
            with patch("subprocess.run", side_effect=Exception("daemon down")):
                self.assertFalse(is_docker_available())

    def test_fallback_setup_excludes_heavy_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "models").mkdir()
            (root / "models" / "weights.safetensors").write_bytes(b"large_weight_data")
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("print('hello')", encoding="utf-8")
            (root / "large.bin").write_bytes(b"x" * (11 * 1024 * 1024))

            sandbox = SandboxExecution(root)
            sandbox.has_docker = False
            sandbox.setup()

            try:
                self.assertTrue((sandbox.sandbox_dir / "src" / "app.py").exists())
                self.assertFalse((sandbox.sandbox_dir / "models").exists())
                self.assertFalse((sandbox.sandbox_dir / "large.bin").exists())
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
