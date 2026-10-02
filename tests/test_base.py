import subprocess
import tempfile
import unittest
from pathlib import Path
import os
from types import SimpleNamespace
from unittest.mock import patch

import base
from base import (
    curl_command,
    delete_file,
    fetch_url,
    list_files,
    load_dotenv,
    read_file,
    auto_approve_command_runs,
    run_command,
    run_skill,
    run_tool,
    run_tool_with_context,
    subprocess_text_options,
    resolve_workspace_path,
    write_file,
)


class BaseTests(unittest.TestCase):
    def setUp(self):
        base._approved_commands.clear()
        base._command_guard = None
        self._old_workspace_root = os.environ.pop("AGENT_WORKSPACE_ROOT", None)

    def tearDown(self):
        if self._old_workspace_root is not None:
            os.environ["AGENT_WORKSPACE_ROOT"] = self._old_workspace_root
        else:
            os.environ.pop("AGENT_WORKSPACE_ROOT", None)

    def test_read_file_returns_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "sample.txt").write_text("hello", encoding="utf-8")
            with patch.dict("os.environ", {"AGENT_WORKSPACE_ROOT": str(root)}):
                self.assertEqual(read_file("sample.txt"), "hello")

    def test_list_files_returns_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir)
            (path / "sample.txt").write_text("hello", encoding="utf-8")

            with patch.dict("os.environ", {"AGENT_WORKSPACE_ROOT": str(path)}):
                self.assertIn("sample.txt", list_files("."))

    def test_write_file_writes_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "sample.txt"

            with patch.dict("os.environ", {"AGENT_WORKSPACE_ROOT": str(root)}):
                write_file("sample.txt", "hello")

            self.assertEqual(path.read_text(encoding="utf-8"), "hello")

    def test_write_file_creates_parent_directories_inside_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict("os.environ", {"AGENT_WORKSPACE_ROOT": str(root)}):
                write_file("notes/sample.txt", "hello")

            self.assertEqual((root / "notes" / "sample.txt").read_text(encoding="utf-8"), "hello")

    def test_delete_file_uses_windows_command_on_windows(self):
        completed = object()

        with (
            patch("base.platform.system", return_value="Windows"),
            patch("base.subprocess.run", return_value=completed) as run,
        ):
            with patch.dict("os.environ", {"AGENT_WORKSPACE_ROOT": str(Path.cwd())}):
                result = delete_file("sample.txt")

        self.assertIs(result, completed)
        run.assert_called_once_with(
            ["cmd", "/c", "del", "/f", "/q", str((Path.cwd() / "sample.txt").resolve())],
            **subprocess_text_options(),
        )

    def test_delete_file_uses_rm_command_on_non_windows(self):
        completed = object()

        with (
            patch("base.platform.system", return_value="Linux"),
            patch("base.subprocess.run", return_value=completed) as run,
        ):
            with patch.dict("os.environ", {"AGENT_WORKSPACE_ROOT": str(Path.cwd())}):
                result = delete_file("sample.txt")

        self.assertIs(result, completed)
        run.assert_called_once_with(
            ["rm", "-f", str((Path.cwd() / "sample.txt").resolve())],
            **subprocess_text_options(),
        )

    def test_delete_file_refuses_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "dir").mkdir()
            with patch.dict("os.environ", {"AGENT_WORKSPACE_ROOT": str(root)}):
                result = delete_file("dir")

        self.assertEqual(result.returncode, 1)
        self.assertIn("Refusing to delete directory", result.stderr)

    def test_curl_command_uses_curl_exe_on_windows(self):
        with patch("base.platform.system", return_value="Windows"):
            command = curl_command("https://example.test", max_time=7)

        self.assertEqual(command[0], "curl.exe")
        self.assertIn("--location", command)
        self.assertIn("7", command)
        self.assertEqual(command[-1], "https://example.test")

    def test_curl_command_uses_curl_on_non_windows(self):
        with patch("base.platform.system", return_value="Linux"):
            command = curl_command("https://example.test", follow_redirects=False)

        self.assertEqual(command[0], "curl")
        self.assertNotIn("--location", command)

    def test_fetch_url_runs_curl_command(self):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="body", stderr="")

        with (
            patch("base.platform.system", return_value="Linux"),
            patch("base.subprocess.run", return_value=completed) as run,
        ):
            result = fetch_url("https://example.test", max_time=3)

        self.assertIs(result, completed)
        run.assert_called_once_with(
            ["curl", "--silent", "--show-error", "--location", "--max-time", "3", "https://example.test"],
            **subprocess_text_options(),
        )

    def test_subprocess_text_options_decode_utf8_with_replacement(self):
        options = subprocess_text_options()

        self.assertEqual(options["encoding"], "utf-8")
        self.assertEqual(options["errors"], "replace")

    def test_run_command_returns_completed_process(self):
        class Guard:
            def assess_command(self, command, cwd=None):
                return base.GuardDecision(needs_confirmation=True)

        with (
            patch("base.get_command_guard", return_value=Guard()),
            patch("builtins.input", return_value="y"),
            patch.dict("os.environ", {"AGENT_WORKSPACE_ROOT": str(Path.cwd())}),
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
            patch.dict("os.environ", {"AGENT_WORKSPACE_ROOT": str(Path.cwd())}),
        ):
            result = run_command(["echo", "hello"])

        self.assertIs(result, completed)
        user_input.assert_not_called()
        run.assert_called_once_with(
            ["echo", "hello"],
            cwd=Path.cwd().resolve(),
            **subprocess_text_options(),
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
            patch.dict("os.environ", {"AGENT_WORKSPACE_ROOT": str(Path.cwd())}),
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
            patch.dict("os.environ", {"AGENT_WORKSPACE_ROOT": str(Path.cwd())}),
        ):
            result = run_command(["echo", "hello"])

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "User cancelled")
        run.assert_not_called()

    def test_run_command_auto_approval_context_skips_prompt_and_guard(self):
        completed = subprocess.CompletedProcess(args=["echo", "hello"], returncode=0, stdout="hello", stderr="")

        with (
            patch("base.get_command_guard") as guard,
            patch("builtins.input") as user_input,
            patch("base.subprocess.run", return_value=completed) as run,
            auto_approve_command_runs(),
            patch.dict("os.environ", {"AGENT_WORKSPACE_ROOT": str(Path.cwd())}),
        ):
            result = run_command(["echo", "hello"])

        self.assertIs(result, completed)
        guard.assert_not_called()
        user_input.assert_not_called()
        run.assert_called_once_with(
            ["echo", "hello"],
            cwd=Path.cwd().resolve(),
            **subprocess_text_options(),
        )

    def test_run_command_env_file_overrides_inherited_environment(self):
        completed = subprocess.CompletedProcess(args=["echo", "hello"], returncode=0, stdout="hello", stderr="")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_path = root / "envfile"
            env_path.write_text("OLLAMA_MODEL=from-envfile\nEMPTY_ALLOWED=\n", encoding="utf-8")
            with (
                patch.dict("os.environ", {"OLLAMA_MODEL": "from-parent"}, clear=False),
                patch.dict("os.environ", {"AGENT_WORKSPACE_ROOT": str(root)}, clear=False),
                patch("base.subprocess.run", return_value=completed) as run,
                auto_approve_command_runs(),
            ):
                result = run_command(["echo", "hello"], env_file="envfile")

        self.assertIs(result, completed)
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["OLLAMA_MODEL"], "from-envfile")
        self.assertEqual(env["EMPTY_ALLOWED"], "")

    def test_run_command_resolves_relative_executable_against_cwd(self):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as temp_dir:
            cwd = Path(temp_dir)
            with (
                patch.dict("os.environ", {"AGENT_WORKSPACE_ROOT": str(cwd)}),
                patch("base.subprocess.run", return_value=completed) as run,
                auto_approve_command_runs(),
            ):
                result = run_command([".\\tool.exe", "--version"])

        self.assertIs(result, completed)
        command = run.call_args.args[0]
        self.assertEqual(command[0], str((cwd / "tool.exe").resolve()))
        self.assertEqual(command[1], "--version")

    def test_workspace_root_allows_paths_inside_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "note.txt"
            target.write_text("hello", encoding="utf-8")
            with patch.dict("os.environ", {"AGENT_WORKSPACE_ROOT": str(root)}):
                self.assertEqual(read_file("note.txt"), "hello")
                self.assertEqual(resolve_workspace_path("note.txt"), target.resolve())

    def test_workspace_root_rejects_paths_outside_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            outside = Path(temp_dir) / "outside.txt"
            root.mkdir()
            outside.write_text("secret", encoding="utf-8")
            with patch.dict("os.environ", {"AGENT_WORKSPACE_ROOT": str(root)}):
                self.assertEqual(read_file(outside), "")
                result = run_command(["echo", "hello"], cwd=outside.parent)

        self.assertEqual(result.returncode, 1)
        self.assertIn("outside Agent workspace", result.stderr)

    def test_workspace_root_rejects_command_path_arguments_outside_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            outside = Path(temp_dir) / "outside.txt"
            root.mkdir()
            outside.write_text("secret", encoding="utf-8")
            with (
                patch.dict("os.environ", {"AGENT_WORKSPACE_ROOT": str(root)}),
                patch("base.subprocess.run") as run,
            ):
                result = run_command(["type", str(outside)])

        self.assertEqual(result.returncode, 1)
        self.assertIn("outside Agent workspace", result.stderr)
        run.assert_not_called()

    def test_default_workspace_root_is_project_workspace(self):
        root = base.workspace_root()

        self.assertEqual(root, (base.PROJECT_ROOT / "workspace").resolve())
        self.assertTrue(root.exists())

    def test_run_command_defaults_to_workspace_cwd(self):
        completed = subprocess.CompletedProcess(args=["echo"], returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.dict("os.environ", {"AGENT_WORKSPACE_ROOT": str(root)}),
                patch("base.subprocess.run", return_value=completed) as run,
                auto_approve_command_runs(),
            ):
                result = run_command(["echo"])

        self.assertIs(result, completed)
        self.assertEqual(run.call_args.kwargs["cwd"], root.resolve())

    def test_run_command_allows_workspace_subdirectory_cwd(self):
        completed = subprocess.CompletedProcess(args=["echo"], returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "subdir").mkdir()
            with (
                patch.dict("os.environ", {"AGENT_WORKSPACE_ROOT": str(root)}),
                patch("base.subprocess.run", return_value=completed) as run,
                auto_approve_command_runs(),
            ):
                result = run_command(["echo"], cwd="subdir")

        self.assertIs(result, completed)
        self.assertEqual(run.call_args.kwargs["cwd"], (root / "subdir").resolve())

    def test_run_command_rejects_external_env_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            outside = Path(temp_dir) / "envfile"
            root.mkdir()
            outside.write_text("SECRET=value\n", encoding="utf-8")
            with (
                patch.dict("os.environ", {"AGENT_WORKSPACE_ROOT": str(root)}),
                patch("base.subprocess.run") as run,
                auto_approve_command_runs(),
            ):
                result = run_command(["echo"], env_file=outside)

        self.assertEqual(result.returncode, 1)
        self.assertIn("outside Agent workspace", result.stderr)
        run.assert_not_called()

    def test_run_command_does_not_treat_urls_as_paths(self):
        completed = subprocess.CompletedProcess(args=["echo"], returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.dict("os.environ", {"AGENT_WORKSPACE_ROOT": str(root)}),
                patch("base.subprocess.run", return_value=completed) as run,
                auto_approve_command_runs(),
            ):
                result = run_command(["echo", "https://example.test/a/b"])

        self.assertIs(result, completed)
        run.assert_called_once()

    def test_run_command_allows_trusted_skill_assets(self):
        completed = subprocess.CompletedProcess(args=["tool"], returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            skill_root = Path(temp_dir) / "skills" / "trusted"
            scripts = skill_root / "scripts"
            root.mkdir()
            scripts.mkdir(parents=True)
            (scripts / "envfile").write_text("KEY=value\n", encoding="utf-8")
            with (
                patch.dict("os.environ", {"AGENT_WORKSPACE_ROOT": str(root)}),
                patch("base.subprocess.run", return_value=completed) as run,
                auto_approve_command_runs(),
            ):
                result = run_command(
                    [".\\tool.exe"],
                    cwd=scripts,
                    env_file=scripts / "envfile",
                    trusted_asset_roots=[skill_root],
                )

        self.assertIs(result, completed)
        self.assertEqual(run.call_args.kwargs["cwd"], scripts.resolve())
        self.assertEqual(run.call_args.args[0][0], str((scripts / "tool.exe").resolve()))

    def test_run_command_prefers_project_relative_trusted_skill_assets(self):
        completed = subprocess.CompletedProcess(args=["tool"], returncode=0, stdout="", stderr="")
        skill_root = Path("skills") / "mybrain_query_cli"
        scripts = skill_root / "scripts"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.dict("os.environ", {"AGENT_WORKSPACE_ROOT": str(root)}),
                patch("base.subprocess.run", return_value=completed) as run,
                auto_approve_command_runs(),
            ):
                result = run_command(
                    [".\\mybrain.exe", "--query", "hello"],
                    cwd=scripts,
                    env_file=scripts / "envfile",
                    trusted_asset_roots=[skill_root],
                )

        self.assertIs(result, completed)
        self.assertEqual(run.call_args.kwargs["cwd"], scripts.resolve())
        self.assertEqual(run.call_args.args[0][0], str((scripts / "mybrain.exe").resolve()))

    def test_run_command_trusted_assets_do_not_allow_other_project_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspace"
            skill_root = Path(temp_dir) / "skills" / "trusted"
            other_skill = Path(temp_dir) / "skills" / "other"
            root.mkdir()
            skill_root.mkdir(parents=True)
            other_skill.mkdir(parents=True)
            with (
                patch.dict("os.environ", {"AGENT_WORKSPACE_ROOT": str(root)}),
                patch("base.subprocess.run") as run,
                auto_approve_command_runs(),
            ):
                result = run_command(["echo"], cwd=other_skill, trusted_asset_roots=[skill_root])

        self.assertEqual(result.returncode, 1)
        self.assertIn("outside Agent workspace", result.stderr)
        run.assert_not_called()

    def test_run_tool_strips_internal_trusted_asset_roots_from_model_args(self):
        calls = []
        tool_call = SimpleNamespace(
            function=SimpleNamespace(
                name="run_command",
                arguments='{"command": ["echo"], "trusted_asset_roots": ["skills"]}',
            )
        )

        def run_command(**kwargs):
            calls.append(kwargs)
            return "ok"

        with patch.dict(base.TOOLS, {"run_command": run_command}):
            result = run_tool(tool_call)

        self.assertEqual(result, "ok")
        self.assertEqual(calls, [{"command": ["echo"]}])

    def test_run_tool_with_context_strips_internal_trusted_asset_roots_from_model_args(self):
        calls = []
        tool_call = SimpleNamespace(
            function=SimpleNamespace(
                name="run_command",
                arguments='{"command": ["echo"], "trusted_asset_roots": ["skills"]}',
            )
        )

        def run_command(**kwargs):
            calls.append(kwargs)
            return "ok"

        with patch.dict(base.TOOLS, {"run_command": run_command}):
            result = run_tool_with_context(tool_call)

        self.assertEqual(result, "ok")
        self.assertEqual(calls, [{"command": ["echo"]}])

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

    def test_run_skill_executes_registered_skill(self):
        class Registry:
            def get(self, name):
                self.name = name
                from skills import Skill

                return Skill(
                    name="read_note",
                    description="Read a note.",
                    triggers=[],
                    inputs={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                    allowed_tools=["read_file"],
                    execution={"mode": "tool_sequence", "steps": [{"tool": "read_file", "args": {"path": "{{path}}"}}]},
                    path=Path("."),
                    instructions="",
                )

        with (
            patch("base.SkillRegistry", return_value=Registry()),
            patch.dict(base.TOOLS, {"read_file": lambda path: "hello"}),
        ):
            result = run_skill("read_note", {"path": "note.txt"})

        self.assertTrue(result["success"])
        self.assertEqual(result["steps"][0]["output"], "hello")


if __name__ == "__main__":
    unittest.main()
