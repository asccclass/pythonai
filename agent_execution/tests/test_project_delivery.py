from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit

from agent_execution import execution_scope
from agent_execution.approvals import ApprovalStore,ApprovalRequired
from agent_execution.patches import ArtifactStore,digest,prepare_patch,apply_patch
from agent_execution.git_delivery import prepare_delivery,inspect_delivery,reconcile_delivery,human_delivery_command
from agent_execution.git_provider import GitProvider,ProviderError,configuration
from agent_execution.worktrees import git
from tools.project_delivery import project_delivery
from tools.prepare_project_patch import prepare_project_patch
from permissions import set_current_role


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'workspace';self.root.mkdir()
        environment=patch.dict(os.environ,{'AGENT_TASK_DB':str(Path(self.temp.name)/'tasks.db')})
        environment.start();self.addCleanup(environment.stop)
        scope=execution_scope(workspace=self.root,task_id='patch',session='patch-test');scope.__enter__();self.addCleanup(lambda:scope.__exit__(None,None,None))
        (self.root/'a.txt').write_bytes('原始\n'.encode());(self.root/'b.txt').write_text('delete',encoding='utf-8')

    def changes(self):
        return [{'path':'a.txt','before_sha256':digest((self.root/'a.txt').read_bytes()),'content':'更新\n'},
            {'path':'b.txt','before_sha256':digest(b'delete'),'content':None},
            {'path':'new/c.txt','before_sha256':None,'content':'新增\n'}]

    def test_prepare_preview_and_apply_exact_utf8_changes_are_idempotent(self):
        proposal=prepare_patch(self.changes());self.assertIn('更新',proposal['diff'])
        self.assertEqual((self.root/'a.txt').read_text(encoding='utf-8'),'原始\n')
        result=apply_patch(proposal['patch_id']);self.assertEqual(result['state'],'applied')
        self.assertFalse((self.root/'b.txt').exists());self.assertEqual((self.root/'new/c.txt').read_text(encoding='utf-8'),'新增\n')
        (self.root/'a.txt').write_text('later user change')
        self.assertTrue(apply_patch(proposal['patch_id'])['reused'])
        self.assertEqual((self.root/'a.txt').read_text(),'later user change')

    def test_stale_second_file_blocks_every_file_before_changes(self):
        proposal=prepare_patch(self.changes());(self.root/'b.txt').write_text('user changed')
        with self.assertRaises(ValueError):apply_patch(proposal['patch_id'])
        self.assertEqual((self.root/'a.txt').read_text(encoding='utf-8'),'原始\n')
        self.assertFalse((self.root/'new/c.txt').exists())

    def test_empty_file_creation_and_deletion_have_reviewable_preview(self):
        value=prepare_patch([{'path':'empty.txt','before_sha256':None,'content':''}])
        self.assertIn('new file',value['diff']);self.assertIn('empty.txt',value['diff'])
        apply_patch(value['patch_id'])
        value=prepare_patch([{'path':'empty.txt','before_sha256':digest(b''),'content':None}])
        self.assertIn('deleted file',value['diff']);apply_patch(value['patch_id'])
        self.assertFalse((self.root/'empty.txt').exists())

    def test_failed_partial_patch_rolls_back_and_requires_inspection(self):
        proposal=prepare_patch(self.changes())
        from agent_execution.patches import replace_bytes
        def fail_second(path,*args):
            if path.name=='b.txt':raise OSError('fixture')
            return replace_bytes(path,*args)
        with patch('agent_execution.patches.replace_bytes',side_effect=fail_second),self.assertRaises(OSError):apply_patch(proposal['patch_id'])
        self.assertEqual((self.root/'a.txt').read_text(encoding='utf-8'),'原始\n')
        self.assertEqual(ArtifactStore().get(proposal['patch_id'])['state'],'needs_resolution')
        with self.assertRaises(ValueError):apply_patch(proposal['patch_id'])

    def test_compensation_preserves_concurrent_user_change(self):
        proposal=prepare_patch(self.changes())
        from agent_execution.patches import replace_bytes
        def interrupt(path,*args):
            if path.name=='b.txt':
                (self.root/'a.txt').write_text('user edit during apply');raise OSError('fixture')
            return replace_bytes(path,*args)
        with patch('agent_execution.patches.replace_bytes',side_effect=interrupt),self.assertRaises(OSError):apply_patch(proposal['patch_id'])
        self.assertEqual((self.root/'a.txt').read_text(),'user edit during apply')

    def test_owner_task_and_traversal_are_enforced(self):
        proposal=prepare_patch(self.changes())
        with execution_scope(identity='another'),self.assertRaises(KeyError):apply_patch(proposal['patch_id'])
        with execution_scope(task_id='another'),self.assertRaises(ValueError):apply_patch(proposal['patch_id'])
        for name in ('../outside','.git/config','.GIT/config','.git./config','file.txt:stream'):
            with self.subTest(name=name),self.assertRaises((ValueError,PermissionError)):prepare_project_patch([{'path':name,'before_sha256':None,'content':'no'}])
        with set_current_role('viewer'),self.assertRaises(PermissionError):prepare_project_patch(self.changes())

    def test_interrupted_apply_is_not_replayed_after_store_restart(self):
        proposal=prepare_patch(self.changes());store=ArtifactStore();row=store.get(proposal['patch_id']);store.update(row,'applying')
        with self.assertRaises(ValueError):apply_patch(proposal['patch_id'])
        self.assertEqual((self.root/'a.txt').read_text(encoding='utf-8'),'原始\n')
        with self.assertRaises(ValueError):human_delivery_command('/delivery '+proposal['patch_id']+' abandon checked')
        with closing(store.connect()) as conn,conn:conn.execute('UPDATE delivery_artifacts SET updated=0 WHERE id=?',(proposal['patch_id'],))
        self.assertIn('abandoned',human_delivery_command('/delivery '+proposal['patch_id']+' abandon checked files'))

    def test_real_junction_is_rejected_and_outside_bytes_retained(self):
        outside=Path(self.temp.name)/'outside';outside.mkdir();(outside/'file.txt').write_text('private')
        link=self.root/'linked'
        if os.name=='nt':
            subprocess.run(['powershell','-NoProfile','-Command',"New-Item -ItemType Junction -Path '"+str(link)+"' -Target '"+str(outside)+"' | Out-Null"],check=True,capture_output=True)
        else:link.symlink_to(outside,target_is_directory=True)
        self.addCleanup(lambda:link.rmdir() if os.name=='nt' else link.unlink())
        with self.assertRaises((ValueError,PermissionError)):prepare_project_patch([{'path':'linked/file.txt','before_sha256':digest(b'private'),'content':'no'}])
        self.assertEqual((outside/'file.txt').read_text(),'private')

    def test_real_process_crash_preserves_partial_patch_and_prevents_replay(self):
        proposal=prepare_patch(self.changes())
        code="""import os,sys
from pathlib import Path
from agent_execution import execution_scope
import agent_execution.patches as patches
original=patches.replace_bytes
def crash(*args):
 original(*args)
 os._exit(42)
patches.replace_bytes=crash
with execution_scope(workspace=Path(sys.argv[1]),task_id='patch',session='patch-test'):
 patches.apply_patch(sys.argv[2])
"""
        from agent_execution.process import run_process
        result=run_process([sys.executable,'-c',code,str(self.root),proposal['patch_id']],cwd=Path(__file__).resolve().parents[2],timeout=15)
        self.assertEqual(result.returncode,42,result.stderr)
        self.assertEqual((self.root/'a.txt').read_text(encoding='utf-8'),'更新\n')
        self.assertEqual(ArtifactStore().get(proposal['patch_id'])['state'],'applying')
        with self.assertRaises(ValueError):apply_patch(proposal['patch_id'])
        self.assertEqual((self.root/'b.txt').read_text(),'delete')


