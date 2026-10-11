"""Bounded, exact-URI loopback OAuth callback receiver; never logs the code."""
import hmac
import ipaddress
import socket
import time
from urllib.parse import parse_qs, urlsplit


class LoopbackReceiver:
    def __init__(self,redirect_uri,timeout=120):
        parsed=urlsplit(redirect_uri)
        if parsed.scheme!='http' or parsed.hostname not in {'127.0.0.1','::1'} or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Callback listener requires an HTTP loopback IP URI')
        if not parsed.port or not parsed.path.startswith('/') or len(redirect_uri)>1024:
            raise ValueError('Callback listener requires an explicit fixed port and path')
        if type(timeout) not in {int,float} or not 1<=timeout<=300:raise ValueError('Callback timeout must be 1..300 seconds')
        self.redirect=redirect_uri;self.parsed=parsed;self.timeout=timeout;self.socket=None;self.state=None
    def __enter__(self):
        listener=socket.socket(socket.AF_INET6 if self.parsed.hostname=='::1' else socket.AF_INET,socket.SOCK_STREAM)
        try:
            if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):listener.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            listener.bind((self.parsed.hostname,self.parsed.port));listener.listen(4);listener.settimeout(.2)
        except BaseException:listener.close();raise
        self.socket=listener;return self
    def __exit__(self,*args):
        if self.socket:self.socket.close();self.socket=None
    def expect(self,authorization_url):
        states=parse_qs(urlsplit(authorization_url).query).get('state',[])
        if len(states)!=1 or not states[0]:raise ValueError('Authorization URL requires one state')
        self.state=states[0]
    def _request(self,connection,deadline):
        data=bytearray();deadline=min(deadline,time.monotonic()+1)
        while b'\r\n\r\n' not in data:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('Callback request deadline')
            connection.settimeout(min(.2,remaining))
            try:chunk=connection.recv(1024)
            except socket.timeout:continue
            if not chunk:raise ValueError('Incomplete callback request')
            data.extend(chunk)
            if len(data)>8192:raise ValueError('Callback headers exceed 8 KiB')
        header,body=bytes(data).split(b'\r\n\r\n',1)
        lines=header.decode('ascii').split('\r\n');parts=lines[0].split(' ')
        if len(parts)!=3 or parts[0]!='GET' or parts[2] not in {'HTTP/1.0','HTTP/1.1'}:raise ValueError('Callback requires GET')
        target=parts[1]
        if len(target)>2048 or not target.startswith('/') or target.startswith('//'):raise ValueError('Invalid callback target')
        headers={}
        for line in lines[1:]:
            key,separator,value=line.partition(':');key=key.lower()
            if not separator or not key or key in headers or line.startswith((' ','\t')):raise ValueError('Invalid callback headers')
            headers[key]=value.strip()
        if headers.get('host')!=self.parsed.netloc or body or headers.get('content-length','0')!='0' or 'transfer-encoding' in headers:
            raise ValueError('Callback host or body rejected')
        parsed=urlsplit(target)
        if parsed.path!=self.parsed.path or parsed.fragment:raise ValueError('Callback path mismatch')
        params=parse_qs(parsed.query,keep_blank_values=True)
        states=params.get('state',[])
        if len(states)!=1 or not hmac.compare_digest(states[0].encode('utf-8'),self.state.encode('utf-8')):raise ValueError('Callback state rejected')
        if 'error' in params:
            if len(params['error'])!=1 or 'code' in params:raise ValueError('Invalid OAuth error callback')
        elif len(params.get('code',[]))!=1 or not params['code'][0]:raise ValueError('Callback requires one code')
        return self.redirect+'?'+parsed.query
    def wait(self,check_cancelled=None):
        if not self.socket or not self.state:raise RuntimeError('Callback listener is not ready')
        deadline=time.monotonic()+self.timeout
        while time.monotonic()<deadline:
            if check_cancelled and check_cancelled():raise InterruptedError('OAuth login cancelled')
            try:connection,address=self.socket.accept()
            except socket.timeout:continue
            with connection:
                callback=None
                try:
                    if not ipaddress.ip_address(address[0]).is_loopback:raise ValueError('Non-loopback callback rejected')
                    callback=self._request(connection,deadline);status='200 OK';body=b'Authorization received. You may close this page.'
                except (ValueError,UnicodeError,TimeoutError,OSError):status='400 Bad Request';body=b'Callback rejected.'
                connection.settimeout(.2)
                response=('HTTP/1.1 '+status+'\r\nContent-Type: text/plain; charset=utf-8\r\nCache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nConnection: close\r\nContent-Length: '+str(len(body))+'\r\n\r\n').encode()+body
                try:connection.sendall(response)
                except OSError:pass
                if callback:self.state=None;return callback
        raise TimeoutError('OAuth callback wait expired')
