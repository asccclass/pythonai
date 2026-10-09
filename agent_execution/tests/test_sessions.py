from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent_execution import execution_scope
from agent_execution.api import authenticated_api
from agent_execution.sessions import tenant_memory_path
from agent_runtime import AgentRuntime, AgentTurnResult
from communication_models import InboundMessage
from communication_store import CommunicationStore
from communication_worker import CommunicationWorker, agent_runtime_command_runner
from memory import MemoryStore


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        roles = root / "roles.json"
        roles.write_text('{"api:alice":"admin","api:bob":"admin"}', encoding="utf-8")
        env = patch.dict(os.environ, {"AGENT_TENANT_ROOT": str(root / "tenants"),
            "MEMORY_DB_PATH": str(root / "memory.db"), "COMMUNICATION_DB_PATH": str(root / "comm.db"),
            "AGENT_APPROVAL_DB": str(root / "approvals.db"), "ROLES_CONFIG": str(roles),
            "AGENT_API_TOKENS": json.dumps({"a" * 32: "alice", "b" * 32: "bob"})})
        (root / "workspace").mkdir()
        (root / "other").mkdir()
        projects = patch.dict(os.environ, {"AGENT_PROJECT_WORKSPACES": json.dumps({"default": str(root / "workspace"), "other": str(root / "other")})})
        projects.start()
        self.addCleanup(projects.stop)
        env.start()
        self.addCleanup(env.stop)
        self.store = CommunicationStore()

    def command(self, message_id, sender="alice", conversation="group", project="default", text="hello", platform="telegram"):
        return self.store.ingest_inbound_message(InboundMessage(platform, message_id, conversation, sender, text, project=project))[0]

    def runtime(self):
        return AgentRuntime(messages=[{"role": "system", "content": "system"}, {"role": "user", "content": "LOCAL SECRET"}],
            guard=Mock(), memory=MemoryStore(), memory_searcher=None, skill_matcher=None, memory_worker=None,
            memory_classifier_factory=None, semantic_extractor=None, procedure_matcher=None, run_agent=Mock())

    def test_two_users_concurrent_histories_memory_and_cancellation(self):
        template = self.runtime()
        runner = agent_runtime_command_runner(template, self.store)
        first, second = self.command("1"), self.command("2", "bob")
        self.store.mark_command_running(first.command_id)
        self.store.mark_command_running(second.command_id)
        barrier = threading.Barrier(2)
        entered = threading.Event()
        release = threading.Event()
        seen = {}
        def turn(text, runtime, attachments=None):
            from agent_execution.policy import current_context
            identity = current_context().identity
            seen[identity] = runtime
            runtime.messages.append({"role": "user", "content": identity})
            barrier.wait(3)
            entered.set()
            self.assertTrue(release.wait(3))
            return AgentTurnResult(str(runtime.check_cancelled()), None)
        with patch("communication_worker.run_agent_turn", side_effect=turn), ThreadPoolExecutor(2) as executor:
            future_a = executor.submit(runner, first)
            future_b = executor.submit(runner, second)
            self.assertTrue(entered.wait(3))
            self.store.cancel_command(first.command_id, owner=first)
            release.set()
            self.assertEqual(future_a.result(3).reply, "True")
            self.assertEqual(future_b.result(3).reply, "False")
        a, b = seen["telegram:alice"], seen["telegram:bob"]
        self.assertIsNot(a, b)
        self.assertNotEqual(a.memory.db_path, b.memory.db_path)
        self.assertNotIn("LOCAL SECRET", str(a.messages))
        self.assertNotIn("bob", str(a.messages))
        self.assertEqual(template.messages[-1]["content"], "LOCAL SECRET")
        self.assertIsNone(template.check_cancelled)

    def test_same_user_conversation_and_project_are_independent_and_clear_is_scoped(self):
        runner = agent_runtime_command_runner(self.runtime(), self.store)
        commands = [self.command("1"), self.command("2", conversation="other"), self.command("3", project="other")]
        seen = []
        def turn(text, runtime, attachments=None):
            from base import workspace_root
            from agent_execution.policy import current_context
            self.assertEqual(workspace_root().name, "other" if current_context().project == "other" else "workspace")
            seen.append(runtime)
            runtime.messages.append({"role": "user", "content": text})
            return AgentTurnResult("ok", None)
        with patch("communication_worker.run_agent_turn", side_effect=turn):
            for command in commands:
                runner(command)
            runner.clear_session(commands[0])
            runner(commands[0])
            runner(commands[1])
        self.assertEqual(len({runtime.memory.db_path for runtime in seen[:3]}), 3)
        self.assertIsNot(seen[0], seen[3])
        self.assertIs(seen[1], seen[4])

    def test_status_cancel_clear_and_resume_cannot_cross_owner(self):
        alice = self.command("1")
        bob = self.command("2", "bob")
        other_platform = self.command("3", platform="line")
        self.store.suspend_command(bob.command_id, '{"messages":[{"content":"BOB SECRET"}]}', "tool1")
        self.assertIsNone(self.store.get_suspended_command("group", owner=alice))
        self.assertEqual(self.store.clear_suspended_command("group", owner=alice), 0)
        self.assertFalse(self.store.cancel_command(bob.command_id, owner=alice))
        self.assertIsNone(self.store.resolve_command_id(bob.command_id, owner=alice))
        self.assertEqual([job.command_id for job in self.store.query_jobs_status(owner=alice)], [alice.command_id])
        self.store.cancel_session(alice)
        self.assertEqual(self.store.command_by_id(bob.command_id).status, "suspended")
        self.assertEqual(self.store.command_by_id(other_platform.command_id).status, "pending")

    def test_cancelled_job_never_becomes_completed_and_claim_is_atomic(self):
        command = self.command("1")
        with ThreadPoolExecutor(4) as executor:
            self.assertEqual(sum(executor.map(lambda _: self.store.mark_command_running(command.command_id), range(4))), 1)
        self.store.cancel_command(command.command_id)
        self.store.complete_command(command.command_id, "fake success")
        self.assertEqual(self.store.command_by_id(command.command_id).status, "cancelled")
        self.assertFalse(self.store.mark_command_running(command.command_id))

    def test_runner_internal_typeerror_not_replayed(self):
        command = self.command("1")
        runner = Mock(side_effect=TypeError("internal failure"))
        worker = CommunicationWorker(self.store, {"telegram": Mock()}, runner)
        worker.process_next()
        worker.drain(3)
        runner.assert_called_once()
        self.assertEqual(self.store.command_by_id(command.command_id).status, "failed")

    def test_api_tenant_project_memory_tasks_and_approvals_isolation(self):
        headers_a = {"Authorization": "Bearer " + "a" * 32, "X-Agent-Session": "one"}
        headers_b = {"Authorization": "Bearer " + "b" * 32, "X-Agent-Session": "one"}
        body = json.dumps({"subject": "user", "predicate": "secret", "object": "ALICE"})
        self.assertEqual(authenticated_api("/api/memories", "POST", body, headers_a)[0], 201)
        for value in ("BOB", "BOB SECOND"):
            self.assertEqual(authenticated_api("/api/memories", "POST", body.replace("ALICE", value), headers_b)[0], 201)
        self.assertEqual(authenticated_api("/api/memories/2", "GET", None, headers_a)[0], 404)
        self.assertNotIn("BOB", str(authenticated_api("/api/memories", "GET", None, headers_a)))
        project = {**headers_a, "X-Agent-Project": "other"}
        self.assertEqual(authenticated_api("/api/memories", "GET", None, project), (200, []))
        self.command("private-job", "bob", platform="api", text="BOB TASK")
        self.assertEqual(authenticated_api("/api/tasks", "GET", None, headers_a), (200, []))
        self.assertEqual(authenticated_api("/api/stats", "GET", None, headers_a)[1]["pending_tasks"], 0)
        status, result = authenticated_api("/api/memories/1", "DELETE", b"{}", headers_a)
        self.assertEqual(status, 409)
        key = result["approval_id"]
        self.assertEqual(authenticated_api(f"/api/approvals/{key}/approve", "POST", b"", project)[0], 403)
        self.assertEqual(authenticated_api(f"/api/approvals/{key}/approve", "POST", b"", headers_a)[0], 200)
        self.assertEqual(authenticated_api("/api/memories/1", "DELETE", b"{}", {**project, "X-Agent-Approval": key})[0], 403)
        with execution_scope(source="api", identity="api:alice"):
            self.assertEqual(MemoryStore().active_semantic_memories()[0]["object"], "ALICE")
        self.assertTrue(tenant_memory_path("api:alice").exists())

    def test_websocket_device_collision_and_proactive_target_do_not_cross_users(self):
        from websocket_server import Hub, websocket_tokens
        hub = Hub()
        alice = hub.get("same-device", user_id="alice")
        bob = hub.get("same-device", user_id="bob")
        other_project = hub.get("same-device", user_id="alice", project="other")
        alice.ws, bob.ws, other_project.ws = Mock(), Mock(), Mock()
        bob.focused = True
        self.assertIsNot(alice, bob)
        self.assertIs(hub.pick_for_proactive("alice"), alice)
        self.assertIs(hub.pick_for_proactive("alice", "other"), other_project)
        with patch.dict(os.environ, {"COMPANION_WS_TOKENS": "", "COMPANION_WS_TOKEN": ""}):
            self.assertEqual(websocket_tokens(), {})

    def test_scoped_workspace_rejects_other_project_paths(self):
        from agent_execution.sessions import project_workspace
        from base import read_file
        other = project_workspace("other")
        default = project_workspace("default")
        (default / "private.txt").write_text("DEFAULT SECRET", encoding="utf-8")
        with execution_scope(workspace=other):
            from agent_execution import execute_tool
            with self.assertRaises(PermissionError):
                execute_tool("read_file", {"path": str(default / "private.txt")}, read_file)
        with self.assertRaises(PermissionError):
            project_workspace("unconfigured")