class ProviderFixture:
    def __init__(self,bare,base,kind='github'):
        self.bare=bare;self.base=base;self.kind=kind;self.branch='codex/feature';self.exists=False;self.merged=False
        self.ci=True;self.approved=True;self.sha_override=None;self.check_sha_override=None;self.comments=[];self.statuses=[];self.reset_approvals=True
        self.calls=[];self.bad_response=None;self.fail_create=False;self.fail_merge=False;self.after_create_failure=False;self.trickle=False
        state=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):self.handle_request()
            def do_POST(self):self.handle_request()
            def do_PATCH(self):self.handle_request()
            def do_PUT(self):self.handle_request()
            def handle_request(self):
                body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0')))) if self.headers.get('Content-Length') else None
                path=urlsplit(self.path).path;state.calls.append((self.command,self.path,body,dict(self.headers)))
                if state.trickle:
                    self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
                    try:
                        for chunk in (b'{',*[b' ']*40,b'}'):
                            self.wfile.write(chunk);self.wfile.flush();time.sleep(0.03)
                    except OSError:pass
                    return
                if state.bad_response:
                    code,mime,data=state.bad_response;self.send_response(code);self.send_header('Content-Type',mime);self.end_headers();self.wfile.write(data);return
                try:value,code=state.route(self.command,path,body)
                except Exception:value,code={'error':'fixture protocol mismatch'},500
                data=json.dumps(value).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    @property
    def url(self):return 'http://127.0.0.1:'+str(self.server.server_port)
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join()
    def sha(self):
        if self.sha_override:return self.sha_override
        path=self.bare/'refs/heads'/self.branch
        return path.read_text().strip() if path.exists() else self.base

    def pull(self):
        sha=self.sha()
        if self.kind=='github':return {'number':1,'head':{'sha':sha,'ref':self.branch,'repo':{'full_name':'test/project'}},'base':{'sha':self.base,'ref':'main','repo':{'full_name':'test/project'}},'html_url':self.url+'/pull/1','state':'closed' if self.merged else 'open','draft':False,'merged':self.merged}
        return {'iid':1,'sha':sha,'source_branch':self.branch,'target_branch':'main','source_project_id':1,'target_project_id':1,'diff_refs':{'base_sha':self.base},'web_url':self.url+'/merge_requests/1','state':'merged' if self.merged else 'opened','draft':False,'detailed_merge_status':'mergeable'}

    def route(self,method,path,body):
        github=self.kind=='github';prefix='/repos/test/project' if github else '/projects/test%2Fproject'
        pulls=prefix+('/pulls' if github else '/merge_requests')
        if path==pulls:
            if method=='GET':return ([{'number':1} if github else {'iid':1}] if self.exists else []),200
            if method=='POST':
                if self.fail_create:return {'error':'failed create'},500
                self.exists=True;self.branch=body['head' if github else 'source_branch']
                if self.after_create_failure:return {'error':'unknown outcome'},500
                return ({'number':1} if github else {'iid':1}),201
        if path==pulls+'/1':return self.pull(),200
        if path==pulls+'/1/merge':
            if self.fail_merge:return {'error':'rejected'},409
            self.merged=True
            return ({'merged':True,'sha':'a'*40} if github else {'state':'merged','merge_commit_sha':'a'*40}),200
        if github:
            if path.endswith('/check-runs'):return {'total_count':1,'check_runs':[{'id':1,'name':'unit','head_sha':self.check_sha_override or self.sha(),'status':'completed','conclusion':'success' if self.ci else 'failure'}]},200
            if path.endswith('/statuses'):return self.statuses,200
            if path.endswith('/reviews'):return ([{'id':10,'user':{'login':'reviewer'},'state':'APPROVED','commit_id':self.sha(),'submitted_at':'2030-01-01T00:00:00Z'}] if self.approved else [{'id':11,'user':{'login':'reviewer'},'state':'CHANGES_REQUESTED','commit_id':self.sha(),'submitted_at':'2030-01-01T00:00:00Z'}]),200
            if path.endswith('/comments'):return self.comments,200
        else:
            if path==prefix+'/approvals':return {'reset_approvals_on_push':self.reset_approvals},200
            if path.endswith('/pipelines'):return [{'id':90,'sha':self.sha(),'status':'success' if self.ci else 'failed'}],200
            if path.endswith('/jobs'):return [{'id':1,'name':'unit','status':'success' if self.ci else 'failed'}],200
            if path.endswith('/approvals'):return {'approvals_left':0 if self.approved else 1,'approved_by':[{'user':{'username':'reviewer'}}] if self.approved else []},200
            if path.endswith('/discussions'):return self.comments,200
        return {'error':'unexpected fixture endpoint'},404


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.repo=self.root/'repo';self.repo.mkdir();self.bare=self.root/'remote.git'
        git(self.repo,'init');git(self.repo,'config','user.name','Test');git(self.repo,'config','user.email','test@example.invalid')
        git(self.repo,'init','--bare',str(self.bare));git(self.repo,'remote','add','origin',str(self.bare))
        (self.repo/'README.md').write_text('old');(self.repo/'.agent').mkdir()
        (self.repo/'.gitignore').write_text('__pycache__/\n.pytest_cache/\n')
        self.criteria={'version':1,'checks':[{'kind':'file_contains','path':'README.md','text':'new'}]}
        (self.repo/'.agent/acceptance.json').write_text(json.dumps(self.criteria))
        git(self.repo,'add','.');git(self.repo,'commit','-m','base');self.base=git(self.repo,'rev-parse','HEAD').strip()
        git(self.repo,'push','origin','HEAD:refs/heads/main')
        (self.repo/'README.md').write_text('new');git(self.repo,'add','README.md');git(self.repo,'commit','-m','feature')
        self.head=git(self.repo,'rev-parse','HEAD').strip()
        self.fixture=ProviderFixture(self.bare,self.base);self.addCleanup(self.fixture.close)
        self.config={'kind':'github','api_url':self.fixture.url,'allow_http_loopback':True,'repository':'test/project',
            'remote_url':str(self.bare),'base':'main','base_ref':self.base,'required_ci':['check:unit'],'required_reviewers':['reviewer'],'token_env':'TEST_GIT_TOKEN'}
        self.environment=patch.dict(os.environ,{'AGENT_TASK_DB':str(self.root/'tasks.db'),'AGENT_APPROVAL_DB':str(self.root/'approvals.db'),
            'AGENT_GIT_PROVIDER_CONFIG':json.dumps(self.config),'TEST_GIT_TOKEN':'fixture-secret-not-real','AGENT_ACCEPTANCE_FILE':'.agent/acceptance.json'})
        self.environment.start();self.addCleanup(self.environment.stop)
        scope=execution_scope(workspace=self.repo,task_id='delivery',session='delivery-test');scope.__enter__();self.addCleanup(lambda:scope.__exit__(None,None,None))

    def plan(self,previous_id=''):
        return prepare_delivery(['README.md'],'codex/feature','Feature','Concrete change and validation',previous_id)['plan_id']

    def approved(self,action,key):
        with self.assertRaises(ApprovalRequired) as raised:project_delivery(action,plan_id=key)
        approval=raised.exception.approval_id
        ApprovalStore().decide(approval,'local','delivery-test',True)
        return approval

    def publish(self,key):return project_delivery('publish',plan_id=key,approval_id=self.approved('publish',key)).evidence

    def test_github_native_publish_head_bound_ci_review_and_approved_merge(self):
        key=self.plan();self.assertEqual(self.fixture.calls,[])
        published=self.publish(key);self.assertFalse(published['verified_delivery']);self.assertEqual(self.fixture.sha(),self.head)
        observed=inspect_delivery(key);self.assertTrue(observed['ready']);self.assertFalse(observed['verified_delivery'])
        merged=project_delivery('merge',plan_id=key,approval_id=self.approved('merge',key)).evidence
        self.assertTrue(merged['verified_delivery']);self.assertEqual(merged['head'],self.head)
        self.assertEqual((self.bare/'refs/heads/main').read_text().strip(),self.base)
        merge=[call for call in self.fixture.calls if call[1].endswith('/merge')][0]
        self.assertEqual(merge[2]['sha'],self.head)
        self.assertEqual(ArtifactStore().get(key)['state'],'merged')
        self.assertNotIn('fixture-secret',json.dumps(ArtifactStore().get(key)))

    def test_ci_failure_missing_or_stale_checks_cannot_merge(self):
        key=self.plan();self.publish(key);self.fixture.ci=False
        self.assertFalse(inspect_delivery(key)['ready'])
        with self.assertRaises(ValueError):project_delivery('merge',plan_id=key)
        self.fixture.ci=True;self.fixture.check_sha_override='f'*40
        self.assertFalse(inspect_delivery(key)['ci_passed'])
        self.fixture.check_sha_override=None;self.fixture.sha_override='e'*40
        self.assertEqual(inspect_delivery(key)['state'],'stale')
        self.assertFalse(any(call[1].endswith('/merge') for call in self.fixture.calls))

    def test_merge_rechecks_ci_after_approval(self):
        key=self.plan();self.publish(key);self.assertTrue(inspect_delivery(key)['ready']);approval=self.approved('merge',key)
        self.fixture.ci=False
        with self.assertRaises(ValueError):project_delivery('merge',plan_id=key,approval_id=approval)
        self.assertFalse(any(call[1].endswith('/merge') for call in self.fixture.calls))
        self.assertEqual(ArtifactStore().get(key)['state'],'ci_pending_or_failed')

    def test_review_feedback_repair_keeps_pull_and_frozen_acceptance(self):
        key=self.plan();self.publish(key);self.fixture.approved=False
        self.fixture.comments=[{'id':5,'path':'README.md','line':1,'body':'Reproduction: read the first line; expected detail','commit_id':self.head,'updated_at':'2020-01-01T00:00:00Z','user':{'login':'reviewer'}}]
        observation=inspect_delivery(key);self.assertEqual(observation['state'],'review_pending')
        self.assertEqual(observation['findings'][0]['line'],1);self.assertIn('Reproduction',observation['findings'][0]['reproduction'])
        (self.repo/'README.md').write_text('new fixed');git(self.repo,'add','README.md');git(self.repo,'commit','-m','fix finding')
        # Editing the acceptance file after the first plan cannot weaken old checks.
        (self.repo/'.agent/acceptance.json').write_text(json.dumps({'version':1,'checks':[]}));git(self.repo,'add','.agent/acceptance.json');git(self.repo,'commit','-m','weaken criteria')
        with self.assertRaises(ValueError):self.plan(previous_id=key)
        git(self.repo,'restore','--source='+self.head,'--staged','--worktree','.agent/acceptance.json');git(self.repo,'commit','-m','restore contract')
        repair=prepare_delivery(['README.md','.agent/acceptance.json'],'codex/feature','Fix','feedback',key)['plan_id']
        self.assertEqual(ArtifactStore().get(repair)['payload']['feedback'][0]['id'],'5')
        self.publish(repair);self.fixture.approved=True;self.assertTrue(inspect_delivery(repair)['ready'])
        self.assertEqual(ArtifactStore().get(repair)['payload']['pull'],1)
        self.assertEqual(len([call for call in self.fixture.calls if call[0]=='POST']),1)
        self.assertTrue(any(call[0]=='PATCH' for call in self.fixture.calls))

    def test_approval_arguments_identity_and_one_use_are_enforced(self):
        key=self.plan();approval=self.approved('publish',key)
        with execution_scope(identity='someone'),self.assertRaises((KeyError,PermissionError)):project_delivery('publish',plan_id=key,approval_id=approval)
        with self.assertRaises(PermissionError):project_delivery('publish',plan_id=key,title='changed',approval_id=approval)
        self.assertFalse((self.bare/'refs/heads/codex/feature').exists())
        project_delivery('publish',plan_id=key,approval_id=approval)
        with self.assertRaises((PermissionError,ValueError)):project_delivery('publish',plan_id=key,approval_id=approval)

    def test_publication_response_lost_is_reconciled_read_only_without_duplicate_pr(self):
        key=self.plan();self.fixture.after_create_failure=True
        with self.assertRaises(ProviderError):self.publish(key)
        self.assertEqual(ArtifactStore().get(key)['state'],'needs_reconciliation')
        with self.assertRaises(ValueError):project_delivery('publish',plan_id=key)
        posts=len([call for call in self.fixture.calls if call[0]=='POST'])
        result=reconcile_delivery(key);self.assertEqual(result['state'],'published')
        self.assertEqual(len([call for call in self.fixture.calls if call[0]=='POST']),posts)

    def test_remote_branch_changes_are_not_overwritten(self):
        key=self.plan();git(self.repo,'push','origin',self.base+':refs/heads/codex/feature')
        with self.assertRaises(ValueError):self.publish(key)
        self.assertEqual(self.fixture.sha(),self.base);self.assertFalse(self.fixture.exists)

    def test_uncertain_merge_is_not_reported_verified_by_reconciliation(self):
        key=self.plan();self.publish(key);self.assertTrue(inspect_delivery(key)['ready'])
        row=ArtifactStore().get(key);ArtifactStore().update(row,'merging');self.fixture.merged=True
        result=reconcile_delivery(key);self.assertEqual(result['state'],'merged_unverified');self.assertFalse(result['verified_delivery'])

    def test_http_html_auth_errors_and_redirects_do_not_leak_secrets(self):
        provider=GitProvider(configuration())
        for response in ((200,'text/html',b'login'),(401,'application/json',b'{"token":"fixture-secret-not-real"}'),(302,'text/html',b'redirect')):
            self.fixture.bad_response=response
            with self.subTest(code=response[0]),self.assertRaises(ProviderError) as raised:provider.request('GET','/repos/test/project/pulls/1')
            self.assertNotIn('fixture-secret',str(raised.exception))

    def test_trickling_http_response_cannot_exceed_read_deadline(self):
        provider=GitProvider(configuration());provider.until=time.monotonic()+0.2
        self.fixture.trickle=True;started=time.monotonic()
        with self.assertRaises(ProviderError):provider.request('GET','/repos/test/project/pulls/1')
        self.assertLess(time.monotonic()-started,0.8)

    def test_github_latest_status_and_review_are_required(self):
        self.config['required_ci'].append('status:lint');os.environ['AGENT_GIT_PROVIDER_CONFIG']=json.dumps(self.config)
        key=self.plan();self.publish(key);self.assertFalse(inspect_delivery(key)['ready'])
        self.fixture.statuses=[{'id':1,'context':'lint','state':'success'},{'id':2,'context':'lint','state':'failure'}]
        self.assertFalse(inspect_delivery(key)['ready'])
        self.fixture.statuses[1]['state']='success';self.assertTrue(inspect_delivery(key)['ready'])

    def test_gitlab_full_protocol_with_jobs_approvals_and_resolved_discussion(self):
        self.fixture.kind='gitlab';self.config.update(kind='gitlab',required_ci=['job:unit'])
        os.environ['AGENT_GIT_PROVIDER_CONFIG']=json.dumps(self.config)
        key=self.plan();self.publish(key)
        self.fixture.comments=[{'notes':[{'id':10,'type':'DiffNote','system':False,'body':'Reproduction: check README first line',
            'position':{'new_path':'README.md','new_line':1,'head_sha':self.head},'resolved':False}]}]
        self.assertFalse(inspect_delivery(key)['ready']);self.fixture.comments[0]['notes'][0]['resolved']=True
        self.assertTrue(inspect_delivery(key)['ready'])
        self.fixture.reset_approvals=False;self.assertFalse(inspect_delivery(key)['review_passed'])
        self.fixture.reset_approvals=True;self.assertTrue(inspect_delivery(key)['ready'])
        result=project_delivery('merge',plan_id=key,approval_id=self.approved('merge',key)).evidence;self.assertTrue(result['verified_delivery'])
        self.assertTrue(any({name.lower():value for name,value in call[3].items()}.get('private-token')=='fixture-secret-not-real' for call in self.fixture.calls))

    def test_code_delivery_requires_real_passing_tests(self):
        (self.repo/'helper.py').write_text('def answer(): return 1\n');(self.repo/'tests').mkdir()
        (self.repo/'tests/test_helper.py').write_text("import runpy\nfrom pathlib import Path\ndef test_answer():\n assert runpy.run_path(str(Path(__file__).parents[1]/'helper.py'))['answer']()==1\n")
        git(self.repo,'add','helper.py','tests/test_helper.py');git(self.repo,'commit','-m','code')
        files=['README.md','helper.py','tests/test_helper.py']
        with self.assertRaises(ValueError):prepare_delivery(files,'codex/feature','Code','validation')
        self.criteria['checks'].append({'kind':'tests','target':'tests','min_tests':1})
        (self.repo/'.agent/acceptance.json').write_text(json.dumps(self.criteria));git(self.repo,'add','.agent/acceptance.json');git(self.repo,'commit','-m','test contract')
        files.append('.agent/acceptance.json')
        value=prepare_delivery(files,'codex/feature','Code','validation')
        tests=[item for item in value['local_acceptance']['evidence'] if item.get('evidence',{}).get('tests')]
        self.assertEqual(tests[0]['exit_code'],0);self.assertGreaterEqual(tests[0]['evidence']['tests']['passed'],1)

    def test_repair_cannot_weaken_original_checks_even_with_selected_contract(self):
        key=self.plan();self.publish(key)
        (self.repo/'README.md').write_text('wrong')
        (self.repo/'.agent/acceptance.json').write_text(json.dumps({'version':1,'checks':[{'kind':'file_exists','path':'README.md'}]}))
        git(self.repo,'add','README.md','.agent/acceptance.json');git(self.repo,'commit','-m','attempt weakened repair')
        with self.assertRaises(ValueError):prepare_delivery(['README.md','.agent/acceptance.json'],'codex/feature','Fix','feedback',key)

    def test_provider_configuration_change_invalidates_prepared_plan(self):
        key=self.plan();approval=self.approved('publish',key)
        self.config['required_ci']=['check:different'];os.environ['AGENT_GIT_PROVIDER_CONFIG']=json.dumps(self.config)
        with self.assertRaises(ValueError):project_delivery('publish',plan_id=key,approval_id=approval)
        self.assertFalse((self.bare/'refs/heads/codex/feature').exists())

    def test_transient_unselected_files_in_history_cannot_be_pushed(self):
        (self.repo/'transient.txt').write_text('removed from final tree')
        git(self.repo,'add','transient.txt');git(self.repo,'commit','-m','temporary file')
        git(self.repo,'rm','transient.txt');git(self.repo,'commit','-m','remove temporary file')
        with self.assertRaises(ValueError):self.plan()
        value=prepare_delivery(['README.md','transient.txt'],'codex/feature','Feature','explicit historical scope')
        self.assertIn('transient.txt',value['files']);self.assertEqual(self.fixture.calls,[])
        self.assertIn('removed from final tree',value['history_diff'])

    def test_uncertain_merge_with_changed_head_stays_unreconciled(self):
        key=self.plan();self.publish(key)
        row=ArtifactStore().get(key);ArtifactStore().update(row,'merging')
        self.fixture.merged=True;self.fixture.sha_override='e'*40
        with self.assertRaises(ProviderError):reconcile_delivery(key)
        self.assertEqual(ArtifactStore().get(key)['state'],'merging')


class ProviderBoundsTests(unittest.TestCase):
    def test_pagination_limit_and_incomplete_check_count_fail_closed(self):
        provider=GitProvider({'kind':'github','repository':'test/project'})
        with patch.object(provider,'request',return_value=[{}]*100) as request,self.assertRaises(ProviderError):provider.pages('/items')
        self.assertEqual(request.call_count,10)
        with patch.object(provider,'request',return_value={'check_runs':[{}],'total_count':2}),self.assertRaises(ProviderError):provider.pages('/checks','check_runs')


if __name__=='__main__':unittest.main()
