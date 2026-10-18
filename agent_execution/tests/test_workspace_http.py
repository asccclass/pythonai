from contextlib import closing
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

from agent_execution.api import authenticated_api
from agent_execution.policy import execution_scope
from agent_execution.workspace_lifecycle import LifecycleStore


@unittest.skipUnless(os.name=='nt','Windows native cleanup')
class WorkspaceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name);self.root=self.home/'workspace';self.root.mkdir()
        roles=self.home/'roles.json';roles.write_text('{"api:alice":"admin","api:bob":"admin","api:viewer":"viewer"}')
        env=patch.dict(os.environ,{'AGENT_TASK_DB':str(self.home/'tasks.db'),'AGENT_APPROVAL_DB':str(self.home/'approval.db'),
            'AGENT_API_TOKENS':json.dumps({'a'*32:'alice','b'*32:'bob','v'*32:'viewer'}),'ROLES_CONFIG':str(roles),
            'COMMUNICATION_DB_PATH':str(self.home/'comm.db'),'SCHEDULER_DB_PATH':str(self.home/'schedule.db'),
            'AGENT_TENANT_ROOT':str(self.home/'tenants'),'MEMORY_DB_PATH':str(self.home/'memory.db'),
            'AGENT_TRACE_DB':str(self.home/'traces.db'),'AGENT_PROJECT_WORKSPACES':json.dumps({'default':str(self.root)}),
            'AGENT_WORKSPACE_CLEANUP':'1'})
        env.start();self.addCleanup(env.stop)
        self.headers={'Authorization':'Bearer '+'a'*32,'X-Agent-Project':'default','X-Agent-Session':'dashboard'}

    def api(self,action,body=None,headers=None):
        return authenticated_api('/api/session-delete/'+action,'POST',json.dumps(body or {}),headers or self.headers)

    def program(self):
        with execution_scope(source='api',identity='api:alice',project='default',session='default:dashboard',workspace=self.root):
            LifecycleStore().create('temporary','temporary')
            (self.root/'temporary/main.py').write_text('private source')

    def test_human_api_confirmation_is_owner_bound_and_idempotent(self):
        self.program()
        self.assertEqual(self.api('preview',headers={'Authorization':'Bearer invalid'})[0],401)
        code,plan=self.api('preview');self.assertEqual(code,200)
        self.assertTrue((self.root/'temporary').exists())
        for token in ('b','v'):
            self.assertEqual(self.api('confirm',{'plan_id':plan['plan_id']},headers={**self.headers,'Authorization':'Bearer '+token*32})[0],403)
        self.assertEqual(self.api('confirm',{'plan_id':plan['plan_id'],'owner':'alice'})[0],409)
        code,result=self.api('confirm',{'plan_id':plan['plan_id']})
        self.assertEqual((code,result['state']),(200,'deleted'))
        self.assertFalse((self.root/'temporary').exists())
        self.assertEqual(self.api('status',{'plan_id':plan['plan_id']})[1]['state'],'deleted')
        self.assertEqual(self.api('confirm',{'plan_id':plan['plan_id']})[1]['state'],'deleted')
        with execution_scope(source='api',identity='api:alice',session='default:dashboard',workspace=self.root):
            with closing(LifecycleStore().connect()) as conn:
                row=conn.execute('SELECT result,files,bytes FROM wl_audit').fetchone()
                self.assertEqual((row[0],row[1]),('deleted',1))

    @unittest.skipUnless(os.environ.get('PYTHONAI_BROWSER_TEST')=='1','opt-in Chromium management UI')
    def test_chromium_preview_cancel_confirm_and_reload_status(self):
        from playwright.sync_api import sync_playwright
        from communication_server import create_request_handler,MultiWebhookService
        from communication_store import CommunicationStore
        from communication_worker import CommunicationWorker
        self.program()
        store=CommunicationStore();service=MultiWebhookService(store,{},CommunicationWorker(store,{},Mock()))
        httpd=ThreadingHTTPServer(('127.0.0.1',0),create_request_handler(service))
        threading.Thread(target=httpd.serve_forever,daemon=True).start()
        try:
            with sync_playwright() as playwright:
                browser=playwright.chromium.launch(headless=True)
                try:
                    page=browser.new_page();page.goto(f'http://127.0.0.1:{httpd.server_port}')
                    page.fill('#api-token','a'*32);page.click('#cleanup-preview')
                    page.wait_for_function("document.getElementById('cleanup-result').textContent.includes('plan_id')")
                    key=page.input_value('#cleanup-operation')
                    page.once('dialog',lambda dialog:dialog.dismiss());page.click('#cleanup-confirm')
                    self.assertTrue((self.root/'temporary').exists())
                    page.once('dialog',lambda dialog:dialog.accept());page.click('#cleanup-confirm')
                    page.wait_for_function("document.getElementById('cleanup-result').textContent.includes('deleted')")
                    self.assertFalse((self.root/'temporary').exists())
                    page.reload();page.fill('#api-token','a'*32);page.fill('#cleanup-operation',key)
                    self.assertTrue(page.is_disabled('#cleanup-confirm'))
                    page.click('#cleanup-status')
                    page.wait_for_function("document.getElementById('cleanup-result').textContent.includes('deleted')")
                    path=Path(__file__).resolve().parents[2]/'temp'/'workspace-cleanup-admin.png';path.parent.mkdir(exist_ok=True)
                    page.screenshot(path=str(path),full_page=True)
                finally:browser.close()
        finally:httpd.shutdown();httpd.server_close()
