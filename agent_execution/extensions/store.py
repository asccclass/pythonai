"""Immutable, owner-scoped extension proposals; test and activation are separate gates."""
import ast
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import uuid
import re
from ..policy import current_context,policy_tool
from ..tasks import task_owner
from ..approvals import ApprovalStore,ApprovalRequired
from .schema import validator


class ExtensionStore:
    def __init__(self,path=None):
        self.path=Path(path or os.environ.get('AGENT_EXTENSION_DB') or Path(os.environ.get('LOCALAPPDATA',Path.home()))/'PythonAI'/'extensions.db')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self.connect()) as conn,conn:
            conn.executescript('''CREATE TABLE IF NOT EXISTS extension_versions(
                id TEXT PRIMARY KEY,owner TEXT NOT NULL,name TEXT NOT NULL,kind TEXT NOT NULL,
                files TEXT NOT NULL,origin TEXT NOT NULL,digest TEXT NOT NULL,tested INTEGER DEFAULT 0,
                evidence TEXT DEFAULT '',created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS extension_active(owner TEXT,name TEXT,version TEXT,
                PRIMARY KEY(owner,name));
                CREATE TABLE IF NOT EXISTS extension_events(id INTEGER PRIMARY KEY,owner TEXT,name TEXT,
                version TEXT,action TEXT,created REAL);''')
    def connect(self):
        if self.path.exists() and self.path.stat().st_size>64*1024*1024:raise ValueError('Extension DB capacity exceeded')
        conn=sqlite3.connect(self.path,timeout=10);conn.row_factory=sqlite3.Row;return conn
    def stage(self,name,kind,files,origin):
        if not re.fullmatch('[a-z][a-z0-9_]{0,47}',name) or kind not in {'tool','skill'}:raise ValueError('Invalid extension name/kind')
        allowed={'tool.py','schema.json','test_extension.py'} if kind=='tool' else {'skill.json','SKILL.md','test_extension.py'}
        required={'tool.py','schema.json'} if kind=='tool' else {'skill.json','SKILL.md'}
        if not isinstance(files,dict) or not required<=set(files)<=allowed or not all(isinstance(v,str) for v in files.values()):raise ValueError('Invalid package files')
        if not isinstance(origin,str) or not 1<=len(origin)<=1024:raise ValueError('Provenance is required')
        if kind=='tool':
            ast.parse(files['tool.py']);schema=json.loads(files['schema.json']);spec=schema.get('function',schema)
            if spec.get('name')!=name:raise ValueError('Schema name differs from extension name')
            validator(spec['parameters'])
        else:
            from skills import load_skill
            with tempfile.TemporaryDirectory() as directory:
                for file in required:(Path(directory)/file).write_text(files[file],encoding='utf-8')
                if load_skill(Path(directory)).name!=name:raise ValueError('Skill name mismatch')
        if 'test_extension.py' in files:ast.parse(files['test_extension.py'])
        data=json.dumps(files,sort_keys=True,ensure_ascii=False,allow_nan=False)
        if len(data.encode())>1024*1024:raise ValueError('Package exceeds 1 MiB')
        key=uuid.uuid4().hex;owner=task_owner(current_context());digest=hashlib.sha256(data.encode()).hexdigest()
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT count(*) FROM extension_versions').fetchone()[0]>=1000:raise ValueError('Extension version capacity reached')
            conn.execute('INSERT INTO extension_versions(id,owner,name,kind,files,origin,digest,created) VALUES(?,?,?,?,?,?,?,?)',(key,owner,name,kind,data,origin,digest,time.time()))
        return key
    def get(self,key):
        with closing(self.connect()) as conn:
            row=conn.execute('SELECT * FROM extension_versions WHERE id=? AND owner=?',(key,task_owner(current_context()))).fetchone()
        if not row:raise PermissionError('Extension not found for owner')
        value=dict(row)
        if hashlib.sha256(value['files'].encode()).hexdigest()!=value['digest']:raise ValueError('Extension content was modified')
        return value
    def test(self,key):
        value=self.get(key);files=json.loads(value['files'])
        if 'test_extension.py' not in files:raise ValueError('Add tests as a new version before testing')
        from sandbox import SandboxExecution
        with tempfile.TemporaryDirectory(prefix='pythonai-extension-') as directory:
            root=Path(directory)
            for name,text in files.items():(root/name).write_text(text,encoding='utf-8')
            sandbox=SandboxExecution(root)
            try:
                sandbox.setup()
                result=sandbox.run(['python','-m','unittest','-v','test_extension'],timeout=60,max_output_bytes=65536)
            finally:sandbox.cleanup()
        evidence=(result.stdout+'\n'+result.stderr)[-65536:]
        passed=result.returncode==0 and re.search(r'Ran [1-9]\d* tests? in ',evidence) is not None and re.search(r'(?m)^OK$',evidence) is not None
        with closing(self.connect()) as conn,conn:
            conn.execute('UPDATE extension_versions SET tested=?,evidence=? WHERE id=? AND digest=?',(int(passed),evidence,key,value['digest']))
        return {'passed':passed,'evidence':evidence,'digest':value['digest']}
    def activate(self,key,approval_id=None):
        from permissions import current_role
        if current_role()!='admin':raise PermissionError('Extension activation requires admin')
        value=self.get(key)
        if not value['tested']:raise ValueError('Untested/failed extension cannot be activated')
        context=current_context();action=f'{context.project}:extension.activate'
        arguments={'version':key,'digest':value['digest'],'name':value['name'],'kind':value['kind']}
        approvals=ApprovalStore()
        if not approval_id:raise ApprovalRequired(approvals.request(context.identity,context.session,action,arguments))
        approvals.consume(approval_id,context.identity,context.session,action,arguments)
        owner=task_owner(context)
        with closing(self.connect()) as conn,conn:
            conn.execute('INSERT INTO extension_active VALUES(?,?,?) ON CONFLICT(owner,name) DO UPDATE SET version=excluded.version',(owner,value['name'],key))
            conn.execute('INSERT INTO extension_events(owner,name,version,action,created) VALUES(?,?,?,?,?)',(owner,value['name'],key,'activate',time.time()))
        return key
    def disable(self,name):
        from permissions import current_role
        if current_role()!='admin':raise PermissionError('Extension disable requires admin')
        owner=task_owner(current_context())
        with closing(self.connect()) as conn,conn:
            conn.execute('DELETE FROM extension_active WHERE owner=? AND name=?',(owner,name))
            conn.execute('INSERT INTO extension_events(owner,name,action,created) VALUES(?,?,?,?)',(owner,name,'disable',time.time()))
    def active(self,kind=None):
        with closing(self.connect()) as conn:
            keys=[row[0] for row in conn.execute('SELECT version FROM extension_active WHERE owner=?',(task_owner(current_context()),))]
        values=[self.get(key) for key in keys]
        return [value for value in values if value['tested'] and (kind is None or value['kind']==kind)]
    def tools(self):
        items=[]
        for value in self.active('tool'):
            files=json.loads(value['files']);spec=json.loads(files['schema.json']);spec=spec.get('function',spec)
            name=value['name'];key=value['id'];owner=value['owner'];namespace={}
            exec(compile(files['tool.py'],f'<approved-extension:{key}>','exec'),namespace)
            function=namespace.get(name)
            if not callable(function):raise ValueError('Approved tool lacks named callable')
            def bind(function,key,owner,schema):
                import inspect
                from .schema import validate
                def bound(**kwargs):
                    if task_owner(current_context())!=owner:raise PermissionError('Extension owner mismatch')
                    self.get(key)
                    if not any(v['id']==key for v in self.active('tool')):raise PermissionError('Extension disabled or replaced')
                    validate(kwargs,schema)
                    return function(**kwargs)
                bound.__signature__=inspect.signature(function)
                return bound
            items.append((name,policy_tool(name)(bind(function,key,owner,spec['parameters'])),{'type':'function','function':spec}))
        return items

    def skills(self):
        from skills import load_skill
        skills=[]
        for value in self.active('skill'):
            directory=self.path.parent/'extension-assets'/value['digest']
            directory.mkdir(parents=True,exist_ok=True)
            if not directory.resolve().is_relative_to(self.path.parent.resolve()):raise PermissionError('Extension asset path escape')
            for name,text in json.loads(value['files']).items():
                target=directory/name
                if target.is_symlink() or not target.resolve().is_relative_to(directory.resolve()):raise PermissionError('Extension asset path escape')
                if target.exists() and target.read_text(encoding='utf-8')!=text:raise ValueError('Extension assets modified')
                if not target.exists():target.write_text(text,encoding='utf-8')
            skills.append(load_skill(directory))
        return skills
