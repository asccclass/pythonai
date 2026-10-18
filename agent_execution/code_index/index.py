"""Transactional file synchronization, exact text search and persistent LSH candidates."""
from contextlib import closing
from functools import lru_cache
from importlib.metadata import version as package_version
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time

from .syntax import extract,LANGUAGES
from ..policy import resolve_path,current_context

HARD_DIRS={'.git','.venv','venv','node_modules','__pycache__','.pytest_cache','.agent','.codex','temp','models','dist','build','.pythonai-workspace-trash'}
TEXT_EXT=set(LANGUAGES)|{'.md','.txt','.json','.yaml','.yml','.toml','.ini','.html','.css','.sql','.sh','.ps1'}
SECRET=re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bAKIA[A-Z0-9]{16}\b|\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,})\b|(?:api[_-]?key|password|secret|(?:access[_-]?|auth[_-]?)?token)[\"\']?\s*[:=]\s*(?:[\"\'][^\"\'\r\n]{8,}[\"\']|[A-Za-z0-9_+/=-]{12,})',re.I)

def check():
    from ..budgets import active_budget
    budget=active_budget()
    if budget:budget.check()
    callback=current_context().check_cancelled
    if callback and callback():raise InterruptedError('Index operation cancelled')

def hard_excluded(path):
    parts=[part.casefold() for part in Path(path).parts];name=parts[-1]
    return any(part.rstrip(' .') in HARD_DIRS for part in parts) or ':' in path or name.startswith('.env') or name.lstrip('.').startswith(('id_rsa','id_ed25519','credential','secrets')) or Path(name).suffix in {'.pem','.key','.p12','.pfx','.db'}

def fingerprint(value):return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()

class Embeddings:
    def __init__(self):
        self.config=json.loads(os.environ.get('AGENT_CODE_EMBEDDING_CONFIG','{"kind":"hashing","dimensions":64,"revision":"1"}'))
        cfg=self.config
        if not isinstance(cfg,dict) or set(cfg)-{'kind','dimensions','revision','model','max_requests'} or cfg.get('kind') not in {'hashing','openai'}:raise ValueError('Invalid code embedding configuration')
        if type(cfg.get('dimensions')) is not int or not 8<=cfg['dimensions']<=2048 or not isinstance(cfg.get('revision'),str) or not cfg['revision']:raise ValueError('Embedding dimensions/revision required')
        self.maximum=cfg.get('max_requests',32)
        if type(self.maximum) is not int or not 0<=self.maximum<=32:raise ValueError('Embedding request limit is 0..32')
        self.client=None
        if cfg['kind']=='openai':
            if not isinstance(cfg.get('model'),str) or not cfg['model']:raise ValueError('Remote embedding model required')
            from server import get_client
            from ..budgets import budget_client
            self.client=budget_client(get_client().with_options(timeout=10,max_retries=0))
            cfg={**cfg,'endpoint':str(self.client.base_url)}
        self.version=fingerprint({key:value for key,value in cfg.items() if key!='max_requests'});self.dimensions=cfg['dimensions']

    def embed(self,text):
        check()
        if self.client:
            result=self.client.embeddings.create(model=self.config['model'],input=text)
            vector=result.data[0].embedding
        else:
            from vector_search import HashingEmbeddingProvider
            vector=HashingEmbeddingProvider(self.dimensions).embed(text)
        if not isinstance(vector,list) or len(vector)!=self.dimensions or any(type(x) not in {int,float} or not math.isfinite(x) for x in vector):raise ValueError('Invalid embedding dimensions/values')
        norm=math.sqrt(sum(x*x for x in vector))
        return [float(x)/norm for x in vector] if norm else [0.0]*len(vector)

@lru_cache(maxsize=8)
def projections(dimensions):
    return [[[1 if hashlib.sha256(f'{table}:{bit}:{dimension}'.encode()).digest()[0]&1 else -1 for dimension in range(dimensions)] for bit in range(8)] for table in range(6)]

def buckets(vector):
    # Six independent deterministic random-sign projections, eight bits each.
    result=[]
    for table in range(6):
        signature=0
        for bit in range(8):
            score=sum(value*sign for value,sign in zip(vector,projections(len(vector))[table][bit]))
            signature|=(score>=0)<<bit
        result.append((table,signature))
    return result

class CodeIndex:
    def __init__(self, initialize_embeddings=True):
        self.root=resolve_path('.');self.embeddings=Embeddings() if initialize_embeddings else None
        directory=os.environ.get('AGENT_CODE_INDEX_DIR')
        context=current_context()
        from ..workspace_lifecycle.store import principal
        self.principal=principal()
        private_scope=self.principal!=json.dumps(['local','local','default'],ensure_ascii=False)
        if not directory and private_scope:
            from ..tasks import TaskStore
            directory=str(TaskStore().path.parent/'code_index')
        if not directory and context.repository_workspace and Path(context.repository_workspace).resolve()!=self.root:
            from ..tasks import TaskStore
            directory=str(TaskStore().path.parent/'code_index')
        if directory:
            storage=Path(directory)
            if not storage.is_absolute():raise ValueError('Code index storage requires an absolute directory')
            storage=storage.resolve()
            if storage==self.root or self.root in storage.parents:raise ValueError('External code index storage must be outside workspace')
            storage.mkdir(parents=True,exist_ok=True)
            self.path=storage/(fingerprint([os.path.normcase(str(self.root)),self.principal])+'.db')
        else:self.path=resolve_path('codebase_index.db')
        self.stats={}

    def connect(self):
        from ..workspace_lifecycle.store import principal
        if principal()!=self.principal:raise PermissionError('Code index belongs to a different owner')
        if self.path.exists() and self.path.stat().st_size>256*1024*1024:raise ValueError('Code index exceeds 256 MiB')
        conn=sqlite3.connect(self.path,timeout=10);conn.row_factory=sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    def inventory(self):
        from pathspec import GitIgnoreSpec
        from ..workspace_lifecycle import LifecycleStore
        blocked=LifecycleStore().inaccessible_paths()
        files={};total=0;visited=0;stack=[(self.root,[])]
        while stack:
            folder,rules=stack.pop();check()
            for filename in ('.gitignore','.agentignore'):
                rule=folder/filename
                if rule.is_file() and not rule.is_symlink() and rule.stat().st_size<=65536:
                    rules=[*rules,(folder,GitIgnoreSpec.from_lines(rule.read_text(encoding='utf-8').splitlines()))]
            for item in sorted(folder.iterdir()):
                visited+=1
                if visited>20000:raise ValueError('Index inventory exceeds 20000 paths')
                name=item.relative_to(self.root).as_posix()
                if any(item.resolve()==p or item.resolve().is_relative_to(p) for p in blocked):continue
                if hard_excluded(name) or item.is_symlink() or (hasattr(item,'is_junction') and item.is_junction()):continue
                ignored=False
                for base,spec in rules:
                    local=item.relative_to(base).as_posix()+('/' if item.is_dir() else '')
                    match=spec.check_file(local)
                    if match.include is not None:ignored=match.include
                if ignored:continue
                if item.is_dir():stack.append((item,rules));continue
                if not item.is_file() or item.suffix.lower() not in TEXT_EXT or item.stat().st_size>1024*1024:continue
                if self.root not in item.resolve().parents:continue
                data=item.read_bytes()
                if len(data)>1024*1024 or b'\x00' in data:continue
                try:text=data.decode('utf-8')
                except UnicodeDecodeError:continue
                if SECRET.search(text):continue
                total+=len(data)
                if len(files)>=10000 or total>64*1024*1024:raise ValueError('Code inventory exceeds 10000 files / 64 MiB')
                files[name]=(hashlib.sha256(data).hexdigest(),text)
        return files

    def refresh(self,embed=True):
        started=time.perf_counter();files=self.inventory();stats={'changed':0,'deleted':0,'unchanged':0,'parse_errors':0,'embedding_requests':0,'embedding_failures':0}
        with closing(self.connect()) as conn,conn:
            conn.executescript('''CREATE TABLE IF NOT EXISTS ci_meta(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS ci_files(path TEXT PRIMARY KEY,hash TEXT NOT NULL,text TEXT NOT NULL,language TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS ci_symbols(id INTEGER PRIMARY KEY,path TEXT REFERENCES ci_files(path) ON DELETE CASCADE,name TEXT,qualified TEXT,kind TEXT,line INTEGER,col INTEGER,end_line INTEGER,chunk TEXT,vector TEXT);
CREATE TABLE IF NOT EXISTS ci_refs(path TEXT REFERENCES ci_files(path) ON DELETE CASCADE,name TEXT,line INTEGER,col INTEGER,scope TEXT,declaration INTEGER);
CREATE TABLE IF NOT EXISTS ci_buckets(symbol INTEGER REFERENCES ci_symbols(id) ON DELETE CASCADE,tab INTEGER,sig INTEGER);
CREATE INDEX IF NOT EXISTS ci_bucket_lookup ON ci_buckets(tab,sig);
CREATE INDEX IF NOT EXISTS ci_symbol_name ON ci_symbols(name);
CREATE INDEX IF NOT EXISTS ci_ref_name ON ci_refs(name);
CREATE INDEX IF NOT EXISTS ci_symbol_path ON ci_symbols(path);
CREATE INDEX IF NOT EXISTS ci_ref_path ON ci_refs(path);''')
            conn.execute('BEGIN IMMEDIATE')
            # Never consume or silently migrate unversioned legacy vectors.
            conn.execute('DROP TABLE IF EXISTS code_index')
            metadata=dict(conn.execute('SELECT key,value FROM ci_meta').fetchall())
            grammar_versions={name:package_version(name) for name in ('tree-sitter','tree-sitter-python','tree-sitter-go','tree-sitter-javascript','tree-sitter-typescript','pathspec')}
            version=fingerprint({'schema':1,'parser':1,'grammars':grammar_versions,'workspace':str(self.root),'embedding':self.embeddings.version})
            if metadata.get('version')!=version:
                conn.execute('DELETE FROM ci_files');conn.execute('DELETE FROM ci_meta');conn.execute('INSERT INTO ci_meta VALUES(?,?)',('version',version))
            self.version=version
            old={row['path']:row['hash'] for row in conn.execute('SELECT path,hash FROM ci_files')}
            for name in old.keys()-files.keys():conn.execute('DELETE FROM ci_files WHERE path=?',(name,));stats['deleted']+=1
            for name,(sha,text) in files.items():
                check()
                if old.get(name)==sha:stats['unchanged']+=1;continue
                conn.execute('DELETE FROM ci_files WHERE path=?',(name,))
                language=LANGUAGES.get(Path(name).suffix.lower(),'text');status='indexed'
                try:symbols,refs=extract(name,text.encode())
                except (ValueError,ImportError):symbols,refs=[],[];status='parse_error';stats['parse_errors']+=1
                if symbols and len(symbols)+conn.execute('SELECT COUNT(*) FROM ci_symbols').fetchone()[0]>20000:raise ValueError('Code index exceeds 20000 symbols')
                if refs and len(refs)+conn.execute('SELECT COUNT(*) FROM ci_refs').fetchone()[0]>200000:raise ValueError('Code index exceeds 200000 references')
                conn.execute('INSERT INTO ci_files VALUES(?,?,?,?,?)',(name,sha,text,language,status));stats['changed']+=1
                for symbol in symbols:
                    chunk=f"File: {name}\nLanguage: {language}\nSymbol: {symbol['qualified_name']}\n{symbol['text']}"
                    conn.execute('INSERT INTO ci_symbols(path,name,qualified,kind,line,col,end_line,chunk) VALUES(?,?,?,?,?,?,?,?)',
                        (name,symbol['name'],symbol['qualified_name'],symbol['node_type'],symbol['line'],symbol['column'],symbol['end_line'],chunk))
                conn.executemany('INSERT INTO ci_refs VALUES(?,?,?,?,?,?)',[(name,r['name'],r['line'],r['column'],r['scope'],r['declaration']) for r in refs])
                if conn.execute('PRAGMA page_count').fetchone()[0]*conn.execute('PRAGMA page_size').fetchone()[0]>256*1024*1024:raise ValueError('Code index exceeds 256 MiB')
            if embed:
                maximum=self.embeddings.maximum if self.embeddings.client else 100000
                for row in conn.execute('SELECT id,chunk FROM ci_symbols WHERE vector IS NULL ORDER BY id LIMIT ?',(maximum,)).fetchall():
                    check();stats['embedding_requests']+=1
                    try:vector=self.embeddings.embed(row['chunk'])
                    except Exception as error:
                        from ..budgets import BudgetExhausted
                        if isinstance(error,(BudgetExhausted,InterruptedError)):raise
                        stats['embedding_failures']+=1;stats['embedding_error']=type(error).__name__
                        if self.embeddings.client:break
                        continue
                    conn.execute('UPDATE ci_symbols SET vector=? WHERE id=?',(json.dumps(vector),row['id']))
                    conn.executemany('INSERT INTO ci_buckets VALUES(?,?,?)',[(row['id'],tab,sig) for tab,sig in buckets(vector)])
                    if conn.execute('PRAGMA page_count').fetchone()[0]*conn.execute('PRAGMA page_size').fetchone()[0]>256*1024*1024:raise ValueError('Code index exceeds 256 MiB')
            stats['files']=conn.execute('SELECT COUNT(*) FROM ci_files').fetchone()[0]
            stats['symbols']=conn.execute('SELECT COUNT(*) FROM ci_symbols').fetchone()[0]
            stats['pending_vectors']=conn.execute('SELECT COUNT(*) FROM ci_symbols WHERE vector IS NULL').fetchone()[0]
            if stats['symbols']>20000 or conn.execute('SELECT COUNT(*) FROM ci_refs').fetchone()[0]>200000:raise ValueError('Code index exceeds 20000 symbols / 200000 references')
            if conn.execute('PRAGMA page_count').fetchone()[0]*conn.execute('PRAGMA page_size').fetchone()[0]>256*1024*1024:raise ValueError('Code index exceeds 256 MiB')
            if {name:value[0] for name,value in self.inventory().items()}!={name:value[0] for name,value in files.items()}:raise ValueError('Workspace changed while indexing; retry a fresh snapshot')
        stats['elapsed_ms']=round((time.perf_counter()-started)*1000,3);self.stats=stats;return stats

    def search(self,query,limit=5,mode='semantic'):
        if not isinstance(query,str) or not 0<len(query)<=2000 or type(limit) is not int or not 1<=limit<=100 or mode not in {'semantic','text','symbol'}:raise ValueError('Invalid code search arguments')
        started=time.perf_counter();self.refresh(embed=mode=='semantic' and not self.embeddings.client)
        results=[];backend=mode;candidate_count=0
        with closing(self.connect()) as conn:
            conn.execute('BEGIN')
            if dict(conn.execute('SELECT key,value FROM ci_meta')).get('version')!=self.version:raise ValueError('Index version changed during search; retry')
            snapshot={row['path']:row['hash'] for row in conn.execute('SELECT path,hash FROM ci_files')}
            if mode=='text':
                for row in conn.execute('SELECT path,text FROM ci_files ORDER BY path'):
                    check()
                    for line,text in enumerate(row['text'].splitlines(),1):
                        if query in text:results.append({'file_path':row['path'],'line':line,'column':text.index(query),'text':text[:1000]})
                        if len(results)>=limit:break
                    if len(results)>=limit:break
            elif mode=='symbol':
                results=[dict(row) for row in conn.execute('SELECT path AS file_path,name,qualified,kind,line,col AS column,end_line FROM ci_symbols WHERE name=? OR qualified=? ORDER BY path,line LIMIT ?',(query,query,limit))]
            else:
                count=conn.execute('SELECT COUNT(*) FROM ci_symbols WHERE vector IS NOT NULL').fetchone()[0]
                vector=self.embeddings.embed(query) if count else [0.0]*self.embeddings.dimensions
                if count<=256:rows=conn.execute('SELECT * FROM ci_symbols WHERE vector IS NOT NULL').fetchall();backend='exact_cosine'
                else:
                    keys=[(tab,sig^(1<<bit)) for tab,sig in buckets(vector) for bit in range(8)]+buckets(vector)
                    where=' OR '.join('(tab=? AND sig=?)' for _ in keys)
                    ids=conn.execute('SELECT symbol,COUNT(*) AS hits FROM ci_buckets WHERE '+where+' GROUP BY symbol ORDER BY hits DESC,symbol LIMIT 512',sum(([tab,sig] for tab,sig in keys),[])).fetchall()
                    rows=conn.execute('SELECT * FROM ci_symbols WHERE id IN ('+','.join('?' for _ in ids)+')',[row['symbol'] for row in ids]).fetchall() if ids else [];backend='lsh_approximate'
                candidate_count=len(rows)
                for row in rows:
                    score=sum(a*b for a,b in zip(vector,json.loads(row['vector'])))
                    results.append({'file_path':row['path'],'name':row['name'],'qualified':row['qualified'],'line':row['line'],'end_line':row['end_line'],'score':score})
                results.sort(key=lambda row:(-row['score'],row['file_path'],row['line']));results=results[:limit]
            if {name:value[0] for name,value in self.inventory().items()}!=snapshot:raise ValueError('Workspace changed during search; retry')
        return {'results':results,'backend':backend,'approximate':backend=='lsh_approximate','candidate_count':candidate_count,'embedding_version':self.embeddings.version,'index':self.stats,'elapsed_ms':round((time.perf_counter()-started)*1000,3)}

    def navigate(self,file_path,line,column,references=False):
        self.refresh(embed=False);path=resolve_path(file_path);name=path.relative_to(self.root).as_posix()
        if type(line) is not int or line<1 or type(column) is not int or column<0:raise ValueError('Invalid source position')
        with closing(self.connect()) as conn:
            conn.execute('BEGIN')
            snapshot={row['path']:row['hash'] for row in conn.execute('SELECT path,hash FROM ci_files')}
            language_row=conn.execute('SELECT language FROM ci_files WHERE path=?',(name,)).fetchone()
            if language_row is None:raise ValueError('File is excluded or not indexed')
            language=language_row['language'];family={'javascript','typescript','tsx'} if language in {'javascript','typescript','tsx'} else {language}
            token=conn.execute('SELECT * FROM ci_refs WHERE path=? AND line=? AND col<=? ORDER BY col DESC LIMIT 1',(name,line,column)).fetchone()
            if token is None or column>token['col']+len(token['name']):raise ValueError('Position is not an indexed identifier')
            condition=' AND path IN (SELECT path FROM ci_files WHERE language IN ('+','.join('?' for _ in family)+'))'
            args=[token['name'],*sorted(family)]
            if references:rows=conn.execute('SELECT path AS file_path,line,col AS column,declaration FROM ci_refs WHERE name=?'+condition+' ORDER BY path,line,col LIMIT 1000',args)
            else:rows=conn.execute('SELECT path AS file_path,line,col AS column,qualified,kind FROM ci_symbols WHERE name=?'+condition+' ORDER BY path,line LIMIT 1000',args)
            results=[dict(row) for row in rows]
            if {key:value[0] for key,value in self.inventory().items()}!=snapshot:raise ValueError('Workspace changed during navigation; retry')
            return {'backend':'syntax_candidates','resolved':False,'name':token['name'],'results':results}
