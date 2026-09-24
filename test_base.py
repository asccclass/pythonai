import subprocess
import tempfile
import unittest
from pathlib import Path
import os
from unittest.mock import patch

import base
from base import delete_file, list_files, load_dotenv, read_file, run_command, write_file


class BaseTests(unittest.TestCase):
    def setUp(self):
        base._approved_commands.clear()
        base._command_guard = None

    def test_read_file_returns_text(self):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as temp_file:
            temp_file.write("hello")
            path = Path(temp_file.name)

        try:
            self.assertEqual(read_file(path), "hello")
        finally:
            path.unlink()

    def test_list_files_returns_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir)
            (path / "sample.txt").write_text("hello", encoding="utf-8")

            self.assertIn("sample.txt", list_files(path))

    def test_write_file_writes_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sample.txt"

            write_file(path, "hello")

            self.assertEqual(path.read_text(encoding="utf-8"), "hello")

    def test_delete_file_uses_windows_command_on_windows(self):
        completed = object()

        with (
            patch("base.platform.system", return_value="Windows"),
            patch("base.subprocess.run", return_value=completed) as run,
        ):
            result = delete_file("sample.txt")

        self.assertIs(result, completed)
        run.assert_called_once_with(
            ["cmd", "/c", "del", "/f", "/q", "sample.txt"],
            capture_output=True,
            check=False,
            text=True,
        )

    def test_delete_file_uses_rm_command_on_non_windows(self):
        completed = object()

        with (
            patch("base.platform.system", return_value="Linux"),
            patch("base.subprocess.run", return_value=completed) as run,
        ):
            result = delete_file("sample.txt")

        self.assertIs(result, completed)
        run.assert_called_once_with(
            ["rm", "-f", "sample.txt"],
            capture_output=True,
            check=False,
            text=True,
        )

    def test_delete_file_refuses_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = delete_file(temp_dir)

        self.assertEqual(result.returncode, 1)
        self.assertIn("Refusing to delete directory", result.stderr)

    def test_run_command_returns_completed_process(self):
        class Guard:
            def assess_command(self, command, cwd=None):
                return base.GuardDecision(needs_confirmation=True)

        with (
            patch("base.get_command_guard", return_value=Guard()),
            patch("builtins.input", return_value="y"),
        ):
            result = run_command(["python", "-c", "print('hello')"])

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "hello")

    def test_run_command_skips_prompt_when_laya_allows(self):
        class Guard:
            def assess_command(self, command, cwd=None):
                return base.GuardDecision(needs_confirmation=False)

        completed = subprocess.CompletedProcess(args=["echo", "hello"], returncode=0, stdout="hello", stderr="")

        with (
            patch("base.get_command_guard", return_value=Guard()),
            patch("builtins.input") as user_input,
            patch("base.subprocess.run", return_value=completed) as run,
        ):
            result = run_command(["echo", "hello"])

        self.assertIs(result, completed)
        user_input.assert_not_called()
        run.assert_called_once_with(
            ["echo", "hello"],
            cwd=None,
            capture_output=True,
            check=False,
            text=True,
        )

    def test_run_command_reuses_previous_confirmation(self):
        class Guard:
            def assess_command(self, command, cwd=None):
                return base.GuardDecision(needs_confirmation=True)

        completed = subprocess.CompletedProcess(args=["echo", "hello"], returncode=0, stdout="hello", stderr="")

        with (
            patch("base.get_command_guard", return_value=Guard()),
            patch("builtins.input", return_value="y") as user_input,
            patch("base.subprocess.run", return_value=completed) as run,
        ):
            first_result = run_command(["echo", "hello"])
            second_result = run_command(["echo", "hello"])

        self.assertIs(first_result, completed)
        self.assertIs(second_result, completed)
        user_input.assert_called_once_with(" Run '['echo', 'hello']'? [y/N]: ")
        self.assertEqual(run.call_count, 2)

    def test_run_command_prompts_when_laya_is_unavailable(self):
        class Guard:
            def assess_command(self, command, cwd=None):
                return base.GuardDecision(available=False, reason="missing")

        with (
            patch("base.get_command_guard", return_value=Guard()),
            patch("builtins.input", return_value="n"),
            patch("base.subprocess.run") as run,
        ):
            result = run_command(["echo", "hello"])

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "User cancelled")
        run.assert_not_called()

    def test_load_dotenv_sets_missing_values(self):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as env_file:
            env_file.write("TEST_DOTENV_VALUE=from-file\n")
            env_path = Path(env_file.name)

        try:
            os.environ.pop("TEST_DOTENV_VALUE", None)
            load_dotenv(env_path)
            self.assertEqual(os.environ["TEST_DOTENV_VALUE"], "from-file")
        finally:
            os.environ.pop("TEST_DOTENV_VALUE", None)
            env_path.unlink()


if __name__ == "__main__":
    unittest.main()
