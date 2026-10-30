"""MCP 2025-11-25 Streamable HTTP: bounded POST JSON/SSE, no replay of writes."""
import json
import os
import threading
import time
import uuid
from urllib.parse import urlsplit
import httpx


class RemoteMCPClient:
    def __init__(self,name,config):
        self.name=name;self.url=config['url'];self.tools=[];self.session=None;self.running=False
        parsed=urlsplit(self.url)
        if parsed.username or parsed.password or parsed.fragment or parsed.query or not parsed.hostname:
            raise ValueError('MCP URL must not contain credentials, query or fragment')
        if parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in {'127.0.0.1','localhost','::1'}):
            raise ValueError('Remote MCP requires HTTPS')
        self.timeout=float(config.get('timeout',10))
        if not 1<=self.timeout<=30:raise ValueError('MCP timeout must be 1..30 seconds')
        self.protocol_version=config.get('protocol_version','2025-11-25')
        if self.protocol_version not in {'2025-11-25','2025-06-18'}:raise ValueError('Unsupported MCP protocol')
        self.health_method=config.get('health_method','ping')
        if self.health_method not in {'ping','tools/list'}:raise ValueError('Invalid MCP health method')
        self.token_env=config.get('token_env');self.guard=threading.Lock();self.last_error=None
        self.oauth=None
        providers={key for key in ('token_env','oauth','memauth') if key in config and config[key] is not None}
        if len(providers)>1:
            raise ValueError('Configure only one MCP authentication provider')
        if 'oauth' in providers:
            from .oauth import OAuthClient
            self.oauth=OAuthClient(self.url,config['oauth'])
        if 'memauth' in providers:
            if not isinstance(config['memauth'],dict):raise ValueError('Invalid MemAuth configuration')
            from .contactai import ContactAuth
            self.oauth=ContactAuth(self.url,config['memauth'])
        self.http=httpx.Client(timeout=self.timeout,follow_redirects=False,trust_env=False)
    def start(self):
        with self.guard:
            self.session=None
            result=self.send_request('initialize',{'protocolVersion':self.protocol_version,'capabilities':{},
                'clientInfo':{'name':'pythonai','version':'1'}})
            if result.get('protocolVersion')!=self.protocol_version:raise ValueError('Unsupported MCP protocol')
            self.send_notification('notifications/initialized',{})
            tools=[];cursor=None
            for _ in range(10):
                page=self.send_request('tools/list',{'cursor':cursor} if cursor else {})
                tools.extend(page['tools'])
                if len(tools)>256:raise ValueError('Too many MCP tools')
                cursor=page.get('nextCursor')
                if not cursor:break
            else:raise ValueError('MCP pagination limit')
            self.tools=tools;self.running=True;self.last_error=None
    def _post(self,request):
        from ..policy import current_context
        cancelled=current_context().check_cancelled
        if cancelled and cancelled():raise InterruptedError('MCP request cancelled')
        headers={'Accept':'application/json, text/event-stream','MCP-Protocol-Version':self.protocol_version}
        token=self.oauth.access_token() if self.oauth else os.environ.get(self.token_env,'') if self.token_env else ''
        if self.token_env and not token:raise PermissionError('MCP OAuth access token required')
        if token:headers['Authorization']='Bearer '+token
        if self.session:headers['Mcp-Session-Id']=self.session
        deadline=time.monotonic()+self.timeout;body=bytearray()
        with self.http.stream('POST',self.url,json=request,headers=headers) as response:
            if response.status_code in {401,403}:raise PermissionError('MCP authorization required')
            if response.status_code==404:self.running=False;raise ConnectionError('MCP session expired; reconnect explicitly')
            response.raise_for_status()
            if response.status_code==202 and 'id' not in request:return {}
            session=response.headers.get('Mcp-Session-Id')
            if session:
                if len(session)>128 or not session.isascii() or not all(33<=ord(c)<=126 for c in session):raise ValueError('Invalid MCP session')
                self.session=session
            for chunk in response.iter_bytes():
                if time.monotonic()>deadline:raise TimeoutError('MCP wall deadline')
                if cancelled and cancelled():raise InterruptedError('MCP request cancelled')
                body.extend(chunk)
                if len(body)>1024*1024:raise ValueError('MCP response exceeds 1 MiB')
                if 'text/event-stream' in response.headers.get('content-type',''):
                    normalized=bytes(body).replace(b'\r\n',b'\n')
                    for raw_block in normalized.split(b'\n\n')[:-1]:
                        block=raw_block.decode('utf-8')
                        raw='\n'.join(line[5:].lstrip() for line in block.splitlines() if line.startswith('data:'))
                        if raw:
                            message=json.loads(raw)
                            if message.get('id')==request.get('id') and ('result' in message or 'error' in message):return message
            if 'application/json' not in response.headers.get('content-type',''):raise ValueError('MCP response must be JSON or SSE')
            return json.loads(body)
    def send_request(self,method,params):
        key=uuid.uuid4().hex
        result=self._post({'jsonrpc':'2.0','id':key,'method':method,'params':params})
        if not isinstance(result,dict) or result.get('jsonrpc')!='2.0' or result.get('id')!=key:raise ValueError('Invalid MCP response correlation')
        if 'error' in result:raise RuntimeError('MCP request rejected')
        return result['result']
    def send_notification(self,method,params):self._post({'jsonrpc':'2.0','method':method,'params':params})
    def call_tool(self,name,arguments):
        with self.guard:
            if not self.running:raise ConnectionError('MCP disconnected; reconnect before calling')
            try:return self.send_request('tools/call',{'name':name,'arguments':arguments})
            except Exception as error:self.last_error=type(error).__name__;raise
    def health(self):
        try:self.send_request(self.health_method,{});return {'ready':self.running,'error':self.last_error}
        except Exception as error:return {'ready':False,'error':type(error).__name__}
    def stop(self):
        self.running=False;self.http.close()
        if self.oauth:self.oauth.close()
