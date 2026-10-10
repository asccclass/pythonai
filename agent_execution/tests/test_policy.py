import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import base
from agent_execution import execute_tool, execution_scope, policy_tool
from agent_execution.policy import PolicyDenied, current_context, git_workspace
from permissions import current_role, set_current_role
from scheduler import SchedulerStore, execute_job
from skills import Skill, SkillExecutor


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "workspace"
        self.root.mkdir()
        environment = patch.dict(os.environ, {"AGENT_WORKSPACE_ROOT": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)

    def skill(self, tool, args):
        return Skill("example", "Example", [], {"type": "object", "properties": {}},
                     [tool], {"mode": "tool_sequence", "steps": [{"tool": tool, "args": args}]},
                     Path(self.directory.name) / "skill", "")

    def test_operator_cannot_bypass_role_through_skill(self):
        tool = Mock()
        store = Mock()
        with set_current_role("operator"), execution_scope(identity="telegram:123"):
            result = SkillExecutor({"write_file": tool}, memory=store, episode_id=7).execute(
                self.skill("write_file", {"path": "note.txt", "content": "bad"}), {})
        self.assertFalse(result.success)
        tool.assert_not_called()
        audit = [call for call in store.add_event.call_args_list if call.args[1] == "permission_denied"]
        self.assertEqual(audit[0].kwargs["metadata"]["identity"], "telegram:123")

    def test_viewer_skill_cannot_invoke_dynamic_or_mcp_tool(self):
        for name in ("created_tool", "mcp_external_write"):
            tool = Mock()
            with set_current_role("viewer"):
                result = SkillExecutor({name: tool}).execute(self.skill(name, {}), {})
            self.assertFalse(result.success)
            tool.assert_not_called()

    def test_traversal_rejected_before_custom_tool_call(self):
        tool = Mock()
        with self.assertRaises(PolicyDenied):
            execute_tool("custom_tool", {"options": {"output_path": "../outside"}}, tool)
        tool.assert_not_called()

    def test_symlink_escape_rejected(self):
        outside = Path(self.directory.name) / "outside"
        outside.mkdir()
        link = self.root / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        tool = Mock()
        with self.assertRaises(PolicyDenied):
            execute_tool("write_file", {"path": "link/file.txt"}, tool)
        tool.assert_not_called()

    def test_skill_cannot_grant_arbitrary_trusted_roots(self):
        tool = Mock()
        result = SkillExecutor({"run_command": tool}).execute(self.skill(
            "run_command", {"command": ["echo"], "trusted_asset_roots": [self.directory.name]}), {})
        self.assertFalse(result.success)
        tool.assert_not_called()

    def test_registered_skill_assets_remain_usable(self):
        skill = self.skill("run_command", {})
        skill.path.mkdir()
        executable = skill.path / "tool.exe"
        calls = []
        def run_command(command, cwd, trusted_asset_roots=None):
            calls.append((command, cwd, trusted_asset_roots))
            return "ok"
        skill.execution["steps"][0]["args"] = {"command": [str(executable)], "cwd": str(skill.path)}
        result = SkillExecutor({"run_command": run_command}).execute(skill, {})
        self.assertTrue(result.success)
        self.assertEqual(calls[0][2], [str(skill.path)])

    def test_model_cannot_inject_memory_or_trusted_roots(self):
        tool = Mock(return_value="ok")
        call = SimpleNamespace(function=SimpleNamespace(name="run_command", arguments=json.dumps({
            "command": ["echo"], "trusted_asset_roots": [self.directory.name], "memory": "spoof", "episode_id": 90})))
        with patch.dict(base.TOOLS, {"run_command": tool}):
            self.assertEqual(base.run_tool(call), "ok")
        tool.assert_called_once_with(command=["echo"])

    def test_dynamic_tool_wrapper_checks_direct_calls(self):
        calls = []
        @policy_tool("custom_tool")
        def custom_tool(**kwargs):
            calls.append(kwargs)
            return "ok"
        self.assertEqual(custom_tool(path="note.txt"), "ok")
        with set_current_role("operator"), self.assertRaises(PolicyDenied):
            custom_tool(path="note.txt")
        with self.assertRaises(PolicyDenied):
            custom_tool(path="../outside")
        self.assertEqual(calls, [{"path": "note.txt"}])

    def test_git_cannot_use_parent_repository(self):
        result = subprocess.CompletedProcess([], 0, stdout=self.directory.name, stderr="")
        with patch("agent_execution.process.run_process", return_value=result), self.assertRaises(PermissionError):
            git_workspace()

    def test_direct_mcp_call_denied_before_starting_servers(self):
        from mcp_manager import execute_mcp_tool
        with set_current_role("viewer"), patch("mcp_manager.get_mcp_manager") as manager:
            with self.assertRaises(PolicyDenied):
                execute_mcp_tool("mcp_external_read", {})
        manager.assert_not_called()

    def test_dynamic_creation_cannot_escape_or_escalate(self):
        with set_current_role("operator"), self.assertRaises(PolicyDenied):
            base.create_dynamic_tool("example", "pass", '{"name":"example"}')
        self.assertIn("identifier", base.create_dynamic_tool("../outside", "pass", '{"name":"example"}'))

    def test_index_build_and_search_share_workspace_location(self):
        from build_codebase_index import build_index
        from tools.search_codebase import search_codebase
        (self.root / "sample.py").write_text("def hello(): pass", encoding="utf-8")
        build_index(".")
        self.assertIn("hello", search_codebase("hello",mode='symbol'))
        self.assertTrue((self.root / "codebase_index.db").exists())

    def test_command_option_assignment_cannot_escape(self):
        with self.assertRaises(PermissionError):
            base.validate_command_paths(["program", f"--output={Path(self.directory.name) / 'outside'}"])

    def test_scheduler_creation_and_execution_denied_for_operator(self):
        store = SchedulerStore(self.root / "scheduler.db")
        with set_current_role("operator"):
            with self.assertRaises(PolicyDenied):
                store.add_job("test", "* * * * *", "command", {"command": ["echo", "bad"]})
            result = execute_job({"job_type": "command", "owner_role": "admin",
                                  "job_payload": json.dumps({"command": ["echo", "bad"]})})
        self.assertEqual(store.list_jobs(), [])
        self.assertEqual(result[0], "failed")

    def test_scheduler_persists_identity_and_uses_command_policy(self):
        store = SchedulerStore(self.root / "scheduler.db")
        with execution_scope(identity="telegram:123"):
            store.add_job("test", "* * * * *", "command", {"command": ["echo", "hello"]})
        job = store.list_jobs()[0]
        self.assertEqual(job["owner_identity"], "telegram:123")
        def command(**kwargs):
            self.assertEqual(current_context().source, "scheduler")
            self.assertEqual(current_context().identity, "telegram:123")
            return subprocess.CompletedProcess([], 0, "hello", "")
        with patch("base.run_command", side_effect=command):
            self.assertEqual(execute_job(job), ("completed", "hello", ""))
        job["owner_role"] = "viewer"
        with patch("base.run_command") as run:
            self.assertEqual(execute_job(job)[0], "failed")
            run.assert_not_called()

    def test_scheduler_rejects_shell_strings_and_outside_cwd(self):
        for payload in ({"command": "echo good & echo bad"},
                        {"command": ["echo"], "cwd": self.directory.name}):
            with patch("base.run_command") as run:
                result = execute_job({"job_type": "command", "job_payload": json.dumps(payload)})
            self.assertEqual(result[0], "failed")
            run.assert_not_called()

    def test_public_path_tools_reject_traversal(self):
        from tools.ast_edit import ast_edit
        from tools.env_manager import env_manager
        from tools.project_scaffold import project_scaffold
        from tools.run_test_suite import run_test_suite
        for function, args in ((ast_edit, ("../outside.py", "function_definition", "x", "pass")),
                               (env_manager, ("create_venv", "../outside")),
                               (project_scaffold, ("../outside", "python_basic")),
                               (run_test_suite, ("../outside.py",))):
            with self.assertRaises(PolicyDenied):
                function(*args)

    def test_subagent_submission_preserves_role_and_identity(self):
        import tools.invoke_subagent as module
        seen = []
        def worker(*args):
            seen.append((current_role(), current_context().identity))
            return 'a'*32
        # A narrow test policy allows spawning under viewer to prove context propagation.
        with set_current_role("viewer"), execution_scope(identity="telegram:123"), \
             patch("agent_execution.policy.check_tool_permission", return_value=True), \
             patch.object(module, "Coordinator") as coordinator, patch.object(module,'start_coordinator'):
            coordinator.return_value.submit.side_effect=worker
            result=module.invoke_subagent("reviewer", "read only")
            self.assertEqual(result.evidence['agent_id'],'a'*32)
        self.assertEqual(seen, [("viewer", "telegram:123")])
