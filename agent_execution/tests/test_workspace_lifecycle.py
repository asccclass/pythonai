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
            'AGENT_APPROVAL_DB':str(self.home/'approvals.db'), 'AGENT_WORKSPACE_CLEANUP':'1',
            'SCHEDULER_DB_PATH':str(self.home/'scheduler.db'),'AGENT_TENANT_ROOT':str(self.home/'tenants')})
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

    def approve(self,plan):
        from agent_execution.approvals import ApprovalStore
        from agent_execution.policy import current_context
        request=self.store.request(plan['plan_id'])
        context=current_context()
        ApprovalStore().decide(request['approval_id'],context.identity,context.session,True)
        return request['approval_id']

    @unittest.skipUnless(os.name=='nt','requires Windows native handles')
    def test_archive_restore_and_idempotent_operation(self):
        key=self.create()
        preview=self.store.preview('archive',key)
        approval=self.approve(preview)
        result=self.store.execute(preview['plan_id'],approval)
        self.assertEqual(result['state'],'completed',result)
        self.assertFalse(self.root.joinpath('example').exists())
        archived=Path(self.store.get(key)['archive'])
        self.assertEqual((archived/'main.txt').read_text(),'hello')
        self.assertEqual(self.store.execute(preview['plan_id'],approval)['state'],'completed')
        restore=self.store.preview('restore',key)
        result=self.store.execute(restore['plan_id'],self.approve(restore))
        self.assertEqual(result['state'],'completed',result)
        self.assertEqual((self.root/'example/main.txt').read_text(),'hello')

    def test_changed_snapshot_or_reference_invalidates_approval(self):
        key=self.create()
        plan=self.store.preview('archive',key)
        approval=self.approve(plan)
        (self.root/'example/main.txt').write_text('changed')
        with self.assertRaises(ValueError):self.store.execute(plan['plan_id'],approval)
        self.assertTrue((self.root/'example').exists())

    @unittest.skipUnless(os.name=='nt','requires Windows native handles')
    def test_session_purge_preserves_shared_retained_and_unknown(self):
        key=self.create()
        retained=self.create('keep','retained')
        shared=self.create('shared')
        with execution_scope(conversation='two',session='default:device:two'):self.store.attach(shared)
        self.root.joinpath('unknown').mkdir()
        plan=self.store.preview('delete_session')
        result=self.store.execute(plan['plan_id'],self.approve(plan))
        self.assertEqual(result['state'],'completed',result)
        self.assertEqual(self.store.get(key)['state'],'purged')
        self.assertFalse(self.root.joinpath('example').exists())
        for name in ('keep','shared','unknown'):self.assertTrue(self.root.joinpath(name).exists())
        with self.assertRaises(PermissionError):self.store.create('late')

    @unittest.skipUnless(os.name=='nt','requires Windows native handles')
    def test_native_locks_prevent_external_edit_and_directory_replacement(self):
        from agent_execution.workspace_lifecycle.native import PinnedTree
        self.create()
        with PinnedTree(self.root/'example'):
            with self.assertRaises(OSError):(self.root/'example/main.txt').write_text('bad')
            with self.assertRaises(OSError):(self.root/'example').rename(self.root/'replacement')

    @unittest.skipUnless(os.name=='nt','requires Windows native handles')
    def test_purge_deletes_one_character_children_before_root(self):
        self.create()
        (self.root/'example/a').mkdir()
        (self.root/'example/a/b').write_text('nested')
        (self.root/'example/c').write_text('short name')
        plan=self.store.preview('delete_session')
        result=self.store.confirm(plan['plan_id'])
        self.assertEqual(result['state'],'completed',result)
        self.assertFalse((self.root/'example').exists())

    @unittest.skipUnless(os.name=='nt','requires Windows native handles')
    def test_junction_is_rejected_without_touching_outside(self):
        import subprocess
        key=self.create()
        outside=self.home/'outside';outside.mkdir();(outside/'safe').write_text('safe')
        link=self.root/'example/link'
        subprocess.run(['cmd','/c','mklink','/J',str(link),str(outside)],capture_output=True,check=True)
        try:
            with self.assertRaises(PermissionError):self.store.preview('archive',key)
            self.assertEqual((outside/'safe').read_text(),'safe')
        finally:link.rmdir()

    @unittest.skipUnless(os.name=='nt','requires Windows native handles')
    def test_crash_after_move_resumes_same_approved_objects(self):
        from agent_execution.workspace_lifecycle.native import PinnedTree
        from agent_execution.workspace_lifecycle.operations import resume
        key=self.create()
        plan=self.store.preview('archive',key)
        approval=self.approve(plan)
        original=PinnedTree.move
        def crash(tree,destination):
            original(tree,destination)
            raise RuntimeError('simulated crash after atomic move')
        with patch.object(PinnedTree,'move',crash):
            self.assertEqual(self.store.execute(plan['plan_id'],approval)['state'],'blocked')
        result=resume(self.store,plan['plan_id'])
        self.assertEqual(result['state'],'completed',result)
        self.assertEqual((Path(self.store.get(key)['archive'])/'main.txt').read_text(),'hello')

    @unittest.skipUnless(os.name=='nt','requires Windows native handles')
    def test_new_files_in_partial_purge_are_never_deleted(self):
        from agent_execution.workspace_lifecycle.native import PinnedTree
        from agent_execution.workspace_lifecycle.operations import resume
        key=self.create()
        plan=self.store.preview('delete_session')
        approval=self.approve(plan)
        with patch.object(PinnedTree,'purge',side_effect=RuntimeError('simulated partial purge')):
            self.assertEqual(self.store.execute(plan['plan_id'],approval)['state'],'blocked')
        archived=Path(self.store.get(key)['archive'])
        extra=archived/'unapproved.txt';extra.write_text('keep')
        result=resume(self.store,plan['plan_id'])
        self.assertEqual(result['state'],'blocked',result)
        self.assertEqual(extra.read_text(),'keep')

    def test_disabled_mutation_and_foreign_plan_leave_files_unchanged(self):
        key=self.create()
        plan=self.store.preview('archive',key)
        approval=self.approve(plan)
        with patch.dict(os.environ,{'AGENT_WORKSPACE_CLEANUP':'0'}):
            with self.assertRaises(PermissionError):self.store.execute(plan['plan_id'],approval)
        with execution_scope(identity='websocket:bob'):
            with self.assertRaises(PermissionError):self.store.plan(plan['plan_id'])
        self.assertEqual((self.root/'example/main.txt').read_text(),'hello')

    def test_human_dispatcher_leaves_ordinary_quotes_untouched(self):
        from agent_execution.workspace_lifecycle.commands import human_command
        self.assertIsNone(human_command("請建立 user's 程式"))
        with execution_scope(source='communication'):
            with self.assertRaises(PermissionError):human_command('/session-delete preview')

    def test_schedule_snapshot_blocks_live_lease_and_changed_job(self):
        from agent_execution.scheduling.store import SchedulerStore
        from agent_execution.workspace_lifecycle.erasure import schedules,disable_schedules,external_blockers
        from agent_execution.tasks import task_owner
        from agent_execution.policy import current_context
        from permissions import set_current_role
        self.store.ensure_session()
        owner=task_owner(current_context())
        scheduler=SchedulerStore()
        with set_current_role('admin'):
            key=scheduler.add_job('temporary job','0 8 * * *','command',{'command':'echo hello'})
        frozen=schedules([owner])
        self.assertEqual(frozen[0]['id'],key)
        scheduler.acquire_lock(key,'worker')
        self.assertEqual(external_blockers([owner]),['schedule:'+key])
        scheduler.release_lock(key,'worker')
        self.assertEqual(external_blockers([owner]),[])
        with closing(scheduler.connect()) as conn,conn:
            conn.execute('UPDATE scheduled_jobs SET job_payload=? WHERE id=?',('{"command":"changed"}',key))
        with self.assertRaises(ValueError):disable_schedules([owner],frozen)
        self.assertTrue(scheduler.list_jobs()[0]['is_enabled'])
        disable_schedules([owner],schedules([owner]))
        self.assertFalse(scheduler.list_jobs()[0]['is_enabled'])
        from agent_execution.workspace_lifecycle.erasure import erase_schedules
        erase_schedules([owner])
        self.assertEqual(scheduler.list_jobs()[0]['job_payload'],'{}')

    def test_erased_memory_cannot_be_revived_by_late_worker(self):
        from memory import MemoryStore
        from agent_execution.workspace_lifecycle.erasure import erase_memory
        memory=MemoryStore(self.home/'memory.db')
        episode=memory.start_episode()
        event=memory.add_event(episode,'message','user','private content',{'private':'data'})
        memory.add_semantic_memory('user','likes','Go',event)
        erase_memory(memory.db_path)
        memory.finish_episode(episode,summary='late content')
        self.assertEqual(memory.get_episode(episode)['status'],'deleted')
        self.assertEqual(memory.get_episode(episode)['summary'],'')
        self.assertEqual(memory.episode_events(episode)[0]['content'],'')
        with self.assertRaises(PermissionError):memory.add_event(episode,'message','assistant','late')
        with self.assertRaises(PermissionError):memory.add_semantic_memory('user','likes','late',event)
        with closing(memory.connect()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM semantic_memories').fetchone()[0],1)

    @unittest.skipUnless(os.name=='nt','requires Windows native handles')
    def test_deleted_name_can_be_reused_without_reviving_old_registration(self):
        key=self.create()
        plan=self.store.preview('delete_session')
        self.assertEqual(self.store.confirm(plan['plan_id'])['state'],'completed')
        with execution_scope(conversation='new',session='default:device:new'):
            new=self.store.create('example','temporary')
            self.assertNotEqual(new,key)
            self.store.assert_writable(self.root/'example/new.txt')

    def test_broader_workspace_does_not_bypass_owner_guard(self):
        self.create()
        with execution_scope(identity='websocket:bob',workspace=self.home):
            with self.assertRaises(PermissionError):self.store.assert_access(self.root/'example/main.txt')

    def test_service_data_cannot_be_adopted_as_temporary_program(self):
        service=self.root/'service';service.mkdir()
        with patch.dict(os.environ,{'AGENT_TENANT_ROOT':str(service)}):
            with self.assertRaises(PermissionError):self.store.create('service','temporary',adopt=True)

    @unittest.skipUnless(os.name=='nt','requires Windows native handles')
    def test_retained_archive_can_be_attached_and_restored_after_old_session_deletion(self):
        key=self.create()
        archive=self.store.preview('archive',key)
        self.assertEqual(self.store.confirm(archive['plan_id'])['state'],'completed')
        self.store.retain(key)
        deletion=self.store.preview('delete_session')
        self.assertEqual(deletion['items'],[])
        self.assertEqual(self.store.confirm(deletion['plan_id'])['state'],'completed')
        with execution_scope(conversation='new',session='default:device:new'):
            self.store.attach(key)
            restore=self.store.preview('restore',key)
            self.assertEqual(self.store.confirm(restore['plan_id'])['state'],'completed')
            self.assertEqual((self.root/'example/main.txt').read_text(),'hello')
            self.assertEqual(self.store.get(key)['purpose'],'retained')

    @unittest.skipUnless(os.name=='nt','requires Windows native handles')
    def test_session_erasure_scrubs_checkpoints_memory_and_search_cache(self):
        from types import SimpleNamespace
        from memory import MemoryStore
        from agent_execution.code_index import CodeIndex
        from agent_execution.tasks import task_owner
        from agent_execution.policy import current_context
        from agent_execution.workspace_lifecycle.erasure import finalize
        self.create()
        (self.root/'example/source.py').write_text('def private_source(): pass\n')
        index=CodeIndex();index.refresh()
        memory=MemoryStore(self.home/'session-memory.db')
        episode=memory.start_episode();memory.add_event(episode,'message','user','private request')
        owner=task_owner(current_context())
        token=self.store.tasks.claim(owner,'done','private goal',[{'role':'user','content':'private'}],episode)
        self.store.tasks.finish(owner,'done',token,'completed','private reply')
        plan=self.store.preview('delete_session')
        self.assertEqual(self.store.confirm(plan['plan_id'])['state'],'completed')
        runtime=SimpleNamespace(memory=memory,messages=[{'role':'system','content':'system'},{'role':'user','content':'private'}])
        self.assertEqual(finalize(self.store,plan['plan_id'],runtime)['state'],'deleted')
        self.assertEqual(finalize(self.store,plan['plan_id'],runtime)['state'],'deleted')
        self.assertEqual(len(runtime.messages),1)
        self.assertEqual(memory.get_episode(episode)['status'],'deleted')
        with closing(self.store.connect()) as conn:
            row=conn.execute('SELECT goal,checkpoint,reply FROM tasks WHERE owner=?',(owner,)).fetchone()
            self.assertEqual(tuple(row),('','{}',''))
        with closing(index.connect()) as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM ci_files').fetchone()[0],0)
        with self.assertRaises(PermissionError):self.store.tasks.claim(owner,'late','late',[])
        with self.assertRaises(PermissionError):self.store.tasks.checkpoint(owner,'done',token,'late',{'messages':['private late']})
