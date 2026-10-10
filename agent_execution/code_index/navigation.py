"""Optional trusted LSP backends, with syntax candidates as an explicit fallback."""
from contextlib import closing
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import url2pathname

from .index import CodeIndex
from ..policy import resolve_path
from ..process import run_process

def servers():
    value=json.loads(os.environ.get('AGENT_CODE_LSP_SERVERS','{}'))
    if not isinstance(value,dict) or set(value)-{'python','go','javascript','typescript','tsx'}:raise ValueError('Invalid LSP language configuration')
    for config in value.values():
        if not isinstance(config,dict) or set(config)-{'command','timeout'}:raise ValueError('Invalid LSP server configuration')
        argv=config.get('command')
        if not isinstance(argv,list) or not argv or any(not isinstance(arg,str) or not arg or '\x00' in arg for arg in argv):raise ValueError('LSP requires trusted command argv')
        if type(config.get('timeout',10)) not in {int,float} or not 0<config.get('timeout',10)<=30:raise ValueError('LSP timeout must be 0..30 seconds')
    return value

def lsp(index,path,method,line=1,column=0):
    name=path.relative_to(index.root).as_posix()
    with closing(index.connect()) as conn:
        row=conn.execute('SELECT * FROM ci_files WHERE path=?',(name,)).fetchone()
        snapshot={entry['path']:entry['hash'] for entry in conn.execute('SELECT path,hash FROM ci_files')}
    if row is None:raise ValueError('File is excluded or not indexed')
    config=servers().get(row['language'])
    if config is None and row['language']=='python' and method!='textDocument/documentSymbol':config={'command':[],'timeout':10,'backend':'jedi'}
    if config is None:raise ValueError('No configured LSP server for this language')
    lines=row['text'].splitlines()
    if method!='textDocument/documentSymbol' and (type(line) is not int or not 1<=line<=len(lines) or type(column) is not int or not 0<=column<=len(lines[line-1])):raise ValueError('Invalid source position')
    params={'textDocument':{'uri':path.as_uri()}}
    if method!='textDocument/documentSymbol':params['position']={'line':line-1,'character':len(lines[line-1][:column].encode('utf-16-le'))//2}
    if method=='textDocument/references':params['context']={'includeDeclaration':True}
    request={**config,'path':str(path),'source_position':{'line':line,'column':column},'root':str(index.root),'root_uri':index.root.as_uri(),'uri':path.as_uri(),'text':row['text'],
        'language':'typescript' if row['language']=='tsx' else row['language'],'method':method,'params':params}
    with tempfile.TemporaryDirectory(prefix='pythonai-lsp-') as directory:
        source=Path(directory)/'request.json';source.write_text(json.dumps(request),encoding='utf-8')
        result=run_process([sys.executable,str(Path(__file__).with_name('lsp_worker.py')),str(source)],cwd=index.root,timeout=config.get('timeout',10),max_output_bytes=2*1024*1024)
    if result.returncode:raise ValueError('LSP worker failed or exceeded its budget: '+str(result.returncode))
    payload=json.loads(result.stdout)['result'];records=[]
    def add(item,uri,range_,name=None):
        parsed=urlsplit(uri)
        if parsed.scheme!='file' or parsed.netloc:return
        candidate=Path(url2pathname(parsed.path)).resolve()
        if index.root not in candidate.parents:return
        filename=candidate.relative_to(index.root).as_posix()
        with closing(index.connect()) as conn:current=conn.execute('SELECT text FROM ci_files WHERE path=?',(filename,)).fetchone()
        if current is None:return
        start=range_['start'];end=range_['end'];source_lines=current['text'].splitlines()
        for point in (start,end):
            if type(point.get('line')) is not int or type(point.get('character')) is not int or point['line']<0 or point['character']<0:raise ValueError('Invalid LSP range')
        if start['line']>=len(source_lines):raise ValueError('LSP location outside indexed source')
        if end['line']>len(source_lines) or (end['line']==len(source_lines) and end['character']!=0) or (end['line'],end['character'])<(start['line'],start['character']):raise ValueError('Invalid LSP end range')
        if end['line']<len(source_lines) and end['character']*2>len(source_lines[end['line']].encode('utf-16-le')):raise ValueError('LSP end outside source')
        raw=source_lines[start['line']].encode('utf-16-le')
        if start['character']*2>len(raw):raise ValueError('LSP character outside source')
        col=len(raw[:start['character']*2].decode('utf-16-le'))
        records.append({'file_path':filename,'line':start['line']+1,'column':col,'name':name,'range_utf16':range_})
    def symbols(items):
        for item in items:
            location=item.get('location',{})
            add(item,location.get('uri',path.as_uri()),item.get('selectionRange') or location.get('range') or item['range'],item['name'])
            symbols(item.get('children',[]))
    if method=='textDocument/documentSymbol':symbols(payload or [])
    else:
        for item in ([payload] if isinstance(payload,dict) else payload or []):add(item,item.get('uri') or item['targetUri'],item.get('range') or item['targetSelectionRange'])
    if len(records)>1000:raise ValueError('LSP locations exceed 1000')
    # Fail closed if source/ignore rules changed while the server was running.
    current=index.inventory()
    if {key:value[0] for key,value in current.items()}!=snapshot:raise ValueError('Workspace changed during navigation request')
    return {'backend':config.get('backend','lsp'),'resolved':True,'results':records}

def navigate(file_path,line,column,references=False):
    index=CodeIndex();index.refresh(embed=False);path=resolve_path(file_path);name=path.relative_to(index.root).as_posix()
    with closing(index.connect()) as conn:row=conn.execute('SELECT language FROM ci_files WHERE path=?',(name,)).fetchone()
    if row is None:raise ValueError('File is excluded or not indexed')
    if row['language'] in servers() or row['language']=='python':return lsp(index,path,'textDocument/references' if references else 'textDocument/definition',line,column)
    return index.navigate(file_path,line,column,references)
