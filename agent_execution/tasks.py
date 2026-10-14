"""Persistent task checkpoints with lease fencing and conservative crash recovery."""
from contextlib import closing
from contextvars import ContextVar
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid


class TaskBusy(RuntimeError):
    pass


class RecoveryRequired(RuntimeError):
    pass


def task_owner(context):
    return json.dumps([context.source, context.identity, context.project, context.session], separators=(',', ':'))


class TaskStore:
    def __init__(self, path=None):
        root = Path(os.environ.get('LOCALAPPDATA') or Path.home()/'.local'/'share')/'PythonAI'
        self.path = Path(path or os.environ.get('AGENT_TASK_DB') or root/'tasks.db')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn, conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS tasks (
                    owner TEXT NOT NULL, id TEXT NOT NULL, goal TEXT NOT NULL, criteria TEXT NOT NULL,
                    state TEXT NOT NULL, phase TEXT NOT NULL, checkpoint TEXT NOT NULL,
                    episode INTEGER, reply TEXT, lease_owner TEXT, lease_until REAL,
                    attempts INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL, PRIMARY KEY(owner,id));
                CREATE TABLE IF NOT EXISTS task_steps (
                    owner TEXT NOT NULL, task_id TEXT NOT NULL, id TEXT NOT NULL,
                    name TEXT NOT NULL, input TEXT NOT NULL, dependencies TEXT NOT NULL,
                    state TEXT NOT NULL, result TEXT, PRIMARY KEY(owner,task_id,id));
                CREATE TABLE IF NOT EXISTS task_workspaces (
                    owner TEXT NOT NULL, task_id TEXT NOT NULL, repository TEXT NOT NULL,
                    storage TEXT NOT NULL, PRIMARY KEY(owner,task_id));
            ''')
            conn.execute('BEGIN IMMEDIATE')
            columns = {r['name'] for r in conn.execute('PRAGMA table_info(tasks)')}
            if 'recoveries' not in columns:
                conn.execute('ALTER TABLE tasks ADD COLUMN recoveries INTEGER NOT NULL DEFAULT 0')
            if 'verification' not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN verification TEXT NOT NULL DEFAULT 'unverified'")

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def capacity(conn, extra):
        from .delivery import positive_limit, CapacityError
        used = conn.execute('SELECT COALESCE(SUM(length(CAST(checkpoint AS BLOB))+length(CAST(criteria AS BLOB))+length(CAST(goal AS BLOB))+length(CAST(COALESCE(reply,\'\') AS BLOB))),0) FROM tasks').fetchone()[0]
        used += conn.execute('SELECT COALESCE(SUM(length(CAST(input AS BLOB))+length(CAST(dependencies AS BLOB))+length(CAST(COALESCE(result,\'\') AS BLOB))),0) FROM task_steps').fetchone()[0]
        used += conn.execute('SELECT COALESCE(SUM(length(CAST(repository AS BLOB))+length(CAST(storage AS BLOB))),0) FROM task_workspaces').fetchone()[0]
        if used+extra > positive_limit('AGENT_TASK_STORAGE_BYTES',256*1024*1024):
            raise CapacityError('Task storage capacity reached')

    def get(self, owner, key):
        with closing(self.connect()) as conn:
            row = conn.execute('SELECT * FROM tasks WHERE owner=? AND id=?', (owner,key)).fetchone()
        if row is None:
            raise KeyError(key)
        value = dict(row)
        value['checkpoint'] = json.loads(value['checkpoint'])
        value['criteria'] = json.loads(value['criteria'])
        return value

    def list(self, owner):
        with closing(self.connect()) as conn:
            return [dict(row) for row in conn.execute('SELECT id,goal,state,phase,episode,attempts,updated,verification FROM tasks WHERE owner=? ORDER BY updated DESC LIMIT 20', (owner,))]

    def workspace_binding(self,owner,key):
        self.get(owner,key)
        with closing(self.connect()) as conn:
            row=conn.execute('SELECT repository,storage FROM task_workspaces WHERE owner=? AND task_id=?',(owner,key)).fetchone()
        return dict(row) if row else None

    def bind_workspace(self,owner,key,token,repository,storage):
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT state,lease_owner FROM tasks WHERE owner=? AND id=?',(owner,key)).fetchone()
            if row is None or row['state']!='running' or row['lease_owner']!=token:raise TaskBusy('Lost task lease')
            if conn.execute('SELECT 1 FROM task_workspaces WHERE owner=? AND task_id=?',(owner,key)).fetchone() is None:
                self.capacity(conn,len(str(repository).encode())+len(str(storage).encode()))
            conn.execute('INSERT OR IGNORE INTO task_workspaces VALUES(?,?,?,?)',(owner,key,str(repository),str(storage)))
        return self.workspace_binding(owner,key)

    def claim(self, owner, key, goal, messages, episode=None, criteria=None):
        if not key or len(key)>256:
            raise ValueError('Task ID must contain 1..256 characters')
        token = uuid.uuid4().hex
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM tasks WHERE owner=? AND id=?', (owner,key)).fetchone()
            if row is None:
                from .delivery import positive_limit, CapacityError
                if conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] >= positive_limit('AGENT_TASK_LIMIT',10000):
                    raise CapacityError('Task record capacity reached')
                initial = json.dumps({'messages':messages})
                if len(initial.encode()) > 64*1024*1024:
                    raise ValueError('Task checkpoint exceeds 64 MiB')
                self.capacity(conn,len(initial.encode())+len(goal.encode())+len(json.dumps(criteria or {'goal':goal}).encode()))
                conn.execute('INSERT INTO tasks(owner,id,goal,criteria,state,phase,checkpoint,episode,updated) VALUES(?,?,?,?,?,?,?,?,?)',
                    (owner,key,goal,json.dumps(criteria or {'goal':goal}), 'pending','model_pending',json.dumps({'messages':messages}),episode,time.time()))
            else:
                if row['state'] in {'completed','failed','cancelled'}:
                    return None
                if row['lease_until'] and row['lease_until'] > time.time():
                    raise TaskBusy('Task is owned by an active worker')
                if row['phase'] in {'tool_started','skill_started'}:
                    conn.execute("UPDATE tasks SET state='uncertain' WHERE owner=? AND id=?", (owner,key))
                    # Commit before reporting recovery so the uncertain status survives.
                    conn.commit()
                    raise RecoveryRequired('A tool may already have produced effects; human resolution is required')
                if row['state'] in {'suspended','uncertain'}:
                    raise RecoveryRequired('Task requires a human answer or side-effect resolution')
                if row['recoveries'] >= 3:
                    raise RecoveryRequired('Task exceeded its recovery attempt limit')
            recovery = bool(row and (row['state']=='running' or row['reply'] is not None))
            conn.execute("UPDATE tasks SET state='running',lease_owner=?,lease_until=?,attempts=attempts+1,recoveries=recoveries+?,updated=? WHERE owner=? AND id=?",
                (token,time.time()+180,int(recovery),time.time(),owner,key))
        return token

    def renew(self, owner, key, token):
        with closing(self.connect()) as conn, conn:
            return conn.execute("UPDATE tasks SET lease_until=? WHERE owner=? AND id=? AND lease_owner=? AND state='running' AND lease_until>?",
                (time.time()+180,owner,key,token,time.time())).rowcount == 1

    def checkpoint(self, owner, key, token, phase, data, step=None):
        serialized = json.dumps(data, ensure_ascii=False)
        if len(serialized.encode()) > 64*1024*1024:
            raise ValueError('Task checkpoint exceeds 64 MiB')
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            current = conn.execute('SELECT length(CAST(checkpoint AS BLOB)) FROM tasks WHERE owner=? AND id=?', (owner,key)).fetchone()
            self.capacity(conn,len(serialized.encode())-(current[0] if current else 0)+len(json.dumps(step).encode()) if step else len(serialized.encode())-(current[0] if current else 0))
            updated = conn.execute('UPDATE tasks SET phase=?,checkpoint=?,updated=? WHERE owner=? AND id=? AND lease_owner=? AND state=\'running\' AND lease_until>?',
                (phase,serialized,time.time(),owner,key,token,time.time()))
            if updated.rowcount != 1:
                raise TaskBusy('Task lease lost; refusing further execution')
            if step:
                conn.execute('INSERT INTO task_steps VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(owner,task_id,id) DO UPDATE SET state=excluded.state,result=excluded.result',
                    (owner,key,step['id'],step['name'],json.dumps(step['arguments']),json.dumps(step.get('dependencies',[])),step['state'],step.get('result')))

    def finish(self, owner, key, token, state, reply=None):
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT length(CAST(COALESCE(reply,\'\') AS BLOB)) FROM tasks WHERE owner=? AND id=?',(owner,key)).fetchone()
            self.capacity(conn,len((reply or '').encode())-(old[0] if old else 0))
            row=conn.execute('SELECT checkpoint,criteria FROM tasks WHERE owner=? AND id=?',(owner,key)).fetchone()
            data=json.loads(row['checkpoint']) if row else {}
            criteria=json.loads(row['criteria']) if row else {}
            evaluation=data.get('evaluation') or data.get('skill_result',{}).get('verification') or {}
            verified=state=='completed' and bool(criteria.get('checks')) and evaluation.get('verified') is True and bool(evaluation.get('evidence'))
            quality='verified_completed' if verified else 'unverified'
            if conn.execute('UPDATE tasks SET state=?,reply=?,verification=?,lease_owner=NULL,lease_until=NULL,updated=? WHERE owner=? AND id=? AND lease_owner=? AND lease_until>?',
                (state,reply,quality,time.time(),owner,key,token,time.time())).rowcount != 1:
                raise TaskBusy('Task lease lost while finishing')

    def answer(self, owner, key, answer):
        if not isinstance(answer,str) or len(answer.encode())>1024*1024:
            raise ValueError('Human answer exceeds 1 MiB')
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT checkpoint FROM tasks WHERE owner=? AND id=? AND state='suspended'", (owner,key)).fetchone()
            if not row:
                raise RecoveryRequired('No suspended task in this scope')
            data = json.loads(row['checkpoint'])
            data['messages'].append({'role':'tool','tool_call_id':data.pop('suspended_tool_call_id'),'content':answer})
            self.capacity(conn,len(json.dumps(data).encode())-len(row['checkpoint'].encode()))
            conn.execute("UPDATE tasks SET state='pending',phase='tool_done',checkpoint=?,reply=NULL,updated=? WHERE owner=? AND id=?", (json.dumps(data),time.time(),owner,key))

    def resolve(self, owner, key, call_id, result):
        if not isinstance(result,str) or len(result.encode())>1024*1024:
            raise ValueError('Human resolution exceeds 1 MiB')
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT checkpoint FROM tasks WHERE owner=? AND id=? AND state='uncertain' AND phase='tool_started'", (owner,key)).fetchone()
            step = conn.execute("SELECT 1 FROM task_steps WHERE owner=? AND task_id=? AND id=? AND state='running'", (owner,key,call_id)).fetchone()
            if row is None or step is None:
                raise RecoveryRequired('No matching uncertain tool step in this scope')
            data = json.loads(row['checkpoint'])
            data['messages'].append({'role':'tool','tool_call_id':call_id,'content':result})
            data['human_resolution'] = {'tool_call_id':call_id,'result':result}
            self.capacity(conn,len(json.dumps(data).encode())-len(row['checkpoint'].encode())+len(result.encode()))
            conn.execute("UPDATE task_steps SET state='resolved_by_human',result=? WHERE owner=? AND task_id=? AND id=?", (result,owner,key,call_id))
            conn.execute("UPDATE tasks SET state='pending',phase='tool_done',checkpoint=?,lease_owner=NULL,lease_until=NULL,updated=? WHERE owner=? AND id=?", (json.dumps(data),time.time(),owner,key))

    def cancel(self, owner, key):
        with closing(self.connect()) as conn, conn:
            return conn.execute("UPDATE tasks SET state='cancelled',reply='Task cancelled',lease_owner=NULL,lease_until=NULL,updated=? WHERE owner=? AND id=? AND state NOT IN ('completed','failed','cancelled')", (time.time(),owner,key)).rowcount == 1

    def cancel_session(self, owner):
        with closing(self.connect()) as conn, conn:
            conn.execute("UPDATE tasks SET state='cancelled',reply='Session cleared',lease_owner=NULL,lease_until=NULL,updated=? WHERE owner=? AND state NOT IN ('completed','failed','cancelled')", (time.time(),owner))

    def mark_uncertain(self, owner, key):
        with closing(self.connect()) as conn, conn:
            conn.execute("UPDATE tasks SET state='uncertain',lease_owner=NULL,lease_until=NULL WHERE owner=? AND id=? AND state='running' AND lease_until<=?", (owner,key,time.time()))

    def steps(self, owner, key):
        self.get(owner,key)
        with closing(self.connect()) as conn:
            return [dict(row) for row in conn.execute('SELECT id,name,input,dependencies,state,result FROM task_steps WHERE owner=? AND task_id=? ORDER BY rowid', (owner,key))]


_active = ContextVar('active_task_checkpoint', default=None)


def save_checkpoint(phase, messages, **metadata):
    active = _active.get()
    if active is None:
        return
    store, owner, key, token = active
    step = metadata.pop('step',None)
    store.checkpoint(owner,key,token,phase,{'messages':messages,**metadata},step)


from .operations.tracing import traced

@traced('task','agent.task')
def durable_invoke(function, messages, episode, resume=False, skill=False, skill_arguments=None, verify_skill=None, acceptance_contract=None):
    from .policy import current_context, execution_scope
    import threading
    context = current_context()
    if context.task_id is None:
        result=function(None)
        return verify_skill(result) if skill and verify_skill else result
    store, owner, key = TaskStore(), task_owner(context), context.task_id
    try:
        previous = store.get(owner,key)
    except KeyError:
        previous = None
    if resume and previous and previous['state'] == 'suspended':
        last = messages[-1]
        if last.get('role') != 'tool' or last.get('tool_call_id') != previous['checkpoint'].get('suspended_tool_call_id'):
            raise RecoveryRequired('Human answer does not match the suspended tool call')
        store.answer(owner,key,last['content'])
        previous = store.get(owner,key)
    content = next((m.get('content') or '' for m in reversed(messages) if m.get('role')=='user'), '')
    goal = ('\n'.join(p.get('text','') for p in content if isinstance(p,dict)) if isinstance(content,list) else str(content))[:16000]
    if previous and not resume and previous['goal'] != goal:
        raise RecoveryRequired('Task ID cannot be reused with a different goal')
    from .acceptance import load_contract
    token = store.claim(owner,key,goal,messages,episode,criteria=(acceptance_contract if acceptance_contract is not None else load_contract(goal)) if previous is None else None)
    if token is None:
        row = store.get(owner,key)
        return row['checkpoint']['skill_result'] if skill else row['reply'] or ''
    if previous:
        previous = store.get(owner,key)
    saved = previous['checkpoint'] if previous else None
    if saved:
        messages[:] = saved['messages']
        saved = {**saved,'phase':previous['phase']}
    active = _active.set((store,owner,key,token))
    stop, lost = threading.Event(), threading.Event()
    def heartbeat():
        while not stop.wait(20):
            try:
                if not store.renew(owner,key,token):
                    lost.set()
                    return
            except Exception:
                lost.set()
                return
    thread = threading.Thread(target=heartbeat,daemon=True)
    thread.start()
    original_cancel = context.check_cancelled
    last_check = [0.0]
    def cancelled():
        if lost.is_set() or bool(original_cancel and original_cancel()):
            return True
        if time.monotonic()-last_check[0] > .2:
            last_check[0] = time.monotonic()
            try:
                row = store.get(owner,key)
                if row['state'] != 'running' or row['lease_owner'] != token or row['lease_until'] <= time.time():
                    lost.set()
            except Exception:
                lost.set()
        return lost.is_set()
    try:
        from .worktrees import task_workspace
        with execution_scope(check_cancelled=cancelled), task_workspace(owner,key):
            is_skill=skill or bool(saved and saved.get('kind')=='skill')
            if saved and saved.get('kind')=='skill' and saved.get('phase')=='skill_done':
                result = saved['skill_result']
            else:
                if saved and saved.get('kind')=='skill' and saved.get('phase')=='skill_executed':
                    result=saved['skill_result']
                else:
                    if skill:
                        save_checkpoint('skill_started',messages,kind='skill',step={'id':'skill','name':'run_skill','arguments':skill_arguments or {},'state':'running'})
                    result = function(saved)
                if is_skill:
                    save_checkpoint('skill_executed',messages,kind='skill',skill_result=result,
                        step={'id':'skill','name':'run_skill','arguments':skill_arguments or {},'state':'completed' if result.get('success') else 'failed','result':json.dumps(result,ensure_ascii=False)})
                    if verify_skill:
                        result=verify_skill(result)
                    save_checkpoint('skill_done',messages,kind='skill',skill_result=result,
                        step={'id':'skill','name':'run_skill','arguments':skill_arguments or {},'state':'completed' if result.get('success') else 'failed','result':json.dumps(result,ensure_ascii=False)})
        if is_skill:
            from agent_runtime import format_skill_reply
            reply = format_skill_reply(result)
        else:
            reply = str(result)
        state = 'cancelled' if lost.is_set() or (original_cancel and original_cancel()) else 'failed' if (is_skill and not result.get('success')) or reply.startswith('Error:') or '已轉交人工審核' in reply or '遠端 LLM 服務發生錯誤' in reply else 'completed'
        if store.get(owner,key)['state'] != 'cancelled':
            store.finish(owner,key,token,state,reply)
        return result if skill or not is_skill else reply
    except Exception as error:
        from exceptions import TaskSuspendedException
        if isinstance(error,TaskSuspendedException):
            metadata = {key:value for key,value in store.get(owner,key)['checkpoint'].items() if key!='messages'}
            metadata['suspended_tool_call_id'] = error.tool_call_id
            save_checkpoint('waiting_user',error.messages,**metadata)
            store.finish(owner,key,token,'suspended',error.question)
        else:
            # A tool started checkpoint survives an exception as uncertain; never blindly replay.
            row = store.get(owner,key)
            if row['state'] != 'cancelled':
                from .budgets import BudgetExhausted
                state='uncertain' if row['phase'] in {'tool_started','skill_started'} else 'failed' if isinstance(error,BudgetExhausted) else 'pending'
                store.finish(owner,key,token,state,str(error))
        raise
    finally:
        stop.set()
        thread.join(1)
        _active.reset(active)


def dispatch_task_input(text, runtime, attachments=None):
    """Human transport commands and answers. Never register as a model tool."""
    from .policy import current_context, execution_scope
    from agent_runtime import AgentTurnResult, resume_agent_turn
    from permissions import current_role
    store = TaskStore()
    context = current_context()
    owner = task_owner(context)
    parts = text.strip().split(maxsplit=3)
    command = parts[0] if parts else ''
    if attachments and command.startswith('/'):
        raise ValueError('Attachments cannot accompany transport control commands')
    if command in {'/workspace','/session-delete'}:
        from .workspace_lifecycle.commands import human_command
        try:return AgentTurnResult(human_command(text),None)
        except (ValueError,PermissionError,OSError) as error:return AgentTurnResult(f'Error: {error}',None)
    if command in {'/clear','/reset'}:
        store.cancel_session(owner)
        runtime.messages = [message for message in runtime.messages if message.get('role')=='system']
        return AgentTurnResult('Session cleared; suspended tasks will not be resumed.',None)
    if command == '/tasks':
        return AgentTurnResult(json.dumps(store.list(owner),ensure_ascii=False),None)
    if command == '/agents':
        from .coordinator import human_agent_command
        try:return AgentTurnResult(human_agent_command(text),None)
        except (KeyError,ValueError,PermissionError,RuntimeError) as error:return AgentTurnResult(f'Error: {error}',None)
    if command == '/delivery':
        from .git_delivery import human_delivery_command
        try:return AgentTurnResult(human_delivery_command(text),None)
        except (KeyError,ValueError,PermissionError,RuntimeError) as error:return AgentTurnResult(f'Error: {error}',None)
    if command == '/worktree':
        from .worktrees import human_worktree_command
        try:
            return AgentTurnResult(human_worktree_command(text,store,owner,context),None)
        except (KeyError,ValueError,PermissionError,RuntimeError) as error:
            return AgentTurnResult(f'Error: {error}',None)
    if command == '/budget':
        from .budgets import TaskBudget
        try:
            key=parts[1] if len(parts)>1 else context.task_id
            row=store.get(owner,key)
            budget=TaskBudget(owner,key)
            snapshot=budget.snapshot()
            snapshot['limits']=json.loads(snapshot['limits'])
            snapshot['cost_enforced']=bool(budget.max_cost)
            snapshot['remaining_seconds']=budget.remaining_seconds()
            return AgentTurnResult(json.dumps(snapshot,ensure_ascii=False),row['episode'])
        except (KeyError,ValueError) as error:
            return AgentTurnResult(f'Error: {error}',None)
    if command in {'/resume','/resolve','/cancel-task','/steps'}:
        try:
            if len(parts) < 2:
                raise ValueError('Task ID is required')
            key = parts[1]
            row = store.get(owner,key)
            if command == '/cancel-task':
                return AgentTurnResult('Task cancelled' if store.cancel(owner,key) else 'Task already finished',row['episode'])
            if command == '/steps':
                return AgentTurnResult(json.dumps(store.steps(owner,key),ensure_ascii=False),row['episode'])
            if command == '/resolve':
                if current_role() != 'admin':
                    raise PermissionError('Side-effect resolution requires admin role')
                if len(parts) != 4:
                    raise ValueError('Usage: /resolve <task_id> <tool_call_id> <observed_result>')
                store.resolve(owner,key,parts[2],parts[3])
                return AgentTurnResult('Human result saved. Use /resume to continue; the tool will not be repeated.',row['episode'])
            with execution_scope(task_id=key):
                return resume_agent_turn(row['checkpoint']['messages'],runtime,row['episode'])
        except (KeyError,ValueError,PermissionError,TaskBusy,RecoveryRequired) as error:
            return AgentTurnResult(f'Error: {error}',None)
    if not command.startswith('/'):
        suspended = next((row for row in store.list(owner) if row['state']=='suspended'),None)
        if suspended:
            row = store.get(owner,suspended['id'])
            messages = row['checkpoint']['messages'] + [{'role':'tool','tool_call_id':row['checkpoint']['suspended_tool_call_id'],'content':text}]
            if attachments:
                from agent_runtime import build_user_message
                messages.append(build_user_message('Attached files for this answer', attachments))
            with execution_scope(task_id=row['id']):
                return resume_agent_turn(messages,runtime,row['episode'])
    return None
