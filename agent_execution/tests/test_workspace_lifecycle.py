from contextlib import closing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_execution.policy import execution_scope
from agent_execution.workspace_lifecycle import LifecycleStore
from agent_execution.workspace_lifecycle.store import session_key


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name)
        self.root=self.home/'workspace'
        self.root.mkdir()
        env=patch.dict(os.environ,{'AGENT_TASK_DB':str(self.home/'tasks.db'),
            'AGENT_APPROVAL_DB':str(self.home/'approvals.db'), 'AGENT_WORKSPACE_CLEANUP':'1'})
        env.start();self.addCleanup(env.stop)
        scope=execution_scope(source='websocket',identity='websocket:alice',project='default',
            session='default:device:one',conversation='one',workspace=self.root)
        scope.__enter__();self.addCleanup(lambda:scope.__exit__(None,None,None))
        self.store=LifecycleStore()

    def create(self,name='example',purpose='temporary'):
        key=self.store.create(name,purpose)
        (self.root/name/'main.txt').write_text('hello',encoding='utf-8')
        return key

    def test_registry_and_preview_preserve_unmanaged_and_retained(self):
        self.root.joinpath('old').mkdir()
        key=self.create()
        retained=self.create('keep','retained')
        inventory=self.store.inventory()
        self.assertEqual(inventory['unmanaged'],['old'])
        preview=self.store.preview('delete_session')
        self.assertEqual([r['id'] for r in preview['items']],[key])
        self.assertEqual(preview['kept'][0]['id'],retained)
        self.assertTrue(self.root.joinpath('example/main.txt').exists())

    def test_same_conversation_on_another_device_has_same_identity(self):
        before=session_key()
        with execution_scope(session='default:other-device:one'):
            self.assertEqual(session_key(),before)
        with execution_scope(conversation='',session='default:device'):
            legacy=session_key()
            with execution_scope(session='default:other-device'):self.assertNotEqual(session_key(),legacy)

    def test_shared_reference_and_cross_session_write_guard(self):
        key=self.create()
        with execution_scope(conversation='two',session='default:device:two'):
            with self.assertRaises(PermissionError):self.store.assert_writable(self.root/'example/main.txt')
            self.store.attach(key)
            self.store.assert_writable(self.root/'example/main.txt')
            preview=self.store.preview('delete_session')
            self.assertEqual(preview['items'],[])
            self.assertEqual(preview['kept'][0]['reason'],'shared')
        with execution_scope(identity='websocket:bob'):
            with self.assertRaises(PermissionError):self.store.get(key)
            with self.assertRaises(PermissionError):self.store.assert_writable(self.root/'example/main.txt')

    def test_root_overlap_link_and_content_limits(self):
        for name in ('.','../outside',str(self.root)):
            with self.assertRaises((PermissionError,ValueError)):self.store.create(name)
        key=self.create()
        with self.assertRaises(ValueError):self.store.create('example/nested')
        self.store.retain(key)
        with self.assertRaises(ValueError):self.store.preview('archive',key)

    def test_active_task_blocks_preview_and_deleted_session_blocks_creation(self):
        from agent_execution.tasks import task_owner
        from agent_execution.policy import current_context
        self.store.ensure_session()
        self.store.tasks.claim(task_owner(current_context()),'busy','work',[])
        self.assertEqual(self.store.preview('delete_session')['blocking_tasks'],['busy'])
        with closing(self.store.connect()) as conn,conn:
            conn.execute("UPDATE wl_sessions SET state='deleted' WHERE id=?",(session_key(),))
        with self.assertRaises(PermissionError):self.store.create('late')
