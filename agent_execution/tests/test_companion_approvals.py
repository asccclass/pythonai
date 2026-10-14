import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_execution import execution_scope
from agent_execution.approvals import ApprovalRequired, ApprovalStore
from agent_execution.companion.approval_bridge import approval_bridge
from agent_execution.policy import execute_tool
from permissions import set_current_role


class CompanionApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.roles = root / 'roles.json'
        self.roles.write_text(json.dumps({'websocket:alice': 'admin'}), encoding='utf-8')
        env = patch.dict(os.environ, {'AGENT_APPROVAL_DB': str(root / 'approvals.db'), 'ROLES_CONFIG': str(self.roles)})
        env.start()
        self.addCleanup(env.stop)
        self.store = ApprovalStore()
        self.effect = Mock(return_value='done')

    def execute(self, callback=None):
        with execution_scope(source='websocket', identity='websocket:alice', session='default:device', project='default'), set_current_role('admin'), approval_bridge(callback):
            return execute_tool('manage_memory', {'action': 'archive', 'memory_id': 99}, self.effect)

    def test_transport_without_waiter_keeps_existing_approval_flow(self):
        with self.assertRaises(ApprovalRequired):
            self.execute()
        self.effect.assert_not_called()

    def test_approve_consumes_exact_operation_once(self):
        def approve(key, name, arguments):
            self.assertEqual(name, 'manage_memory')
            self.assertEqual(arguments['memory_id'], 99)
            self.store.decide(key, 'websocket:alice', 'default:device', True)
            self.key = key
            return True
        self.assertEqual(self.execute(approve), 'done')
        self.effect.assert_called_once()
        self.assertEqual(self.store.decision_state(self.key, 'websocket:alice', 'default:device'), 'consumed')
        with self.assertRaises(PermissionError):
            self.store.decide(self.key, 'websocket:alice', 'default:device', True)

    def test_waiter_cannot_grant_permission_without_stored_human_decision(self):
        with self.assertRaises(PermissionError):
            self.execute(lambda *args: True)
        self.effect.assert_not_called()

    def test_denial_and_role_change_do_not_execute(self):
        with self.assertRaises(PermissionError):
            self.execute(lambda *args: False)
        def changed(key, *args):
            self.store.decide(key, 'websocket:alice', 'default:device', True)
            self.roles.write_text(json.dumps({'websocket:alice': 'viewer'}), encoding='utf-8')
            return True
        with self.assertRaises(PermissionError):
            self.execute(changed)
        self.effect.assert_not_called()

    def test_cancelled_wait_is_not_approvable_and_scope_is_exact(self):
        key = self.store.request('websocket:alice', 'default:device', 'default:manage_memory', {'memory_id': 99})
        self.assertEqual(self.store.decision_state(key, 'websocket:alice', 'other'), 'expired')
        self.store.cancel_wait(key, 'websocket:alice', 'default:device')
        with self.assertRaises(PermissionError):
            self.store.decide(key, 'websocket:alice', 'default:device', True)
