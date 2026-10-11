import http.client
import socket
import threading
import time
import unittest
from urllib.parse import urlencode
from agent_execution.extensions.oauth_callback import LoopbackReceiver


def free_port():
    with socket.socket() as listener:listener.bind(('127.0.0.1',0));return listener.getsockname()[1]


class CallbackTests(unittest.TestCase):
    def callback(self,port,target,host=None):
        connection=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
        try:
            connection.request('GET',target,headers={'Host':host or f'127.0.0.1:{port}'})
            result=connection.getresponse();return result.status,result.read()
        finally:connection.close()
    def test_exact_host_path_state_and_one_use_real_socket(self):
        port=free_port();redirect=f'http://127.0.0.1:{port}/callback';results=[];failures=[]
        with LoopbackReceiver(redirect,3) as receiver:
            receiver.expect('https://issuer.example/authorize?state=trusted-state')
            def wait():
                try:results.append(receiver.wait())
                except Exception as error:failures.append(type(error).__name__)
            thread=threading.Thread(target=wait);thread.start()
            for target,host in [('/callback?state=trusted-state&code=private-code','evil.example'),
                ('/other?state=trusted-state&code=private-code',None),('/callback?state=wrong&code=private-code',None),
                ('/callback?state=%E4%B8%AD%E6%96%87&code=private-code',None),('/callback?state=trusted-state&code=a&code=b',None)]:
                self.assertEqual(self.callback(port,target,host)[0],400)
            target='/callback?'+urlencode({'state':'trusted-state','code':'private-code'})
            status,body=self.callback(port,target);thread.join(3)
            self.assertEqual(status,200);self.assertNotIn(b'private-code',body)
            self.assertFalse(failures);self.assertEqual(results,[redirect+'?'+target.split('?',1)[1]])
            with self.assertRaises(RuntimeError):receiver.wait()
        with socket.socket() as probe:self.assertNotEqual(probe.connect_ex(('127.0.0.1',port)),0)
    def test_timeout_cancel_and_port_conflict_close_without_state(self):
        port=free_port();redirect=f'http://127.0.0.1:{port}/callback'
        with LoopbackReceiver(redirect,1) as receiver:
            receiver.expect('https://issuer.example/?state=test')
            with self.assertRaises(OSError):
                with LoopbackReceiver(redirect):pass
            with self.assertRaises(InterruptedError):receiver.wait(lambda:True)
            started=time.monotonic()
            with self.assertRaises(TimeoutError):receiver.wait()
            self.assertLess(time.monotonic()-started,2)
    def test_slow_partial_request_does_not_block_valid_callback(self):
        port=free_port();redirect=f'http://127.0.0.1:{port}/callback';result=[]
        with LoopbackReceiver(redirect,4) as receiver:
            receiver.expect('https://issuer.example/?state=test')
            thread=threading.Thread(target=lambda:result.append(receiver.wait()));thread.start()
            with socket.create_connection(('127.0.0.1',port),timeout=2) as slow:
                slow.sendall(b'GET /callback HTTP/1.1\r\nHost: ')
                status,_=self.callback(port,'/callback?state=test&code=private-code')
                self.assertEqual(status,200)
            thread.join(3);self.assertEqual(len(result),1)
    def test_non_loopback_and_ambiguous_listener_uris_are_rejected(self):
        for uri in ['http://0.0.0.0:8765/callback','http://localhost:8765/callback','https://127.0.0.1:8765/callback',
                    'http://127.0.0.1/callback','http://127.0.0.1:0/callback','http://127.0.0.1:8765/callback?x=1']:
            with self.subTest(uri=uri),self.assertRaises(ValueError):LoopbackReceiver(uri)
