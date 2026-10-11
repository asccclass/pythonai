from contextlib import closing
import base64
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit,urlencode
from agent_execution.extensions.oauth import OAuthClient
from agent_execution.extensions.remote import RemoteMCPClient
from agent_execution.policy import execution_scope
from agent_execution.tests.test_oauth_callback import free_port


class OAuthTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        owner=self;self.posts=[];self.challenge=None;self.reject_token=False;self.issuer_mismatch=False;self.resource_mismatch=False;self.oauth_fallback=False;self.authorization_query=''
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def output(self,value,status=200):
                self.send_response(status);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(value).encode())
            def do_GET(self):
                base=owner.base
                if self.path=='/mcp':
                    self.send_response(401);self.send_header('WWW-Authenticate',f'Bearer resource_metadata="{base}/metadata", scope="files:read"');self.end_headers()
                elif self.path=='/metadata':self.output({'resource':base+('/wrong' if owner.resource_mismatch else '/mcp'),'authorization_servers':[base+'/tenant']})
                elif self.path=='/.well-known/oauth-authorization-server/tenant' and owner.oauth_fallback:self.output({},404)
                elif self.path in {'/.well-known/oauth-authorization-server/tenant','/.well-known/openid-configuration/tenant'}:
                    self.output({'issuer':base+('/wrong' if owner.issuer_mismatch else '/tenant'),'authorization_endpoint':base+'/authorize'+owner.authorization_query,
                        'token_endpoint':base+'/token','code_challenge_methods_supported':['S256'],'token_endpoint_auth_methods_supported':['none']})
                else:self.output({},404)
            def do_POST(self):
                body=self.rfile.read(int(self.headers.get('Content-Length','0')))
                if self.path=='/token':
                    params=parse_qs(body.decode());owner.posts.append(params)
                    if owner.reject_token:return self.output({'error':'invalid_grant'},400)
                    if params['grant_type']==['authorization_code']:
                        challenge=base64.urlsafe_b64encode(hashlib.sha256(params['code_verifier'][0].encode()).digest()).rstrip(b'=').decode()
                        if challenge!=owner.challenge:return self.output({'error':'bad_pkce'},400)
                    return self.output({'token_type':'Bearer','access_token':'access-private','refresh_token':'rotated-private','expires_in':3600})
                owner.posts.append({'rpc':json.loads(body),'authorization':self.headers.get('Authorization')})
                return self.output({'error':'unauthorized'},401)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.base=f'http://127.0.0.1:{self.server.server_port}'
        thread=threading.Thread(target=self.server.serve_forever,daemon=True);thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.env=patch.dict(os.environ,{'AGENT_MCP_OAUTH_DB':str(self.root/'oauth.db')});self.env.start();self.addCleanup(self.env.stop)
        self.config={'issuer':self.base+'/tenant','client_id':'registered-client','redirect_uri':'http://127.0.0.1:8765/callback'}
        self.client=OAuthClient(self.base+'/mcp',self.config);self.addCleanup(self.client.close)
    def authorize(self):
        begin=self.client.begin();params=parse_qs(urlsplit(begin['authorization_url']).query)
        self.challenge=params['code_challenge'][0]
        self.assertEqual(params['resource'],[self.base+'/mcp']);self.assertEqual(params['scope'],['files:read'])
        callback=self.config['redirect_uri']+'?'+urlencode({'state':params['state'][0],'code':'human-code'})
        result=self.client.finish(callback);return callback,result
    def test_real_http_discovery_pkce_callback_resource_and_local_revoke(self):
        callback,result=self.authorize();self.assertTrue(result['authorized']);self.assertNotIn('access-private',json.dumps(result))
        self.assertEqual(self.client.access_token(),'access-private');self.assertEqual(self.posts[0]['resource'],[self.base+'/mcp'])
        with self.assertRaises(PermissionError):self.client.finish(callback)
        self.client.revoke()
        with self.assertRaises(PermissionError):self.client.access_token()
    def test_state_owner_exact_redirect_and_issuer_resource_validation(self):
        begin=self.client.begin();params=parse_qs(urlsplit(begin['authorization_url']).query);self.challenge=params['code_challenge'][0]
        callback=self.config['redirect_uri']+'?'+urlencode({'state':params['state'][0],'code':'human-code'})
        with self.assertRaises(PermissionError):self.client.finish(callback.replace('/callback','/other'))
        with execution_scope(identity='other'),self.assertRaises(PermissionError):self.client.finish(callback)
        self.client.finish(callback)
        self.issuer_mismatch=True
        with self.assertRaises(PermissionError):self.client.begin()
        self.issuer_mismatch=False;self.resource_mismatch=True
        with self.assertRaises(PermissionError):self.client.begin()
    def test_oidc_metadata_fallback_and_expired_one_use_state(self):
        self.oauth_fallback=True;begin=self.client.begin();params=parse_qs(urlsplit(begin['authorization_url']).query)
        with closing(self.client.connect()) as conn,conn:conn.execute('UPDATE oauth_states SET expires=0')
        callback=self.config['redirect_uri']+'?'+urlencode({'state':params['state'][0],'code':'human-code'})
        with self.assertRaises(PermissionError):self.client.finish(callback)
    def test_discovered_authorization_query_collisions_fail_before_state_creation(self):
        self.authorize()
        for name in ('response_type','client_id','redirect_uri','state','code_challenge',
                'code_challenge_method','resource','scope','%73tate'):
            with self.subTest(parameter=name):
                self.authorization_query='?'+name+'='
                with self.assertRaises(ValueError):self.client.begin()
                with closing(self.client.connect()) as conn:
                    self.assertEqual(conn.execute('SELECT count(*) FROM oauth_states').fetchone()[0],0)
                self.assertEqual(self.client.access_token(),'access-private')
        self.assertEqual(len(self.posts),1)
    def test_non_oauth_endpoint_query_is_preserved_through_real_exchange(self):
        self.authorization_query='?tenant=company%2Fdivision&prompt=consent'
        begin=self.client.begin();url=begin['authorization_url']
        self.assertIn('/authorize'+self.authorization_query+'&',url)
        params=parse_qs(urlsplit(url).query)
        self.assertEqual(params['tenant'],['company/division']);self.assertEqual(len(params['state']),1)
        self.challenge=params['code_challenge'][0]
        callback=self.config['redirect_uri']+'?'+urlencode({'state':params['state'][0],'code':'human-code'})
        self.assertTrue(self.client.finish(callback)['authorized'])
        self.assertEqual(self.posts[-1]['redirect_uri'],[self.config['redirect_uri']])
    def test_serialized_refresh_rotation_and_uncertain_refresh_not_replayed(self):
        self.authorize()
        with closing(self.client.connect()) as conn,conn:conn.execute('UPDATE oauth_tokens SET expires=0')
        self.assertEqual(self.client.access_token(),'access-private');self.assertEqual(self.posts[-1]['grant_type'],['refresh_token'])
        self.assertEqual(self.posts[-1]['refresh_token'],['rotated-private'])
        with closing(self.client.connect()) as conn,conn:conn.execute('UPDATE oauth_tokens SET expires=0')
        self.reject_token=True
        with self.assertRaises(PermissionError):self.client.access_token()
        count=len(self.posts)
        with self.assertRaises(PermissionError):self.client.access_token()
        self.assertEqual(len(self.posts),count)
    def test_mcp_unauthorized_tool_is_sent_once_without_login_or_replay(self):
        self.authorize();remote=RemoteMCPClient('fixture',{'url':self.base+'/mcp','oauth':self.config});self.addCleanup(remote.stop);remote.running=True
        with self.assertRaises(PermissionError):remote.call_tool('side_effect',{})
        rpc=[p for p in self.posts if 'rpc' in p];self.assertEqual(len(rpc),1);self.assertEqual(rpc[0]['authorization'],'Bearer access-private')
    def test_untrusted_challenge_and_plaintext_endpoints_are_rejected(self):
        with self.assertRaises(PermissionError):self.client.endpoint('https://attacker.example/metadata',True)
        with self.assertRaises(ValueError):self.client.endpoint('http://issuer.example/token')
        with self.assertRaises(PermissionError):self.client.endpoint('https://other.example/token')
    def test_concurrent_refresh_only_exchanges_once(self):
        self.authorize()
        with closing(self.client.connect()) as conn,conn:conn.execute('UPDATE oauth_tokens SET expires=0')
        second=OAuthClient(self.base+'/mcp',self.config);self.addCleanup(second.close);tokens=[];errors=[]
        def refresh(client):
            try:tokens.append(client.access_token())
            except Exception as error:errors.append(type(error).__name__)
        threads=[threading.Thread(target=refresh,args=(client,)) for client in (self.client,second)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertFalse(errors);self.assertEqual(tokens,['access-private','access-private'])
        self.assertEqual(sum(p.get('grant_type')==['refresh_token'] for p in self.posts),1)
    def test_real_cli_listener_pkce_exchange_does_not_print_code_or_tokens(self):
        import subprocess
        import sys
        from urllib.request import urlopen
        self.config['redirect_uri']=f'http://127.0.0.1:{free_port()}/callback'
        config=self.root/'config.json';config.write_text(json.dumps({'url':self.base+'/mcp','oauth':self.config}))
        process=subprocess.Popen([sys.executable,'-m','agent_execution.extensions.oauth_cli','login',str(config),'--timeout','5'],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
        try:
            begin=json.loads(process.stdout.readline());params=parse_qs(urlsplit(begin['authorization_url']).query)
            self.challenge=params['code_challenge'][0]
            callback=self.config['redirect_uri']+'?'+urlencode({'state':params['state'][0],'code':'private-human-code'})
            with urlopen(callback,timeout=3) as response:self.assertEqual(response.status,200)
            stdout,stderr=process.communicate(timeout=8)
            self.assertEqual(process.returncode,0,stderr);self.assertTrue(json.loads(stdout)['authorized'])
            for secret in ['private-human-code','access-private','rotated-private']:self.assertNotIn(secret,stdout+stderr)
            self.assertEqual(self.posts[-1]['resource'],[self.base+'/mcp'])
        finally:
            if process.poll() is None:process.kill();process.communicate()
        with closing(self.client.connect()) as conn:self.assertEqual(conn.execute('SELECT count(*) FROM oauth_states').fetchone()[0],0)
    def test_cancelled_listener_cleans_only_new_state_without_revoking_grant(self):
        from agent_execution.extensions.oauth_cli import login
        self.config['redirect_uri']=f'http://127.0.0.1:{free_port()}/callback'
        self.client=OAuthClient(self.base+'/mcp',self.config);self.addCleanup(self.client.close)
        self.authorize()
        with self.assertRaises(InterruptedError):login(self.client,timeout=1,emit=lambda text:None,check_cancelled=lambda:True)
        self.assertEqual(self.client.access_token(),'access-private')
        with closing(self.client.connect()) as conn:self.assertEqual(conn.execute('SELECT count(*) FROM oauth_states').fetchone()[0],0)
