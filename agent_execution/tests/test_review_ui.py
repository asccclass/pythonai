from contextlib import closing
from http.server import ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from agent_execution.api import authenticated_api
from agent_execution.ide.server import IDERequestHandler
from agent_execution.policy import execution_scope, current_context
from agent_execution.tasks import TaskStore, task_owner
from agent_execution.patches import ArtifactStore
from agent_execution.approvals import ApprovalStore


class Stream:
    def __init__(self, error=False): self.closed=False; self.error=error
    def __iter__(self):
        yield NS(choices=[NS(delta=NS(content='新資料🙂'))],usage=None)
        if self.error: raise RuntimeError('SECRET_PROVIDER_ERROR')
    def close(self): self.closed=True


class ReviewUITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); roles=self.root/'roles.json'
        roles.write_text(json.dumps({'api:alice':'admin','api:bob':'viewer'}))
        self.env=patch.dict(os.environ,{'AGENT_TASK_DB':str(self.root/'tasks.db'),
            'AGENT_APPROVAL_DB':str(self.root/'approvals.db'),'AGENT_WORKSPACE_ROOT':str(self.root),
            'AGENT_PROJECT_WORKSPACES':'{}','ROLES_CONFIG':str(roles),
            'AGENT_API_TOKENS':json.dumps({'a'*32:'alice','b'*32:'bob'}),
            'AGENT_TASK_MAX_COST_USD':'0','AGENT_MODEL_ROUTING':'{}','AGENT_MODEL_CAPABILITIES':'{}'})
        self.env.start();self.addCleanup(self.env.stop)
        self.headers={'Authorization':'Bearer '+'a'*32,'X-Agent-Project':'default','X-Agent-Session':'dashboard'}
        self.store=TaskStore()
        with execution_scope(source='api',identity='api:alice',project='default',session='default:dashboard',task_id='task'):
            self.owner=task_owner(current_context());lease=self.store.claim(self.owner,'task','review goal',[])
            self.store.checkpoint(self.owner,'task',lease,'tool_done',{'messages':[],'plan':'計畫🙂','evaluation':{'verified':False}},
                step={'id':'call','name':'read_file','arguments':{'path':'x.py'},'state':'done','result':'original'})
            self.artifact=ArtifactStore().add('patch',{'diff':'--- old\n+++ new\n<script>bad()</script>',
                'provider':{'secret':'DO_NOT_PROJECT'},'head':'abc'})
            self.approval=ApprovalStore().request('api:alice','default:dashboard','apply_patch',{'patch_id':self.artifact})

    def api(self,path,method='GET',headers=None):
        return authenticated_api(path,method,b'',self.headers if headers is None else headers)

    def test_detail_projects_plan_events_acceptance_diff_without_provider(self):
        code,value=self.api('/api/review/tasks/task');self.assertEqual(code,200)
        self.assertEqual(value['plan']['text'],'計畫🙂');self.assertEqual(value['events'][0]['name'],'read_file')
        self.assertIn('<script>',value['artifacts'][0]['diff']['text']);self.assertNotIn('DO_NOT_PROJECT',str(value))
        self.assertEqual(value['approvals'][0]['id'],self.approval)
        self.assertEqual(value['task']['verification'],'unverified')

    def test_auth_identity_project_and_session_isolation(self):
        self.assertEqual(self.api('/api/review/tasks',headers={})[0],401)
        for changes in [{'Authorization':'Bearer '+'b'*32},{'X-Agent-Project':'other'},{'X-Agent-Session':'other'}]:
            headers={**self.headers,**changes}
            self.assertEqual(self.api('/api/review/tasks',headers=headers),(200,[]))
            self.assertEqual(self.api('/api/review/tasks/task',headers=headers)[0],404)
        self.assertEqual(self.api('/api/review/context')[1]['role'],'admin')

    def test_cancel_fences_lease_and_cannot_cancel_other_owner(self):
        self.assertEqual(self.api('/api/review/tasks/task/cancel','POST', {**self.headers,'Authorization':'Bearer '+'b'*32})[0],403)
        self.assertEqual(self.api('/api/review/tasks/task/cancel','POST')[1],{'cancelled':True})
        self.assertEqual(self.store.get(self.owner,'task')['state'],'cancelled')
        self.assertEqual(self.api('/api/review/tasks/task/cancel','POST')[1],{'cancelled':False})

    def test_unknown_actions_do_not_apply_or_publish_artifacts(self):
        for action in ['apply','merge','publish','undo']:
            self.assertEqual(self.api('/api/review/tasks/task/'+action,'POST')[0],404)
        self.assertEqual(self.api('/api/review/tasks/task')[1]['artifacts'][0]['state'],'prepared')

    def test_projection_truncates_diff_and_tool_events(self):
        with closing(self.store.connect()) as conn,conn:
            conn.execute('UPDATE delivery_artifacts SET payload=? WHERE id=?',(json.dumps({'diff':'x'*70000}),self.artifact))
            for i in range(105):conn.execute('INSERT INTO task_steps VALUES(?,?,?,?,?,?,?,?)',
                (self.owner,'task',str(i),'tool','{}','[]','done','x'*70000 if i==0 else 'ok'))
        value=self.api('/api/review/tasks/task')[1]
        self.assertEqual(len(value['events']),100);self.assertTrue(value['events_truncated'])
        self.assertTrue(value['artifacts'][0]['diff']['truncated']);self.assertEqual(len(value['artifacts'][0]['diff']['text']),65536)

    def server(self):
        httpd=ThreadingHTTPServer(('127.0.0.1',0),IDERequestHandler)
        thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
        self.addCleanup(httpd.server_close);self.addCleanup(httpd.shutdown)
        return httpd

    def post(self,httpd,payload,headers=None,path='/api/inline_chat'):
        conn=http.client.HTTPConnection('127.0.0.1',httpd.server_port,timeout=15)
        try:
            conn.request('POST',path,json.dumps(payload),{'Content-Type':'application/json',**(self.headers if headers is None else headers)})
            res=conn.getresponse();return res.status,res.read().decode('utf-8')
        finally:conn.close()

    def test_ide_auth_and_workspace_boundary_precede_provider(self):
        httpd=self.server()
        with patch('server.get_client') as client:
            self.assertEqual(self.post(httpd,{},headers={})[0],401)
            self.assertEqual(self.post(httpd,{},headers={'Authorization':'Bearer '+'b'*32})[0],403)
            outside=str(self.root.parent/'outside.py')
            self.assertEqual(self.post(httpd,{'file_path':outside})[0],403)
            self.assertEqual(self.post(httpd,{'file_path':str(self.root/'missing')})[0],400)
            client.assert_not_called()

    def test_real_http_proposal_is_utf8_complete_and_unverified_task(self):
        path=self.root/'code.py';path.write_text('original',encoding='utf-8');stream=Stream()
        client=Mock();client.with_options.return_value=client;client.chat.completions.create.return_value=stream
        with patch('server.get_client',return_value=client):
            code,body=self.post(self.server(),{'file_path':str(path),'selected_text':'original','prompt':'rewrite'})
        self.assertEqual(code,200);events=[json.loads(line) for line in body.splitlines()]
        self.assertEqual([item['type'] for item in events],['start','delta','done'])
        self.assertEqual(events[1]['text'],'新資料🙂');self.assertEqual(path.read_text(),'original');self.assertTrue(stream.closed)
        row=self.store.get(self.owner,events[0]['task_id']);self.assertEqual(row['state'],'completed');self.assertEqual(row['verification'],'unverified')

    def test_failed_provider_never_sends_done_or_leaks_error(self):
        path=self.root/'code.py';path.write_text('original');stream=Stream(True)
        client=Mock();client.with_options.return_value=client;client.chat.completions.create.return_value=stream
        with patch('server.get_client',return_value=client):
            _,body=self.post(self.server(),{'file_path':str(path),'selected_text':'original','prompt':'rewrite'})
        self.assertNotIn('SECRET_PROVIDER_ERROR',body);self.assertNotIn('"type": "done"',body)
        self.assertIn('generation_failed',body);self.assertEqual(path.read_text(),'original')

    def test_completion_is_authenticated_bounded_and_budgeted(self):
        path=self.root/'code.py';path.write_text('original')
        client=Mock();client.with_options.return_value=client
        client.chat.completions.create.return_value=NS(choices=[NS(message=NS(content='next'))],usage=None)
        with patch('server.get_client',return_value=client):
            code,body=self.post(self.server(),{'file_path':str(path),'prefix':'original'},path='/api/autocomplete')
        self.assertEqual(code,200);self.assertEqual(json.loads(body),{'completion':'next'})
        kwargs=client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs.get('max_completion_tokens',kwargs.get('max_tokens')),60)
        with closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute('SELECT requests FROM task_budgets').fetchone()[0],1)

    def test_admin_cancel_closes_live_provider_stream(self):
        path=self.root/'code.py';path.write_text('original');released=threading.Event();started=threading.Event()
        class Waiting(Stream):
            def __iter__(self):
                started.set()
                yield NS(choices=[NS(delta=NS(content='partial'))],usage=None)
                released.wait(5)
                yield NS(choices=[NS(delta=NS(content='late'))],usage=None)
            def close(self):self.closed=True;released.set()
        stream=Waiting();client=Mock();client.with_options.return_value=client;client.chat.completions.create.return_value=stream
        result=[];httpd=self.server()
        with patch('server.get_client',return_value=client):
            thread=threading.Thread(target=lambda:result.append(self.post(httpd,
                {'file_path':str(path),'selected_text':'original','prompt':'rewrite'})))
            thread.start()
            import time
            for _ in range(100):
                rows=[row for row in self.store.list(self.owner) if row['id'].startswith('ide-')]
                if rows:break
                time.sleep(.02)
            self.assertTrue(rows);self.assertTrue(started.wait(5),'Provider stream has started')
            self.api('/api/review/tasks/'+rows[0]['id']+'/cancel','POST')
            released.set();thread.join(8);self.assertFalse(thread.is_alive())
        self.assertTrue(stream.closed);self.assertNotIn('"type": "done"',result[0][1]);self.assertEqual(path.read_text(),'original')

    @unittest.skipUnless(os.environ.get('PYTHONAI_BROWSER_TEST')=='1','opt-in live Chromium test')
    def test_chromium_review_diff_approval_cancel_and_viewer(self):
        from playwright.sync_api import sync_playwright
        from communication_server import create_request_handler,MultiWebhookService
        from communication_store import CommunicationStore
        from communication_worker import CommunicationWorker
        from memory import MemoryStore
        with patch.dict(os.environ,{'MEMORY_DB_PATH':str(self.root/'memory.db'),'COMMUNICATION_DB_PATH':str(self.root/'communication.db'),
                'AGENT_TENANT_ROOT':str(self.root/'tenants')}):
            self.store.claim(self.owner,'second','second goal',[])
            MemoryStore();store=CommunicationStore();service=MultiWebhookService(store,{},CommunicationWorker(store,{},Mock()))
            httpd=ThreadingHTTPServer(('127.0.0.1',0),create_request_handler(service))
            threading.Thread(target=httpd.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as playwright:
                    browser=playwright.chromium.launch(headless=True)
                    try:
                        page=browser.new_page();page.goto(f'http://127.0.0.1:{httpd.server_port}')
                        page.fill('#api-token','a'*32);page.click('#login')
                        page.get_by_role('button',name='task · running · unverified',exact=True).click()
                        page.wait_for_function("document.getElementById('review-detail').textContent.includes('計畫🙂')")
                        self.assertIn('<script>bad()</script>',page.locator('#review-detail').inner_text())
                        self.assertEqual(page.locator('#review-detail script').count(),0)
                        latest=page.evaluate("""async () => {
                            const original = window.fetch;
                            window.fetch = async (...args) => {
                                const response = await original(...args);
                                if (args[0] === '/api/review/tasks/task') await new Promise(r => setTimeout(r,200));
                                return response;
                            };
                            try {
                                await Promise.all([showReview('task',reviewEpoch,reviewScope()),showReview('second',reviewEpoch,reviewScope())]);
                                return document.getElementById('review-detail').textContent;
                            } finally {window.fetch=original;}
                        }""")
                        self.assertIn('second goal',latest);self.assertNotIn('review goal',latest)
                        page.get_by_role('button',name='task · running · unverified',exact=True).click()
                        page.wait_for_function("document.getElementById('review-detail').textContent.includes('計畫🙂')")
                        page.screenshot(path='temp/t16-dashboard-diff.png',full_page=True)
                        page.on('dialog',lambda dialog:dialog.accept())
                        page.locator('#review-approvals button').filter(has_text='批准').click()
                        page.wait_for_function("document.getElementById('review-approvals').textContent === ''")
                        page.get_by_role('button',name='task · running · unverified',exact=True).click();page.click('#review-cancel')
                        page.get_by_role('button',name='task · cancelled · unverified',exact=True).wait_for()
                        page.screenshot(path='temp/t16-dashboard-review.png',full_page=True)
                        page.fill('#api-token','b'*32);page.click('#login')
                        page.wait_for_function("document.getElementById('review-scope').textContent.includes('viewer')")
                        self.assertIn('尚無',page.locator('#review-list').inner_text())
                        self.assertEqual(page.locator('#review-detail').inner_text(),'')
                    finally:browser.close()
            finally:httpd.shutdown();httpd.server_close()
