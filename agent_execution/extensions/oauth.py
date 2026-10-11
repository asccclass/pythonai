"""Human initiated, resource-bound public-client OAuth with bounded discovery."""
from contextlib import closing
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
import httpx
from ..policy import current_context
from ..tasks import task_owner


def safe_url(value):
    if not isinstance(value,str) or len(value)>2048:raise ValueError('Invalid OAuth URL')
    parsed=urlsplit(value)
    if parsed.username or parsed.password or parsed.fragment or not parsed.hostname:raise ValueError('Invalid OAuth URL')
    if parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in {'localhost','127.0.0.1','::1'}):raise ValueError('OAuth requires HTTPS or loopback')
    return value


def origin(value):
    parsed=urlsplit(safe_url(value));return parsed.scheme+'://'+parsed.netloc


class OAuthClient:
    def __init__(self,resource,config,path=None):
        self.resource=safe_url(resource);self.config=dict(config)
        self.issuer=safe_url(config['issuer'])
        self.redirect=safe_url(config['redirect_uri'])
        if urlsplit(self.redirect).query:raise ValueError('OAuth redirect query must be empty')
        self.client_id=config['client_id']
        if not isinstance(self.client_id,str) or not 1<=len(self.client_id)<=2048:raise ValueError('Invalid client ID')
        self.scope=config.get('scope','')
        if not isinstance(self.scope,str) or len(self.scope)>2048:raise ValueError('Invalid OAuth scope')
        self.owner=task_owner(current_context())
        self.key=hashlib.sha256(json.dumps([self.resource,self.config],sort_keys=True).encode()).hexdigest()
        self.path=Path(path or os.environ.get('AGENT_MCP_OAUTH_DB') or Path(os.environ.get('LOCALAPPDATA',Path.home()))/'PythonAI'/'mcp-oauth.db')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.http=httpx.Client(timeout=10,follow_redirects=False,trust_env=False)
        with closing(self.connect()) as conn,conn:
            conn.executescript('''CREATE TABLE IF NOT EXISTS oauth_states(state TEXT PRIMARY KEY,owner TEXT,key TEXT,verifier TEXT,metadata TEXT,expires REAL);
                CREATE TABLE IF NOT EXISTS oauth_tokens(owner TEXT,key TEXT,data TEXT,expires REAL,PRIMARY KEY(owner,key));''')
        if os.name!='nt':self.path.chmod(0o600)
    def connect(self):
        if self.path.exists() and self.path.stat().st_size>16*1024*1024:raise ValueError('OAuth storage capacity reached')
        conn=sqlite3.connect(self.path,timeout=12);conn.row_factory=sqlite3.Row;return conn
    def check_owner(self):
        if self.owner!=task_owner(current_context()):raise PermissionError('OAuth owner changed')
    def endpoint(self,value,resource_metadata=False):
        safe_url(value)
        trusted={origin(self.resource)} if resource_metadata else {origin(self.issuer),*self.config.get('allowed_endpoint_origins',[])}
        if origin(value) not in trusted:raise PermissionError('OAuth endpoint origin is not trusted')
        return value
    def request(self,method,url,data=None):
        deadline=time.monotonic()+10;body=bytearray()
        with self.http.stream(method,url,data=data,headers={'Accept':'application/json'}) as response:
            if response.status_code>=400:raise PermissionError('OAuth endpoint rejected request')
            if response.is_redirect:raise PermissionError('OAuth redirects are not followed')
            if 'application/json' not in response.headers.get('content-type',''):raise ValueError('OAuth endpoint must return JSON')
            for chunk in response.iter_bytes():
                if time.monotonic()>deadline:raise TimeoutError('OAuth response deadline')
                body.extend(chunk)
                if len(body)>65536:raise ValueError('OAuth response exceeds 64 KiB')
        result=json.loads(body)
        if not isinstance(result,dict):raise ValueError('OAuth response must be an object')
        return result
    def discover(self):
        parsed=urlsplit(self.resource);base=origin(self.resource);urls=[]
        # Only a read-only probe: no tools/call replay or credentials during discovery.
        with self.http.stream('GET',self.resource,headers={'Accept':'application/json'}) as response:
            if response.is_redirect:raise PermissionError('OAuth discovery redirect rejected')
            challenge=response.headers.get('WWW-Authenticate','')[:8192]
        match=re.search(r'\bresource_metadata="([^"]+)"',challenge,re.I)
        if match:urls.append(self.endpoint(match.group(1),True))
        else:
            if parsed.path and parsed.path!='/':urls.append(base+'/.well-known/oauth-protected-resource'+parsed.path)
            urls.append(base+'/.well-known/oauth-protected-resource')
        resource=None
        for url in urls:
            try:resource=self.request('GET',url);break
            except PermissionError:continue
        issuers=(resource or {}).get('authorization_servers')
        if not resource or resource.get('resource')!=self.resource or not isinstance(issuers,list) or not 1<=len(issuers)<=16 or self.issuer not in issuers:
            raise PermissionError('Protected resource or trusted issuer mismatch')
        issuer=urlsplit(self.issuer);root=origin(self.issuer);path=issuer.path.rstrip('/')
        urls=[root+'/.well-known/oauth-authorization-server'+path,root+'/.well-known/openid-configuration'+path]
        if path:urls.append(self.issuer.rstrip('/')+'/.well-known/openid-configuration')
        metadata=None
        for url in urls:
            try:metadata=self.request('GET',url);break
            except PermissionError:continue
        if not metadata or metadata.get('issuer')!=self.issuer:raise PermissionError('OAuth issuer mismatch')
        if 'S256' not in metadata.get('code_challenge_methods_supported',[]):raise PermissionError('OAuth requires advertised S256 PKCE')
        for name in ('authorization_endpoint','token_endpoint'):self.endpoint(metadata[name])
        if 'none' not in metadata.get('token_endpoint_auth_methods_supported',['none']):raise ValueError('Only pre-registered public clients are supported')
        match=re.search(r'\bscope="([^"]*)"',challenge,re.I)
        scope=match.group(1) if match else self.scope
        if len(scope)>2048:raise ValueError('OAuth scope exceeds limit')
        metadata['requested_scope']=scope
        return metadata
    def begin(self):
        self.check_owner();metadata=self.discover();state=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(48)
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE');conn.execute('DELETE FROM oauth_states WHERE expires<?',(time.time(),))
            if conn.execute('SELECT count(*) FROM oauth_states').fetchone()[0]>=100:raise ValueError('Too many pending authorizations')
            conn.execute('INSERT INTO oauth_states VALUES(?,?,?,?,?,?)',(state,self.owner,self.key,verifier,json.dumps(metadata),time.time()+600))
        params={'response_type':'code','client_id':self.client_id,'redirect_uri':self.redirect,'state':state,
            'code_challenge':challenge,'code_challenge_method':'S256','resource':self.resource}
        if metadata['requested_scope']:params['scope']=metadata['requested_scope']
        url=metadata['authorization_endpoint'];separator='&' if urlsplit(url).query else '?'
        return {'authorization_url':url+separator+urlencode(params),'expires_in':600,'redirect_uri':self.redirect}
    def token_data(self,result,metadata,old=None):
        token=result.get('access_token');refresh=result.get('refresh_token',(old or {}).get('refresh_token'))
        if not isinstance(token,str) or not 1<=len(token)<=8192 or not token.isascii() or any(ord(c)<33 or ord(c)>126 for c in token):raise ValueError('Invalid access token')
        if result.get('token_type','').lower()!='bearer':raise ValueError('Expected Bearer token')
        if refresh is not None and (not isinstance(refresh,str) or not 1<=len(refresh)<=8192):raise ValueError('Invalid refresh token')
        lifetime=result.get('expires_in',300)
        if type(lifetime) not in {int,float} or not 1<=lifetime<=86400:raise ValueError('Invalid token lifetime')
        return {'access_token':token,'refresh_token':refresh,'token_endpoint':metadata['token_endpoint']},time.time()+lifetime
    def finish(self,callback_url):
        self.check_owner();parsed=urlsplit(safe_url(callback_url))
        if urlunsplit((parsed.scheme,parsed.netloc,parsed.path,'',''))!=self.redirect:raise PermissionError('OAuth redirect mismatch')
        params=parse_qs(parsed.query,keep_blank_values=True)
        if len(callback_url)>4096 or len(params.get('state',[]))!=1 or len(params.get('code',[]))!=1 or 'error' in params:raise ValueError('Invalid OAuth callback')
        state=params['state'][0];code=params['code'][0]
        if not code or len(code)>2048:raise ValueError('Invalid authorization code')
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT * FROM oauth_states WHERE state=? AND owner=? AND key=? AND expires>?',(state,self.owner,self.key,time.time())).fetchone()
            if not row:raise PermissionError('OAuth state expired, consumed or wrong owner')
            conn.execute('DELETE FROM oauth_states WHERE state=?',(state,))
        # Consume before network: unknown exchange outcome requires a fresh human login.
        metadata=json.loads(row['metadata']);result=self.request('POST',self.endpoint(metadata['token_endpoint']),
            {'grant_type':'authorization_code','code':code,'client_id':self.client_id,'redirect_uri':self.redirect,
             'code_verifier':row['verifier'],'resource':self.resource})
        data,expires=self.token_data(result,metadata)
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT count(*) FROM oauth_tokens').fetchone()[0]>=1000 and not conn.execute('SELECT 1 FROM oauth_tokens WHERE owner=? AND key=?',(self.owner,self.key)).fetchone():raise ValueError('OAuth token capacity reached')
            conn.execute('INSERT OR REPLACE INTO oauth_tokens VALUES(?,?,?,?)',(self.owner,self.key,json.dumps(data),expires))
        return {'authorized':True,'resource':self.resource,'expires_at':expires}
    def access_token(self):
        self.check_owner()
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT * FROM oauth_tokens WHERE owner=? AND key=?',(self.owner,self.key)).fetchone()
            if not row:raise PermissionError('Human MCP OAuth authorization required')
            data=json.loads(row['data'])
            if row['expires']<=time.time()+30:
                if not data.get('refresh_token'):raise PermissionError('OAuth login expired; authorize again')
                # Fence before exchange. Timeout cannot replay a possibly rotated refresh token.
                conn.execute('DELETE FROM oauth_tokens WHERE owner=? AND key=?',(self.owner,self.key));conn.commit();conn.execute('BEGIN IMMEDIATE')
                newer=conn.execute('SELECT data FROM oauth_tokens WHERE owner=? AND key=?',(self.owner,self.key)).fetchone()
                if newer:return json.loads(newer['data'])['access_token']
                result=self.request('POST',self.endpoint(data['token_endpoint']),{'grant_type':'refresh_token',
                    'refresh_token':data['refresh_token'],'client_id':self.client_id,'resource':self.resource})
                updated,expires=self.token_data(result,data,data)
                conn.execute('INSERT OR REPLACE INTO oauth_tokens VALUES(?,?,?,?)',(self.owner,self.key,json.dumps(updated),expires));data=updated
            return data['access_token']
    def revoke(self):
        self.check_owner()
        with closing(self.connect()) as conn,conn:
            conn.execute('DELETE FROM oauth_tokens WHERE owner=? AND key=?',(self.owner,self.key))
            conn.execute('DELETE FROM oauth_states WHERE owner=? AND key=?',(self.owner,self.key))
        return {'local_credentials_removed':True,'provider_revocation':'use provider account controls'}
    def cancel_state(self,state):
        self.check_owner()
        with closing(self.connect()) as conn,conn:
            conn.execute('DELETE FROM oauth_states WHERE state=? AND owner=? AND key=?',(state,self.owner,self.key))
    def close(self):self.http.close()
