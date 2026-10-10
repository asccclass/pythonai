"""Private stdio worker; its parent bounds time/output and kills the process tree."""
import json
import os
import subprocess
import sys
from pathlib import Path

def run_jedi(request):
    import jedi
    path=Path(request['path']);root=Path(request['root'])
    script=jedi.Script(code=request['text'],path=str(path),project=jedi.Project(str(root)))
    position=request['source_position']
    items=script.get_references(**position) if request['method']=='textDocument/references' else script.goto(**position)
    result=[]
    for item in items:
        if item.module_path is None or type(item.line) is not int or type(item.column) is not int:continue
        target=Path(item.module_path).resolve()
        if root not in target.parents:continue
        text=request['text'] if target==path else target.read_text(encoding='utf-8')
        line=text.splitlines()[item.line-1];column=len(line[:item.column].encode('utf-16-le'))//2
        point={'line':item.line-1,'character':column}
        result.append({'uri':target.as_uri(),'range':{'start':point,'end':point}})
        if len(result)>=1000:break
    return result

def run(request):
    options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
    process=subprocess.Popen(request['command'],cwd=request['root'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,**options)
    total=0;messages=0
    def send(value):
        data=json.dumps(value).encode();process.stdin.write(b'Content-Length: '+str(len(data)).encode()+b'\r\n\r\n'+data);process.stdin.flush()
    def receive(identifier):
        nonlocal total,messages
        while True:
            headers={};size=0
            while True:
                line=process.stdout.readline(8193);size+=len(line)
                if not line or size>8192:raise ValueError('Invalid LSP header')
                if line in {b'\r\n',b'\n'}:break
                key,value=line.decode('ascii').strip().split(':',1);headers[key.lower()]=value.strip()
            count=int(headers['content-length']);total+=count;messages+=1
            if not 0<count<=1024*1024 or total>2*1024*1024 or messages>64:raise ValueError('LSP response exceeds limits')
            data=process.stdout.read(count)
            if len(data)!=count:raise ValueError('Truncated LSP body')
            value=json.loads(data)
            if value.get('id')==identifier:
                if 'error' in value:raise ValueError('LSP server rejected request')
                return value.get('result')
            if 'method' in value and 'id' in value:
                method=value['method']
                result=[{} for _ in value.get('params',{}).get('items',[])] if method=='workspace/configuration' else None
                if method=='workspace/workspaceFolders':result=[{'uri':request['root_uri'],'name':'workspace'}]
                send({'jsonrpc':'2.0','id':value['id'],'result':result} if method in {'workspace/configuration','workspace/workspaceFolders','client/registerCapability','window/workDoneProgress/create'} else {'jsonrpc':'2.0','id':value['id'],'error':{'code':-32601,'message':'Unsupported client method'}})
    try:
        send({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'processId':os.getpid(),'rootUri':request['root_uri'],
            'workspaceFolders':[{'uri':request['root_uri'],'name':'workspace'}],'capabilities':{'general':{'positionEncodings':['utf-16']}}}})
        initialized=receive(1) or {}
        if initialized.get('capabilities',{}).get('positionEncoding','utf-16')!='utf-16':raise ValueError('Unsupported LSP position encoding')
        send({'jsonrpc':'2.0','method':'initialized','params':{}})
        send({'jsonrpc':'2.0','method':'textDocument/didOpen','params':{'textDocument':{'uri':request['uri'],'languageId':request['language'],'version':1,'text':request['text']}}})
        send({'jsonrpc':'2.0','id':2,'method':request['method'],'params':request['params']})
        result=receive(2)
        send({'jsonrpc':'2.0','id':3,'method':'shutdown','params':None});receive(3)
        send({'jsonrpc':'2.0','method':'exit','params':None})
        return result
    finally:
        if process.poll() is None:process.kill()
        process.wait(timeout=5)
        for stream in (process.stdin,process.stdout):stream.close()

if __name__=='__main__':
    try:
        with open(sys.argv[1],encoding='utf-8') as source:request=json.load(source)
        print(json.dumps({'result':run_jedi(request) if request.get('backend')=='jedi' else run(request)},ensure_ascii=True))
    except Exception as error:
        print(json.dumps({'error':type(error).__name__}));sys.exit(1)
