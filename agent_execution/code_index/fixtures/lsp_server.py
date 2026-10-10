"""Offline protocol fixture, not a production language server."""
import json
from pathlib import Path
import sys
import time

mode=sys.argv[1] if len(sys.argv)>1 else 'normal'
uri=None;text=''
def send(value):
    data=json.dumps(value).encode();sys.stdout.buffer.write(b'Content-Length: '+str(len(data)).encode()+b'\r\n\r\n'+data);sys.stdout.buffer.flush()
while True:
    headers={}
    while True:
        line=sys.stdin.buffer.readline()
        if not line:sys.exit()
        if line==b'\r\n':break
        key,value=line.decode().split(':',1);headers[key.lower()]=value.strip()
    value=json.loads(sys.stdin.buffer.read(int(headers['content-length'])))
    method=value.get('method')
    if method=='initialize':
        if mode=='hang':time.sleep(60)
        if mode=='oversize':sys.stdout.buffer.write(b'Content-Length: 3000000\r\n\r\n');sys.stdout.buffer.flush();time.sleep(60)
        send({'jsonrpc':'2.0','id':value['id'],'result':{'capabilities':{'positionEncoding':'utf-16'}}})
    elif method=='textDocument/didOpen':uri=value['params']['textDocument']['uri'];text=value['params']['textDocument']['text']
    elif method in {'textDocument/definition','textDocument/references','textDocument/documentSymbol'}:
        if method!='textDocument/documentSymbol':
            position=value['params']['position'];line=text.splitlines()[position['line']]
            expected=len(line[:line.index('calculate')].encode('utf-16-le'))//2
            if position['character']!=expected:send({'id':value['id'],'error':{'code':-1}});continue
        position={'line':4,'character':13};range_={'start':position,'end':position}
        result=[{'uri':uri,'range':range_},{'uri':'file:///C:/outside/secret.ts','range':range_}]
        if method=='textDocument/documentSymbol':result=[{'name':'calculate','kind':12,'range':range_,'selectionRange':range_}]
        if mode=='malformed':result=[{'uri':uri,'range':{'start':{'line':-1,'character':0},'end':position}}]
        send({'jsonrpc':'2.0','id':value['id'],'result':result})
    elif method=='shutdown':send({'jsonrpc':'2.0','id':value['id'],'result':None})
    elif method=='exit':break
