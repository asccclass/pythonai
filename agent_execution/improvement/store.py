from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
from ..policy import current_context
from ..tasks import task_owner
from ..approvals import ApprovalStore,ApprovalRequired


def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()


class ImprovementStore:
    def __init__(self,path=None):
        self.path=Path(path or os.environ.get('AGENT_IMPROVEMENT_DB') or Path(os.environ.get('LOCALAPPDATA',Path.home()))/'PythonAI'/'improvements.db')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self.connect()) as conn,conn:
            conn.executescript('''CREATE TABLE IF NOT EXISTS improvement_versions(id TEXT PRIMARY KEY,owner TEXT,name TEXT,
                kind TEXT,content TEXT,source TEXT,digest TEXT,evaluation TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS improvement_active(owner TEXT,name TEXT,version TEXT,PRIMARY KEY(owner,name));
                CREATE TABLE IF NOT EXISTS improvement_events(id INTEGER PRIMARY KEY,owner TEXT,name TEXT,version TEXT,action TEXT,created REAL);''')
    def connect(self):
        if self.path.exists() and self.path.stat().st_size>64*1024*1024:raise ValueError('Improvement DB capacity')
        conn=sqlite3.connect(self.path,timeout=10);conn.row_factory=sqlite3.Row;return conn
    def propose(self,name,content,source,kind='persona'):
        if not re.fullmatch('[a-z][a-z0-9_]{0,47}',name) or kind not in {'persona','skill'}:raise ValueError('Invalid improvement name/kind')
        if kind=='persona':
            if not isinstance(content,list) or not 1<=len(content)<=20 or not all(isinstance(v,dict) and set(v)=={'subject','predicate','object'} and all(isinstance(x,str) and 0<len(x)<=1000 for x in v.values()) for v in content):raise ValueError('Invalid persona proposal')
        else:
            from ..extensions.store import ExtensionStore
            if not isinstance(content,dict) or set(content)!={'extension_version'}:raise ValueError('Skill proposal references an extension version')
            extension=ExtensionStore().get(content['extension_version'])
            if extension['kind']!='skill':raise ValueError('Proposal must reference a skill')
        if not isinstance(source,str) or not 1<=len(source)<=8192:raise ValueError('Improvement source required')
        data=json.dumps(content,ensure_ascii=False,sort_keys=True)
        if len(data.encode())>8192:raise ValueError('Persona content exceeds 8 KiB')
        key=uuid.uuid4().hex;owner=task_owner(current_context())
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT count(*) FROM improvement_versions').fetchone()[0]>=1000:raise ValueError('Improvement version capacity')
            conn.execute('INSERT INTO improvement_versions VALUES(?,?,?,?,?,?,?,?,?)',(key,owner,name,kind,data,source,digest(content),None,time.time()))
        return key
    def get(self,key):
        with closing(self.connect()) as conn:row=conn.execute('SELECT * FROM improvement_versions WHERE id=? AND owner=?',(key,task_owner(current_context()))).fetchone()
        if not row:raise PermissionError('Improvement not found for owner')
        value=dict(row);value['content']=json.loads(value['content'])
        if value['digest']!=digest(value['content']):raise ValueError('Improvement content changed')
        value['evaluation']=json.loads(value['evaluation']) if value['evaluation'] else None
        return value
    def active(self):
        with closing(self.connect()) as conn:keys=[row[0] for row in conn.execute('SELECT version FROM improvement_active WHERE owner=?',(task_owner(current_context()),))]
        return [self.get(key) for key in keys]
    def snapshot(self):return {v['name']:v['digest'] for v in self.active()}
    def evaluate(self,key,cases,runner):
        from permissions import current_role
        if current_role()!='admin':raise PermissionError('Trusted improvement evaluation requires admin')
        value=self.get(key)
        if not isinstance(cases,list) or not 5<=len(cases)<=100 or any(not isinstance(case,dict) or not isinstance(case.get('id'),str) for case in cases) or len({case['id'] for case in cases})!=len(cases):raise ValueError('Expected 5..100 unique fixed benchmark cases')
        if len(json.dumps(cases).encode())>65536:raise ValueError('Benchmark capacity exceeded')
        baseline=self.snapshot();existing={v['name']:v['content'] for v in self.active()};candidate={**existing,value['name']:value['content']};observations=[]
        for case in cases:
            pair=[]
            for label,proposals in [('baseline',existing),('candidate',candidate)]:
                started=time.monotonic();result=runner(case,proposals)
                if not isinstance(result,dict) or type(result.get('verified')) is not bool or type(result.get('unexpected_side_effects')) is not int or not 0<=result['unexpected_side_effects']<=1000:raise ValueError('Benchmark runner must return verified evidence and side-effect count')
                if not isinstance(result.get('evidence'),str) or not 1<=len(result['evidence'])<=4096:raise ValueError('Benchmark evidence required')
                pair.append({'label':label,**result,'latency':time.monotonic()-started})
            observations.append({'id':case['id'],'results':pair})
        base=sum(row['results'][0]['verified'] for row in observations)/len(cases)
        score=sum(row['results'][1]['verified'] for row in observations)/len(cases)
        regressions=sum(row['results'][0]['verified'] and not row['results'][1]['verified'] for row in observations)
        side_effects=sum(row['results'][1]['unexpected_side_effects'] for row in observations)
        report={'suite_digest':digest(cases),'baseline_snapshot':baseline,'baseline_success':base,'candidate_success':score,
            'regressions':regressions,'unexpected_side_effects':side_effects,'observations':observations,
            'passed':score>base and regressions==0 and side_effects==0,'proposal_digest':value['digest']}
        with closing(self.connect()) as conn,conn:conn.execute('UPDATE improvement_versions SET evaluation=? WHERE id=?',(json.dumps(report),key))
        return report
    def activate(self,key,approval_id=None,rollback=False):
        from permissions import current_role
        if current_role()!='admin':raise PermissionError('Improvement activation requires admin')
        value=self.get(key);report=value['evaluation']
        if not report or not report['passed'] or report['proposal_digest']!=value['digest']:raise ValueError('Improvement requires a passing A/B evaluation')
        if rollback:
            with closing(self.connect()) as conn:
                if not conn.execute("SELECT 1 FROM improvement_events WHERE owner=? AND version=? AND action='activate'",(value['owner'],key)).fetchone():raise ValueError('Rollback requires a previously approved version')
        elif report['baseline_snapshot']!=self.snapshot():raise ValueError('Improvement requires a fresh A/B baseline')
        if value['kind']=='skill':raise ValueError('After A/B review, activate the referenced tested Skill through the extension approval gate')
        context=current_context();operation='rollback' if rollback else 'activate';action=f'{context.project}:improvement.{operation}';arguments={'version':key,'digest':value['digest'],'evaluation_digest':digest(report)}
        approvals=ApprovalStore()
        if not approval_id:raise ApprovalRequired(approvals.request(context.identity,context.session,action,arguments))
        approvals.consume(approval_id,context.identity,context.session,action,arguments)
        owner=task_owner(context)
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            if not rollback and report['baseline_snapshot']!=self.snapshot():raise ValueError('A/B baseline changed before activation')
            if conn.execute('SELECT count(*) FROM improvement_active WHERE owner=?',(owner,)).fetchone()[0]>=20 and value['name'] not in self.snapshot():raise ValueError('Active improvement capacity reached')
            conn.execute('INSERT INTO improvement_active VALUES(?,?,?) ON CONFLICT(owner,name) DO UPDATE SET version=excluded.version',(owner,value['name'],key))
            conn.execute('INSERT INTO improvement_events(owner,name,version,action,created) VALUES(?,?,?,?,?)',(owner,value['name'],key,operation,time.time()))
    def revoke(self,name):
        from permissions import current_role
        if current_role()!='admin':raise PermissionError('Improvement revocation requires admin')
        owner=task_owner(current_context())
        with closing(self.connect()) as conn,conn:
            conn.execute('DELETE FROM improvement_active WHERE owner=? AND name=?',(owner,name))
            conn.execute('INSERT INTO improvement_events(owner,name,action,created) VALUES(?,?,?,?)',(owner,name,'revoke',time.time()))
    def guidance(self):
        lines=[];size=0
        for version in self.active():
            if version['kind']!='persona':continue
            for rule in version['content']:
                line=' '.join(rule.values());size+=len(line.encode())
                if size>8192:return '\n'.join(lines)
                lines.append(f"[reviewed-version:{version['id']}] {line}")
        return '\n'.join(lines)
