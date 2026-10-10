from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import httpx
from agent_execution.extensions.schema import validate,validator
from agent_execution.extensions.remote import RemoteMCPClient
from agent_execution.extensions.store import ExtensionStore
from agent_execution.policy import execution_scope
from agent_execution.approvals import ApprovalStore,ApprovalRequired


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        env=patch.dict(os.environ,{'AGENT_EXTENSION_DB':str(self.root/'extensions.db'),'AGENT_APPROVAL_DB':str(self.root/'approvals.db')})
        env.start();self.addCleanup(env.stop);self.store=ExtensionStore()
    def package(self,answer=1):
        return {'tool.py':f'def example():\n    return {answer}\n',
            'schema.json':json.dumps({'name':'example','parameters':{'type':'object','properties':{},'additionalProperties':False}}),
            'test_extension.py':f'import unittest\nimport tool\nclass TestExample(unittest.TestCase):\n    def test_answer(self):\n        self.assertEqual(tool.example(), {answer})\n'}
    def stage(self,answer=1):return self.store.stage('example','tool',self.package(answer),'local-reviewed-source')
    def mark_tested(self,key):
        # Unit fixture for isolated activation tests; real Docker case below tests the gate.
        with closing(self.store.connect()) as conn,conn:conn.execute('UPDATE extension_versions SET tested=1 WHERE id=?',(key,))
    def enable(self,key):
        with self.assertRaises(ApprovalRequired) as raised:self.store.activate(key)
        approval=raised.exception.approval_id;ApprovalStore().decide(approval,'local','local',True)
        return self.store.activate(key,approval)
    def test_staging_never_imports_code_and_untested_cannot_activate(self):
        package=self.package();package['tool.py']=f'raise RuntimeError("never import staged code")\n'+package['tool.py']
        key=self.store.stage('example','tool',package,'generated')
        with self.assertRaises(ValueError):self.store.activate(key)
        self.assertEqual(self.store.active(),[])
    def test_human_gate_owner_revocation_and_version_rollback(self):
        first=self.stage();second=self.stage(2);self.mark_tested(first);self.mark_tested(second)
        self.enable(first);old=self.store.tools()[0][1];self.assertEqual(old(),1)
        self.enable(second)
        with self.assertRaises(PermissionError):old()
        self.assertEqual(self.store.tools()[0][1](),2)
        self.enable(first);self.assertEqual(self.store.tools()[0][1](),1)
        with execution_scope(identity='other'):
            self.assertEqual(self.store.active(),[])
            with self.assertRaises(PermissionError):self.store.get(first)
        self.store.disable('example');self.assertEqual(self.store.active(),[])
    def test_tampered_package_and_schema_name_mismatch_rejected(self):
        key=self.stage()
        with closing(self.store.connect()) as conn,conn:conn.execute("UPDATE extension_versions SET files='{}' WHERE id=?",(key,))
        with self.assertRaises(ValueError):self.store.get(key)
        with self.assertRaises(ValueError):self.store.stage('different','tool',self.package(),'generated')
    def test_nested_schema_required_additional_properties_and_bad_refs(self):
        schema={'type':'object','required':['child'],'properties':{'child':{'type':'array','items':{'type':'integer'}}},'additionalProperties':False}
        validate({'child':[1,2]},schema)
        for value in [{},{'child':['bad']},{'child':[],'extra':True}]:
            with self.assertRaises(Exception):validate(value,schema)
        for schema in [{'$ref':'https://attacker/schema'},{'$ref':'#'},{'pattern':'(a+)+$'}]:
            with self.assertRaises(ValueError):validator(schema)
    @unittest.skipUnless(os.environ.get('PYTHONAI_DOCKER_TEST')=='1','real Docker opt-in')
    def test_real_docker_test_gate_and_failure(self):
        key=self.stage();self.assertTrue(self.store.test(key)['passed'])
        self.enable(key);self.assertEqual(self.store.tools()[0][1](),1)
        package=self.package();package['tool.py']='def example():\n    return 99\n'
        failed=self.store.stage('example','tool',package,'bad-version');self.assertFalse(self.store.test(failed)['passed'])
        with self.assertRaises(ValueError):self.store.activate(failed)
        self.assertEqual(self.store.tools()[0][1](),1)


