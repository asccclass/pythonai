import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import sys
from contextlib import closing
from agent_execution.process import run_process

from agent_execution.worktrees import WorktreeStore, commit_files, git, selected_files
from agent_execution.policy import execution_scope,current_context,execute_tool
from agent_execution.tasks import durable_invoke,TaskStore,task_owner,dispatch_task_input
from agent_execution.approvals import ApprovalStore,ApprovalRequired
from permissions import set_current_role
from types import SimpleNamespace as NS


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.repo=self.root/'repo';self.repo.mkdir()
        git(self.repo,'init')
        git(self.repo,'config','user.name','Test')
        git(self.repo,'config','user.email','test@example.invalid')
        (self.repo/'same.txt').write_text('base',encoding='utf-8')
        (self.repo/'other.txt').write_text('other',encoding='utf-8')
        (self.repo/'.gitignore').write_text('ignored.txt\n',encoding='utf-8')
        git(self.repo,'add','same.txt','other.txt','.gitignore')
        git(self.repo,'commit','-m','initial')
        self.store=WorktreeStore(self.repo,self.root/'managed')
        env=patch.dict(os.environ,{'AGENT_TASK_DB':str(self.root/'tasks.db'),
            'AGENT_APPROVAL_DB':str(self.root/'approvals.db'),'AGENT_WORKTREE_ROOT':str(self.root/'managed'),
            'AGENT_ROLLBACK_ROOT':str(self.root/'rollback'),'AGENT_TASK_WORKTREE':'0'})
        env.start();self.addCleanup(env.stop)

    def test_two_tasks_and_dirty_source_are_independent(self):
        (self.repo/'same.txt').write_text('user dirty')
        (self.repo/'private.txt').write_text('user untracked')
        git(self.repo,'add','same.txt')
        original=git(self.repo,'diff','--cached')
        a=self.store.create('owner','task-a');b=self.store.create('owner','task-b')
        self.assertEqual((a/'same.txt').read_text(),'base')
        (a/'same.txt').write_text('agent a');(b/'same.txt').write_text('agent b')
        self.assertEqual((a/'same.txt').read_text(),'agent a')
        self.assertEqual((b/'same.txt').read_text(),'agent b')
        self.assertEqual((self.repo/'same.txt').read_text(),'user dirty')
        self.assertEqual((self.repo/'private.txt').read_text(),'user untracked')
        self.assertEqual(git(self.repo,'diff','--cached'),original)
        self.assertEqual(self.store.create('owner','task-a'),a)
        with self.assertRaises(KeyError):self.store.get('another owner','task-a')

    def test_commit_uses_only_selection_and_preserves_other_staging(self):
        (self.repo/'same.txt').write_text('chosen')
        (self.repo/'other.txt').write_text('user staged')
        git(self.repo,'add','other.txt')
        index=git(self.repo,'show',':other.txt')
        evidence=commit_files(self.repo,'selected change',['same.txt'])
        self.assertEqual(git(self.repo,'show','--format=','--name-only','HEAD').strip(),'same.txt')
        self.assertEqual(git(self.repo,'show','HEAD:other.txt'),'other')
        self.assertEqual(git(self.repo,'show',':other.txt'),index)
        self.assertEqual(git(self.repo,'diff','--cached','--name-only').strip(),'other.txt')
        self.assertEqual(evidence['files'],['same.txt'])

    def test_repository_hook_cannot_add_unselected_files_to_scoped_commit(self):
        hook=self.repo/'.git'/'hooks'/'pre-commit'
        hook.write_text('#!/bin/sh\ngit add other.txt\n',encoding='utf-8')
        hook.chmod(0o755)
        (self.repo/'same.txt').write_text('normal')
        (self.repo/'other.txt').write_text('hook changed')
        git(self.repo,'add','same.txt');git(self.repo,'commit','-m','prove hook runs')
        self.assertEqual(set(git(self.repo,'show','--format=','--name-only','HEAD').splitlines()),{'same.txt','other.txt'})
        (self.repo/'same.txt').write_text('scoped')
        (self.repo/'other.txt').write_text('keep outside commit')
        commit_files(self.repo,'scoped',['same.txt'])
        self.assertEqual(git(self.repo,'show','--format=','--name-only','HEAD').strip(),'same.txt')
        self.assertEqual(git(self.repo,'show','HEAD:other.txt'),'hook changed')

    def test_archive_and_restore_dirty_ignored_deleted_and_commit_history(self):
        path=self.store.create('owner','task')
        (path/'same.txt').write_text('committed')
        commit=commit_files(path,'task commit',['same.txt'])['commit']
        (path/'same.txt').write_text('pending')
        (path/'other.txt').unlink()
        (path/'ignored.txt').write_text('ignored resource')
        (path/'new.txt').write_text('new file')
        archive=self.store.archive('owner','task')
        self.assertFalse(path.exists())
        self.assertTrue((archive/'history.bundle').is_file())
        restored=self.store.restore('owner','task')
        self.assertEqual(restored,path)
        self.assertEqual(git(path,'rev-parse','HEAD').strip(),commit)
        self.assertEqual((path/'same.txt').read_text(),'pending')
        self.assertEqual((path/'ignored.txt').read_text(),'ignored resource')
        self.assertEqual((path/'new.txt').read_text(),'new file')
        self.assertFalse((path/'other.txt').exists())
        self.assertEqual((self.repo/'same.txt').read_text(),'base')

    def test_invalid_selections_cannot_escape_or_stage_everything(self):
        for files in ([],['.'],['../outside'],['.git/config'],['same.txt\x00'],[':(glob)*']):
            with self.subTest(files=files):
                if files==[':(glob)*']:
                    # This is a literal filename, never a Git pathspec.
                    with self.assertRaises(RuntimeError):commit_files(self.repo,'invalid',files)
                else:
                    with self.assertRaises(ValueError):selected_files(self.repo,files)
        self.assertEqual(git(self.repo,'status','--porcelain'),'')

    def test_failed_snapshot_never_removes_workspace(self):
        path=self.store.create('owner','task')
        (path/'same.txt').write_text('keep')
        with patch('agent_execution.worktrees.snapshot_files',side_effect=ValueError('too large')):
            with self.assertRaises(ValueError):self.store.archive('owner','task')
        self.assertEqual((path/'same.txt').read_text(),'keep')
        self.assertEqual(self.store.get('owner','task')['state'],'ready')

    def test_restore_rejects_zip_path_traversal(self):
        self.store.create('owner','task')
        archive=self.store.archive('owner','task')
        manifest=[{'path':'../escaped.txt','sha256':'bad','size':4}]
        with zipfile.ZipFile(archive/'files.zip','w') as zipped:
            zipped.writestr('.pythonai-archive-manifest.json',json.dumps(manifest))
            zipped.writestr('../escaped.txt','evil')
        with self.assertRaises(ValueError):self.store.restore('owner','task')
        self.assertFalse((self.store.storage/'escaped.txt').exists())

    def test_storage_inside_source_is_rejected(self):
        with self.assertRaises(ValueError):WorktreeStore(self.repo,self.repo/'managed')

    def test_existing_unregistered_directory_is_never_overwritten(self):
        path=self.store.location('owner','task')
        path.mkdir();(path/'keep.txt').write_text('user data')
        with self.assertRaises(ValueError):self.store.create('owner','task')
        self.assertEqual((path/'keep.txt').read_text(),'user data')

    def test_invalid_archive_digest_retains_workspace_and_source(self):
        path=self.store.create('owner','task')
        archive=self.store.archive('owner','task')
        manifest=[{'path':'same.txt','sha256':'bad','size':4}]
        with zipfile.ZipFile(archive/'files.zip','w') as zipped:
            zipped.writestr('.pythonai-archive-manifest.json',json.dumps(manifest))
            zipped.writestr('same.txt','evil')
        with self.assertRaises(ValueError):self.store.restore('owner','task')
        self.assertTrue(path.exists())
        self.assertEqual((path/'same.txt').read_text(),'base')
        self.assertEqual((self.repo/'same.txt').read_text(),'base')

    def test_preview_contains_committed_and_pending_diff(self):
        path=self.store.create('owner','task')
        (path/'same.txt').write_text('change')
        commit_files(path,'task change',['same.txt'])
        (path/'other.txt').write_text('pending')
        preview=self.store.preview('owner','task')
        self.assertIn('+change',preview['diff'])
        self.assertIn('+pending',preview['diff'])

    def test_durable_task_scopes_workspace_and_reuses_after_failure(self):
        from base import workspace_root
        saved=[]
        def first(checkpoint):
            path=workspace_root();saved.append(path)
            (path/'same.txt').write_text('agent change')
            raise RuntimeError('crash before model')
        with patch.dict(os.environ,{'AGENT_TASK_WORKTREE':'1'}),execution_scope(workspace=self.repo,task_id='durable'):
            with self.assertRaises(RuntimeError):durable_invoke(first,[{'role':'user','content':'change'}],None)
            with patch.dict(os.environ,{'AGENT_TASK_WORKTREE':'0','AGENT_WORKTREE_ROOT':str(self.root/'changed-storage')}):
                result=durable_invoke(lambda checkpoint:str(workspace_root()),[{'role':'user','content':'change'}],None,resume=True)
            self.assertEqual(result,str(saved[0]))
            self.assertEqual(workspace_root(),self.repo)
        self.assertEqual((saved[0]/'same.txt').read_text(),'agent change')
        self.assertEqual((self.repo/'same.txt').read_text(),'base')

    def test_real_process_exit_preserves_binding_and_workspace(self):
        from base import workspace_root
        with patch.dict(os.environ,{'AGENT_TASK_WORKTREE':'1'}):
            result=run_process([sys.executable,'-m','agent_execution.tests.worktree_fixture',str(self.repo)],capture_output=True,timeout=30)
        self.assertEqual(result.returncode,23,result.stderr)
        with execution_scope(workspace=self.repo,task_id='process-crash'):
            tasks=TaskStore();owner=task_owner(current_context())
            with closing(tasks.connect()) as conn,conn:
                conn.execute('UPDATE tasks SET lease_until=0 WHERE owner=? AND id=?',(owner,'process-crash'))
            original=self.store.get(owner,'process-crash')['path']
            result=durable_invoke(lambda saved:str(workspace_root()),[{'role':'user','content':'crash worktree'}],None,resume=True)
            self.assertEqual(result,original)
            self.assertEqual((Path(result)/'same.txt').read_text(),'durable child bytes')
        self.assertEqual((self.repo/'same.txt').read_text(),'base')

    def test_git_rollback_requires_exact_one_use_approval_and_archives_selection(self):
        from tools.git_rollback import git_rollback
        (self.repo/'same.txt').write_text('discard only after approval')
        (self.repo/'other.txt').write_text('keep user change')
        (self.repo/'untracked.txt').write_text('keep untracked')
        with execution_scope(workspace=self.repo,task_id='rollback'):
            with self.assertRaises(ApprovalRequired) as caught:git_rollback(['same.txt'])
            key=caught.exception.approval_id
            self.assertEqual((self.repo/'same.txt').read_text(),'discard only after approval')
            ApprovalStore().decide(key,'local','local',True)
            with self.assertRaises(PermissionError):git_rollback(['other.txt'],approval_id=key)
            result=execute_tool('git_rollback',{'files':['same.txt'],'approval_id':key},git_rollback)
            self.assertTrue(result.success)
            self.assertEqual((self.repo/'same.txt').read_text(),'base')
            self.assertEqual((self.repo/'other.txt').read_text(),'keep user change')
            self.assertEqual((self.repo/'untracked.txt').read_text(),'keep untracked')
            with zipfile.ZipFile(Path(result.artifacts[0])/'files.zip') as archive:
                self.assertEqual(archive.read('same.txt'),b'discard only after approval')
            with self.assertRaises(PermissionError):git_rollback(['same.txt'],approval_id=key)

    def test_public_git_commit_rejects_broad_staging(self):
        from tools.git_commit import git_commit
        (self.repo/'same.txt').write_text('chosen')
        with execution_scope(workspace=self.repo):
            self.assertFalse(git_commit('no',files=['same.txt'],add_all=True).success)
            result=git_commit('selected',files=['same.txt'])
        self.assertTrue(result.success)
        self.assertEqual(result.evidence['files'],['same.txt'])

    def test_rollback_restores_staged_deletion_and_keeps_untracked_addition(self):
        from agent_execution.worktrees import rollback_files
        git(self.repo,'rm','same.txt')
        (self.repo/'new.txt').write_text('user new file')
        result=rollback_files(self.repo,['same.txt'])
        self.assertEqual((self.repo/'same.txt').read_text(),'base')
        self.assertEqual((self.repo/'new.txt').read_text(),'user new file')
        self.assertIn('deleted file mode',Path(result['archive'],'index.patch').read_text())

    def test_human_archive_rejects_active_task_and_requires_approval(self):
        with execution_scope(workspace=self.repo,task_id='human'):
            tasks=TaskStore();owner=task_owner(current_context())
            token=tasks.claim(owner,'human','goal',[{'role':'user','content':'goal'}])
            self.store.create(owner,'human')
            runtime=NS(messages=[])
            result=dispatch_task_input('/worktree human archive',runtime)
            self.assertIn('Finish or cancel',result.reply)
            tasks.finish(owner,'human',token,'completed','done')
            result=dispatch_task_input('/worktree human archive',runtime)
            self.assertIn('approval_required:',result.reply)
            key=ApprovalStore().pending('local','local')[0]['id']
            ApprovalStore().decide(key,'local','local',True)
            result=dispatch_task_input('/worktree human archive '+key,runtime)
            self.assertNotIn('Error:',result.reply)
            self.assertEqual(self.store.get(owner,'human')['state'],'archived')
            with set_current_role('viewer'):
                result=dispatch_task_input('/worktree human restore',runtime)
                self.assertIn('admin role',result.reply)

    def test_model_evaluator_uses_task_worktree_without_mutating_template(self):
        from task_evaluator import TaskEvaluator
        import server
        from openai.types.chat import ChatCompletionMessage
        evaluator=TaskEvaluator(self.repo)
        roots=[]
        raw=NS(chat=NS(completions=NS(create=lambda **kwargs:NS(choices=[NS(message=ChatCompletionMessage(role='assistant',content='done'))]))))
        original=TaskEvaluator.capture_state
        def capture(value,*args,**kwargs):roots.append(value.workspace_root);return original(value,*args,**kwargs)
        with patch.dict(os.environ,{'AGENT_TASK_WORKTREE':'1'}),execution_scope(workspace=self.repo,task_id='eval'),patch('server.get_client',return_value=raw),patch.object(TaskEvaluator,'capture_state',capture):
            durable_invoke(lambda saved:server.run_agent([{'role':'user','content':'describe'}],task_evaluator=evaluator),[{'role':'user','content':'describe'}],None)
        self.assertTrue(roots)
        self.assertTrue(all(root!=self.repo for root in roots))
        self.assertEqual(evaluator.workspace_root,self.repo)

    def test_skill_acceptance_checks_its_worktree_and_keeps_source_unchanged(self):
        from base import workspace_root
        from task_evaluator import TaskEvaluator
        from agent_runtime import verify_skill_result
        config=self.repo/'.agent';config.mkdir()
        (config/'acceptance.json').write_text(json.dumps({'version':1,'checks':[{'kind':'file_contains','path':'document.txt','text':'skill result'}]}))
        evaluator=TaskEvaluator(self.repo)
        runtime=NS(task_evaluator=evaluator,memory=None,event_callback=None)
        messages=[{'role':'user','content':'write documentation'}]
        def skill(saved):
            (workspace_root()/'document.txt').write_text('skill result')
            return {'skill_name':'fixture','success':True,'steps':[]}
        with patch.dict(os.environ,{'AGENT_TASK_WORKTREE':'1'}),execution_scope(workspace=self.repo,task_id='skill-check'):
            result=durable_invoke(skill,messages,None,skill=True,
                verify_skill=lambda value:verify_skill_result(value,runtime,messages,None))
        self.assertTrue(result['verification']['verified'],result)
        self.assertEqual(evaluator.workspace_root,self.repo)
        self.assertFalse((self.repo/'document.txt').exists())


if __name__=='__main__':unittest.main()
