import json
import os
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent_execution.approvals import ApprovalRequired, ApprovalStore, human_command
from agent_execution.api import authenticated_api
from agent_execution import execution_scope
from permissions import get_user_role, set_current_role
from memory_tools import manage_memory
import base


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.roles = root / "roles.json"
        self.roles.write_text(json.dumps({"api:alice": "admin", "api:bob": "viewer"}), encoding="utf-8")
        self.env = patch.dict(os.environ, {"AGENT_APPROVAL_DB": str(root / "approvals.db"),
                                         "ROLES_CONFIG": str(self.roles),
                                         "AGENT_API_TOKENS": json.dumps({"a" * 32: "alice", "b" * 32: "bob"})})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = ApprovalStore()
        self.headers = {"Authorization": "Bearer " + "a" * 32, "X-Agent-Session": "one"}

    def test_remote_defaults_and_invalid_roles_fail_closed(self):
        with patch.dict(os.environ, {"ROLES_CONFIG": "", "COMM_ADMIN_SENDERS": ""}):
            self.assertEqual(get_user_role("telegram:123"), "viewer")
        self.roles.write_text('{"api:alice":"root"}', encoding="utf-8")
        self.assertEqual(get_user_role("api:alice"), "viewer")

    def test_communication_admins_require_exact_platform_identity(self):
        with patch.dict(os.environ, {"ROLES_CONFIG": "",
                "COMM_ADMIN_SENDERS": " telegram:123 , discord:456, line:abc, api:alice, local, other:123, telegram: "}):
            for identity in ("telegram:123", "discord:456", "line:abc"):
                with self.subTest(identity=identity):
                    self.assertEqual(get_user_role(identity), "admin")
            for identity in ("telegram:12", "telegram:1234", "123", "api:alice", "local", "other:123", "telegram:"):
                with self.subTest(identity=identity):
                    self.assertEqual(get_user_role(identity), "viewer")

    def test_allowlist_does_not_grant_admin(self):
        with patch.dict(os.environ, {"ROLES_CONFIG": "", "COMM_ADMIN_SENDERS": "",
                                    "COMM_ALLOWED_SENDERS": "telegram:123"}):
            self.assertEqual(get_user_role("telegram:123"), "viewer")

    def test_roles_file_overrides_communication_admin_list(self):
        with patch.dict(os.environ, {"COMM_ADMIN_SENDERS": "telegram:123"}):
            for role in ("viewer", "operator", "admin"):
                with self.subTest(role=role):
                    self.roles.write_text(json.dumps({"telegram:123": role}), encoding="utf-8")
                    self.assertEqual(get_user_role("telegram:123"), role)
            for contents in ('{}', '[]', 'bad json', '{"telegram:123":"root"}'):
                with self.subTest(contents=contents):
                    self.roles.write_text(contents, encoding="utf-8")
                    self.assertEqual(get_user_role("telegram:123"), "viewer")
            self.roles.unlink()
            self.assertEqual(get_user_role("telegram:123"), "viewer")

    def test_identity_session_parameters_replay_and_expiry(self):
        args = {"memory_id": 1, "action": "archive"}
        key = self.store.request("alice", "one", "memory", args)
        with self.assertRaises(PermissionError):
            self.store.decide(key, "bob", "one", True)
        with self.assertRaises(PermissionError):
            self.store.decide(key, "alice", "two", True)
        self.store.decide(key, "alice", "one", True)
        for identity, session, action, arguments in (("bob", "one", "memory", args),
                ("alice", "two", "memory", args), ("alice", "one", "other", args),
                ("alice", "one", "memory", {**args, "memory_id": 2})):
            with self.assertRaises(PermissionError):
                self.store.consume(key, identity, session, action, arguments)
        self.store.consume(key, "alice", "one", "memory", args)
        with self.assertRaises(PermissionError):
            self.store.consume(key, "alice", "one", "memory", args)
        expired = self.store.request("alice", "one", "memory", args, ttl=-1)
        with self.assertRaises(PermissionError):
            self.store.decide(expired, "alice", "one", True)

    def test_concurrent_consumption_only_one_wins(self):
        key = self.store.request("a", "s", "tool", {})
        self.store.decide(key, "a", "s", True)
        def consume(_):
            try:
                self.store.consume(key, "a", "s", "tool", {})
                return True
            except PermissionError:
                return False
        with ThreadPoolExecutor(4) as executor:
            self.assertEqual(sum(executor.map(consume, range(4))), 1)

    def test_model_confirm_cannot_grant_and_direct_calls_are_guarded(self):
        memory = Mock()
        with execution_scope(identity="alice", session="one"):
            with self.assertRaises(ApprovalRequired) as required:
                manage_memory("archive", 4, confirm=True, memory=memory, episode_id=8)
            memory.archive_semantic_memory.assert_not_called()
            key = required.exception.approval_id
            self.assertIn("saved", human_command("/approve " + key, "alice", "one"))
            result = manage_memory("archive", 4, memory=memory, episode_id=8, approval_id=key)
            self.assertIn("Success", result)
            with self.assertRaises(PermissionError):
                manage_memory("archive", 4, memory=memory, episode_id=8, approval_id=key)
        memory.archive_semantic_memory.assert_called_once_with(4, reason="")

    def test_model_entry_and_decorator_consume_once(self):
        memory = Mock()
        call = SimpleNamespace(function=SimpleNamespace(name="manage_memory", arguments='{"action":"archive","memory_id":3,"confirm":true}'))
        with execution_scope(identity="alice", session="one"):
            self.assertIn("approval_required", base.run_tool_with_context(call, memory, 1))
            key = self.store.pending("alice", "one")[0]["id"]
            self.store.decide(key, "alice", "one", True)
            call.function.arguments = json.dumps({"action": "archive", "memory_id": 3, "approval_id": key})
            self.assertIn("Success", base.run_tool_with_context(call, memory, 1))
        memory.archive_semantic_memory.assert_called_once()

    def test_api_auth_role_and_human_decision(self):
        with patch("web_api.handle_api_request", return_value=(200, {"ok": True})) as handle:
            self.assertEqual(authenticated_api("/api/memories", "POST", b"{}", {})[0], 401)
            viewer = {"Authorization": "Bearer " + "b" * 32}
            self.assertEqual(authenticated_api("/api/memories", "POST", b"{}", viewer)[0], 403)
            handle.assert_not_called()
            path, body = "/api/memories/1", b'{"reason":"obsolete"}'
            status, payload = authenticated_api(path, "DELETE", body, self.headers)
            self.assertEqual(status, 409)
            key = payload["approval_id"]
            handle.assert_not_called()
            self.assertEqual(authenticated_api(f"/api/approvals/{key}/approve", "POST", b"", self.headers)[0], 200)
            approved = {**self.headers, "X-Agent-Approval": key}
            self.assertEqual(authenticated_api(path, "DELETE", b"{}", approved)[0], 403)
            self.assertEqual(authenticated_api(path, "DELETE", body, approved)[0], 200)
            self.assertEqual(authenticated_api(path, "DELETE", body, approved)[0], 403)
            handle.assert_called_once()

    def test_api_invalid_config_and_unknown_token(self):
        for config in ('[]', 'bad json', '{"short":"alice"}'):
            with patch.dict(os.environ, {"AGENT_API_TOKENS": config}):
                self.assertEqual(authenticated_api("/api/tasks", "GET", None, self.headers)[0], 401)
        self.assertEqual(authenticated_api("/api/memories/1", "PUT", b"\xff", self.headers)[0], 400)

    def test_approved_decision_expiry_prevents_consumption(self):
        with patch("agent_execution.approvals.time.time", return_value=1000):
            key = self.store.request("alice", "one", "tool", {}, ttl=10)
            self.store.decide(key, "alice", "one", True)
        with patch("agent_execution.approvals.time.time", return_value=1011):
            with self.assertRaises(PermissionError):
                self.store.consume(key, "alice", "one", "tool", {})

    def test_deny_and_pending_are_scoped(self):
        key = self.store.request("alice", "one", "memory", {})
        self.assertEqual(self.store.pending("alice", "two"), [])
        self.store.decide(key, "alice", "one", False)
        with self.assertRaises(PermissionError):
            self.store.consume(key, "alice", "one", "memory", {})
        with set_current_role("viewer"):
            self.assertIn("denied", human_command("/approve " + key, "alice", "one"))