class RemoteMCPTests(unittest.TestCase):
    def client(self,handler):
        client=RemoteMCPClient('external',{'url':'https://mcp.example/endpoint'})
        client.http.close();client.http=httpx.Client(transport=httpx.MockTransport(handler));self.addCleanup(client.stop)
        return client
    def test_handshake_session_and_correlated_sse_tool_result(self):
        calls=[]
        def handler(request):
            value=json.loads(request.content);calls.append(value['method'])
            if value['method']=='notifications/initialized':return httpx.Response(202)
            result={'protocolVersion':'2025-11-25'} if value['method']=='initialize' else {'tools':[]} if value['method']=='tools/list' else {'content':[{'type':'text','text':'中文'}]}
            message={'jsonrpc':'2.0','id':value['id'],'result':result}
            if value['method']=='tools/call':
                self.assertEqual(request.headers['Mcp-Session-Id'],'session')
                return httpx.Response(200,headers={'content-type':'text/event-stream'},content=('data: '+json.dumps(message,ensure_ascii=False)+'\n\n').encode())
            return httpx.Response(200,headers={'Mcp-Session-Id':'session'},json=message)
        client=self.client(handler);client.start()
        self.assertEqual(client.call_tool('tool',{})['content'][0]['text'],'中文')
        self.assertEqual(calls,['initialize','notifications/initialized','tools/list','tools/call'])
    def test_auth_failure_no_replay_and_redacted_error(self):
        count=[]
        client=self.client(lambda request:(count.append(request) or httpx.Response(401,text='secret-provider-body')))
        client.running=True
        with self.assertRaises(PermissionError) as error:client.call_tool('write',{})
        self.assertNotIn('secret',str(error.exception));self.assertEqual(len(count),1)
    def test_url_policy_response_size_and_wrong_id(self):
        for url in ['http://public.example/mcp','https://user:secret@public.example/mcp','https://public.example/mcp?token=x']:
            with self.assertRaises(ValueError):RemoteMCPClient('bad',{'url':url})
        client=self.client(lambda request:httpx.Response(200,json={'jsonrpc':'2.0','id':'wrong','result':{}}))
        with self.assertRaises(ValueError):client.send_request('ping',{})


class MCPRoutingTests(unittest.TestCase):
    def test_underscore_collisions_have_exact_routes_and_validate_results(self):
        from mcp_manager import MCPManager
        seen=[]
        def client(server,name):
            return SimpleNamespace(tools=[{'name':name,'inputSchema':{'type':'object','required':['x'],'properties':{'x':{'type':'integer'}},'additionalProperties':False}}],
                call_tool=lambda tool,args:(seen.append((server,tool)) or {'content':[{'type':'text','text':'ok'}]}))
        manager=MCPManager();manager.clients={'a_b':client('a_b','c'),'a':client('a','b_c')}
        tools=manager.get_all_tools();self.assertEqual(len({t['function']['name'] for t in tools}),2)
        for tool in tools:self.assertTrue(json.loads(manager.call_tool(tool['function']['name'],{'x':1}))['success'])
        self.assertEqual(set(seen),{('a_b','c'),('a','b_c')})
        self.assertFalse(json.loads(manager.call_tool(tools[0]['function']['name'],{}))['success']);self.assertEqual(len(seen),2)
    def test_malicious_schema_and_failed_structured_output_are_rejected(self):
        from mcp_manager import MCPManager
        manager=MCPManager();manager.clients={'server':SimpleNamespace(tools=[{'name':'bad','inputSchema':{'$ref':'https://attacker'}}])}
        self.assertEqual(manager.get_all_tools(),[])
        manager.clients={'server':SimpleNamespace(tools=[{'name':'tool','inputSchema':{'type':'object'},'outputSchema':{'type':'object','required':['ok']}}],call_tool=lambda name,args:{'structuredContent':{}})}
        name=manager.get_all_tools()[0]['function']['name']
        self.assertFalse(json.loads(manager.call_tool(name,{}))['success'])
    def test_real_stdio_handshake_tool_and_shutdown(self):
        from mcp_manager import MCPClient
        client=MCPClient('stdio_fixture',sys.executable,[str(Path(__file__).parents[1]/'extensions'/'fixtures'/'stdio_server.py')])
        self.addCleanup(client.stop);client.start()
        self.assertEqual(client.tools[0]['name'],'read_note')
        self.assertEqual(client.call_tool('read_note',{})['content'][0]['text'],'verified')
        client.stop();self.assertIsNotNone(client.process.poll())
    def test_real_nonreading_pipe_has_bounded_write(self):
        import subprocess,time
        from mcp_manager import MCPClient
        client=MCPClient('blocked',sys.executable,[])
        client.process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        client.running=True;self.addCleanup(client.stop);started=time.monotonic()
        with self.assertRaises(TimeoutError):client._write('x'*1024*1024,.2)
        self.assertLess(time.monotonic()-started,5);self.assertFalse(client.running)
