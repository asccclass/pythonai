from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from agent_execution.policy import execution_scope, current_context
from agent_execution.tasks import TaskStore, task_owner
from agent_execution.companion.outputs import available, read_chunk
from agent_execution.companion.turn_cleanup import delete, status
from agent_execution.companion.text_attachments import references
from agent_execution.workspace_lifecycle import LifecycleStore
from agent_runtime import build_user_message
from base import write_file
from permissions import set_current_role


@unittest.skipUnless(os.name=='nt','native Windows cleanup')
class TurnCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name); self.root = self.home/'workspace'; self.root.mkdir()
        env = patch.dict(os.environ,{'AGENT_TASK_DB':str(self.home/'tasks.db'),
            'AGENT_APPROVAL_DB':str(self.home/'approvals.db'),'AGENT_WORKSPACE_CLEANUP':'1',
            'AGENT_TENANT_ROOT':str(self.home/'tenants'),'SCHEDULER_DB_PATH':str(self.home/'scheduler.db')})
        env.start(); self.addCleanup(env.stop)
        scope = execution_scope(source='websocket',identity='websocket:alice',session='default:one',
                                conversation='one',workspace=self.root,task_id='request')
        scope.__enter__(); self.addCleanup(lambda:scope.__exit__(None,None,None))
        role = set_current_role('admin'); role.__enter__(); self.addCleanup(lambda:role.__exit__(None,None,None))

    def source(self):
        message = build_user_message('翻譯成繁體中文',[NS(filename='source.txt',content_type='text/plain',data=b'Original input')])
        return message, self.root/references([message])[1][0]['path']

    def test_delete_turn_removes_only_its_output_and_unshared_uploaded_copy(self):
        original = self.home/'original.txt'; original.write_text('Original input')
        _, source = self.source()
        item = write_file('translated.txt','翻譯')['artifacts'][0]
        (self.root/'unmanaged.txt').write_text('keep')
        with execution_scope(task_id='other'):write_file('other.txt','其他問答')
        result = delete('request')
        self.assertEqual(result['state'],'deleted')
        self.assertEqual(result['files'],1)
        self.assertFalse((self.root/'translated.txt').exists())
        self.assertFalse(source.parent.exists())
        self.assertTrue(original.exists()); self.assertTrue((self.root/'unmanaged.txt').exists())
        self.assertTrue((self.root/'other.txt').exists())
        with self.assertRaises(PermissionError):read_chunk(item['id'],0)
        with execution_scope(task_id='fresh'):
            write_file('fresh.txt','新的問答仍可使用此對話')
        self.assertEqual(status('request')['state'],'deleted')

    def test_missing_output_and_manually_removed_source_are_skipped(self):
        _, source = self.source()
        write_file('translated.txt','翻譯')
        (self.root/'translated.txt').unlink(); source.unlink(); source.parent.rmdir()
        self.assertEqual(delete('request')['state'],'deleted')
        self.assertEqual(delete('request')['state'],'deleted')

    def test_shared_uploaded_source_is_kept_until_last_turn_is_deleted(self):
        _, source = self.source(); write_file('one.txt','一')
        with execution_scope(task_id='other'):
            self.source(); write_file('two.txt','二')
        delete('request')
        self.assertTrue(source.exists()); self.assertTrue((self.root/'two.txt').exists())
        delete('other')
        self.assertFalse(source.parent.exists())

    def test_completed_retry_never_deletes_a_new_file_reusing_the_name(self):
        write_file('translated.txt','old'); delete('request')
        with execution_scope(task_id='new'):
            write_file('translated.txt','new')
        self.assertEqual(delete('request')['state'],'deleted')
        self.assertEqual((self.root/'translated.txt').read_text(),'new')

    def test_other_conversation_cannot_delete_registered_output(self):
        write_file('translated.txt','翻譯')
        for changes in ({'conversation':'two'},{'identity':'websocket:bob'}):
            with execution_scope(**changes), self.assertRaises(PermissionError):delete('request')
        self.assertTrue((self.root/'translated.txt').exists())

    def test_partial_cleanup_failure_keeps_intent_and_resumes_exact_files(self):
        _, source = self.source(); write_file('translated.txt','翻譯')
        from agent_execution.companion.outputs import purge
        def after_output(*args):
            purge(*args)
            raise OSError('simulated crash after output deletion')
        with patch('agent_execution.companion.outputs.purge',side_effect=after_output), self.assertRaises(OSError):
            delete('request')
        self.assertEqual(status('request')['state'],'blocked')
        self.assertTrue(source.exists())
        self.assertEqual(delete('request')['state'],'deleted')
        self.assertFalse(source.parent.exists())

    def test_unexpected_source_files_are_preserved_before_any_cleanup(self):
        _, source = self.source(); write_file('translated.txt','翻譯')
        (source.parent/'user-added.txt').write_text('unrelated')
        with self.assertRaises(PermissionError):delete('request')
        self.assertTrue((self.root/'translated.txt').exists())
        self.assertTrue((source.parent/'user-added.txt').exists())

    def test_partial_source_purge_can_finish_after_source_file_is_already_gone(self):
        import ctypes
        from agent_execution.workspace_lifecycle.native import PinnedTree
        _, source = self.source(); write_file('translated.txt','翻譯')
        class PartialPurge(PinnedTree):
            def purge(self):
                if self.path==source.parent.resolve():
                    handle = self.handles['source.txt']
                    flag = ctypes.c_ubyte(1)
                    if not self.kernel.SetFileInformationByHandle(handle,4,ctypes.byref(flag),ctypes.sizeof(flag)):
                        raise OSError('Cannot delete test source')
                    self.kernel.CloseHandle(handle)
                    del self.handles['source.txt']
                    raise OSError('simulated interruption before directory removal')
                return super().purge()
        with patch('agent_execution.workspace_lifecycle.native.PinnedTree',PartialPurge), self.assertRaises(OSError):
            delete('request')
        self.assertFalse(source.exists()); self.assertTrue(source.parent.exists())
        self.assertEqual(delete('request')['state'],'deleted')
        self.assertFalse(source.parent.exists())

    def test_live_task_is_not_cancelled_or_deleted(self):
        write_file('translated.txt','翻譯')
        store = TaskStore(); owner = task_owner(current_context())
        token = store.claim(owner,'request','goal',[])
        try:
            with self.assertRaisesRegex(ValueError,'stop'):delete('request')
            self.assertTrue((self.root/'translated.txt').exists())
        finally:store.finish(owner,'request',token,'completed','done')

    def test_background_task_in_same_conversation_blocks_file_cleanup(self):
        write_file('translated.txt','翻譯')
        store = TaskStore(); owner = task_owner(current_context())
        token = store.claim(owner,'background','background work',[])
        try:
            with self.assertRaisesRegex(ValueError,'stop'):delete('request')
            self.assertTrue((self.root/'translated.txt').exists())
            self.assertEqual(store.get(owner,'background')['state'],'running')
        finally:store.finish(owner,'background',token,'completed','done')

    def test_legacy_source_references_are_recovered_from_checkpoint(self):
        message, source = self.source(); write_file('translated.txt','翻譯')
        store = TaskStore(); owner = task_owner(current_context())
        token = store.claim(owner,'request','goal',[message]); store.finish(owner,'request',token,'completed','done')
        with closing(store.connect()) as conn, conn:conn.execute('DELETE FROM companion_attachment_refs')
        self.assertEqual(delete('request')['state'],'deleted')
        self.assertFalse(source.parent.exists())

    def test_deleted_turn_cannot_generate_new_outputs(self):
        write_file('translated.txt','翻譯'); delete('request')
        with self.assertRaises(PermissionError):write_file('late.txt','must not reappear')
        self.assertFalse((self.root/'late.txt').exists())
