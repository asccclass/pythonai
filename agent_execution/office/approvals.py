"""Human-only approval commands and native durable continuation authority.

Source intent -> target grant/receipt -> source started -> tool -> source result.
There is deliberately no distributed transaction or automatic replay of a
consuming/started operation whose result is missing.
"""
from contextlib import contextmanager, closing
from dataclasses import dataclass
import inspect
import hashlib
import json
from pathlib import Path
import time
import uuid

from permissions import current_role, get_user_role, set_current_role, check_tool_permission
from agent_execution.policy import current_context, execution_scope, _validate_paths
from agent_execution.results import ToolResult, normalize_result
from agent_execution.tasks import TaskStore
from .admission import assert_admitted
from .bindings import arguments_for, control_snapshot, preview, tree_digest
from .contracts import OwnerScope, _identifier
from .directory import OfficeConflict
from .gate import OfficeWait, OfficeUncertain
from .persistence import canonical, digest
from .reconcile import OfficeReconciler


@dataclass
class Frame:
    id: str
    name: str
    args: str
    path: list
    children: int = 0
    approved: bool = False


class OfficeApprovals:
    def __init__(self, ledger, *, events, approvals):
        self.ledger = ledger
        self.directory = ledger.directory
        self.store = ledger.store
        self.bridge = OfficeReconciler(ledger,events=events,approvals=approvals)

    @contextmanager
    def connection(self, actor=None):
        owner = OwnerScope(*json.loads(actor.ticket.owner)) if actor else OwnerScope.from_current()
        with execution_scope(source=owner.source,identity=owner.identity,project=owner.project,session=owner.session):
            with self.directory._connection(write=True) as pair:
                yield pair

    @staticmethod
    def role(owner):
        role = current_role()
        if owner.source != 'local':
            ranks = {'viewer':0,'operator':1,'admin':2}
            role = min((role,get_user_role(owner.identity)),key=lambda r:ranks.get(r,-1))
        return role

    def check(self, conn, owner, actor, name=None):
        assert_admitted(conn,owner.key,actor.ticket.task_id)
        from agent_execution.tasks import _active
        active = _active.get()
        core = conn.execute('SELECT state,lease_owner,lease_until FROM tasks WHERE owner=? AND id=?',(owner.key,actor.ticket.task_id)).fetchone()
        if (not active or active[0].path.resolve()!=self.store.path.resolve() or active[1:3]!=(owner.key,actor.ticket.task_id)
                or core['state']!='running' or core['lease_owner']!=active[3] or core['lease_until']<=time.time()):
            raise PermissionError('Tool lacks its admitted active core task')
        from agent_execution.sessions import project_workspace
        project = project_workspace(owner.project).resolve()
        workspace = current_context().workspace.resolve()
        if not workspace.is_relative_to(project):
            binding = conn.execute('SELECT repository,storage FROM task_workspaces WHERE owner=? AND task_id=?',(owner.key,actor.ticket.task_id)).fetchone()
            if binding is None or Path(binding['repository']).resolve()!=project:
                raise PermissionError('Office tool workspace is not its admitted project/worktree')
            key = hashlib.sha256(json.dumps([owner.key,actor.ticket.task_id,str(project)],ensure_ascii=False).encode()).hexdigest()
            worktree = Path(binding['storage']).resolve()/key
            if worktree.resolve()!=worktree or not workspace.is_relative_to(worktree):
                raise PermissionError('Office tool workspace binding changed')
        row = conn.execute('''SELECT s.*,a.staff_revision,a.payload,a.team_id,a.team_revision,
            COALESCE(c.paused,0) AS paused FROM office_assignments a
            JOIN office_staff s ON s.owner=a.owner AND s.id=a.staff_id
            LEFT JOIN office_staff_controls c ON c.owner=s.owner AND c.staff_id=s.id
            WHERE a.owner=? AND a.id=?''',(owner.key,actor.ticket.assignment_id)).fetchone()
        if not row or not row['enabled'] or row['deleted'] or row['paused'] or row['revision'] != row['staff_revision']:
            raise OfficeUncertain('Office staff configuration changed')
        payload = json.loads(row['payload'])
        tools = frozenset(payload['staff']['allowed_tools'])
        if payload.get('tool_ceiling',[]) is not None:
            tools &= frozenset(payload.get('tool_ceiling',[]))
        if not actor.tools <= tools or (name is not None and name not in actor.tools):
            raise PermissionError('Tool exceeds admitted Office ceiling')
        if current_context().allowed_tools is not None and name is not None and name not in current_context().allowed_tools:
            raise PermissionError('Nested tool exceeds caller ceiling')
        if row['team_id']:
            team = conn.execute('SELECT * FROM office_teams WHERE owner=? AND id=?',(owner.key,row['team_id'])).fetchone()
            member = conn.execute('SELECT 1 FROM office_members WHERE owner=? AND team_id=? AND staff_id=?',(owner.key,row['team_id'],actor.ticket.staff_id)).fetchone()
            if not team or team['deleted'] or not team['enabled'] or team['revision']!=row['team_revision'] or not member:
                raise OfficeUncertain('Office team binding changed')
        role = self.role(owner)
        if role != current_role():
            raise PermissionError('Role changed during Office execution')
        with set_current_role(role):
            if name is not None and not check_tool_permission(name):
                raise PermissionError('Role no longer authorizes this tool')
        return role

    @staticmethod
    def read_only(name,args,function):
        import base
        from tools.workspace_info import workspace_info
        expected = {'read_file':base.read_file,'list_files':base.list_files,'workspace_info':workspace_info}
        return (name in expected and inspect.unwrap(function) is inspect.unwrap(expected[name])
                and (name != 'workspace_info' or args.get('action','list') == 'list'))

    @staticmethod
    def encode(value):
        if isinstance(value,ToolResult):
            return canonical({'kind':'tool','value':value.to_dict()})
        try:
            return canonical({'kind':'json','value':value})
        except (ValueError,TypeError):
            return canonical({'kind':'tool','value':normalize_result(value).to_dict()})

    @staticmethod
    def decode(data):
        value = json.loads(data)
        return ToolResult(**value['value']) if value['kind']=='tool' else value['value']

    def invoke(self, actor, provider_call, name, arguments, function, execute):
        _identifier(provider_call,'provider tool-call ID')
        if name in {'manage_agents','create_dynamic_tool','manage_extensions'} or (name=='manage_schedule' and arguments.get('action')=='add'):
            raise PermissionError('Unmanaged/background Office execution is disabled')
        args = arguments_for(function,arguments)
        owner = OwnerScope(*json.loads(actor.ticket.owner))
        context = current_context()
        if ((context.identity,context.project,context.session,context.task_id,context.task_database) !=
                (owner.identity,owner.project,owner.session,actor.ticket.task_id,actor.ticket.database)):
            raise PermissionError('Nested execution cannot replace Office owner/task authority')
        parent = actor.frames[-1] if actor.frames else None
        if parent:
            parent.children += 1
        path = [*parent.path,parent.children] if parent else []
        key = digest(canonical({'provider_call':provider_call,'path':path}))
        with self.connection(actor) as (conn,scope):
            role = self.check(conn,scope,actor,name)
            staff_revision = conn.execute('SELECT staff_revision FROM office_assignments WHERE owner=? AND id=?',(scope.key,actor.ticket.assignment_id)).fetchone()[0]
        _validate_paths(arguments,current_context().trusted_assets if name=='run_command' else (),
                        writable=name not in {'read_file','list_files','workspace_info'})
        from agent_execution.budgets import active_budget
        budget = active_budget()
        if budget: budget.check()
        if current_context().check_cancelled and current_context().check_cancelled():
            raise InterruptedError('Office task cancelled')
        control = control_snapshot(name,args,function)
        static = {'owner':owner.key,'task_id':actor.ticket.task_id,'assignment_id':actor.ticket.assignment_id,
            'staff_id':actor.ticket.staff_id,'generation':actor.ticket.generation,
            'staff_revision':staff_revision,'provider':'pythonai','model':actor.model,
            'session_id':actor.session_id,'turn_id':actor.turn_id,'provider_tool_call_id':provider_call,
            'path':path,'name':name,'arguments':args,'arguments_digest':digest(canonical(args)),
            'workspace':str(current_context().workspace.resolve()),'role':role,
            'tools':sorted(actor.tools),'control':control}
        fingerprint = digest(canonical(static))
        readonly = self.read_only(name,args,function)
        import base
        composite = name=='run_skill' and inspect.unwrap(function) is inspect.unwrap(base.run_skill)
        with self.connection(actor) as (conn,scope):
            self.check(conn,scope,actor,name)
            row = conn.execute('SELECT * FROM office_calls WHERE owner=? AND assignment_id=? AND call_key=?',
                               (owner.key,actor.ticket.assignment_id,key)).fetchone()
            if row is not None:
                if row['fingerprint'] != fingerprint:
                    self.uncertain(conn,owner,actor.ticket.assignment_id,row['id'],'binding_changed')
                    raise OfficeUncertain('Office binding changed')
                binding = json.loads(row['binding'])
                if row['state']=='completed':
                    return self.decode(row['result'])
                if row['state'] in {'denied','expired','cancelled'}:
                    return ToolResult(False,error='Office approval '+row['state']+': '+row['reason'],side_effects={'status':'none'})
                if row['state'] in {'consuming','started','uncertain'}:
                    raise OfficeUncertain('Office operation result is unknown')
                if row['state']=='composite' and not composite:
                    raise OfficeUncertain('Opaque tool stack cannot be restored')
            else:
                count = conn.execute('SELECT COUNT(*) FROM office_calls').fetchone()[0]
                if count >= self.ledger.limits.operations:
                    raise OfficeConflict('Office call capacity reached')
                call_id = uuid.uuid4().hex
                binding = {**static,'before':tree_digest(current_context().workspace),
                    'preview':preview(name,args),'expires':time.time()+self.ledger.settings.approval_ttl_seconds}
                serialized = self.ledger._json(binding)
                operation_id = None if readonly else uuid.uuid4().hex
                outbox_id = None if readonly else uuid.uuid4().hex
                payload = self.ledger._json({'approval_id':outbox_id,'action':'office:'+operation_id,
                    'arguments':{'binding':binding},'expires':binding['expires'],'managed':True}) if operation_id else None
                self.ledger._capacity(conn,owner,operations=int(not readonly),outbox=int(not readonly),
                                      bytes_added=len(serialized.encode())+(len(serialized.encode())+len(payload.encode()) if payload else 0))
                if operation_id:
                    conn.execute('''INSERT INTO office_operations(owner,id,assignment_id,request_id,fingerprint,payload,state,created)
                        VALUES(?,?,?,?,?,?,'waiting_approval',?)''',
                        (owner.key,operation_id,actor.ticket.assignment_id,'call:'+key,digest(serialized),serialized,time.time()))
                    self.ledger._outbox(conn,owner,outbox_id,actor.ticket.assignment_id,operation_id,'approval',payload)
                conn.execute('''INSERT INTO office_calls(owner,id,assignment_id,parent_id,operation_id,call_key,name,binding,fingerprint,state,created)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (owner.key,call_id,actor.ticket.assignment_id,
                    parent.id if parent else None,operation_id,key,name,serialized,fingerprint,'started' if readonly else 'waiting',time.time()))
                row = conn.execute('SELECT * FROM office_calls WHERE owner=? AND id=?',(owner.key,call_id)).fetchone()
            row = dict(row)
        if not readonly and row['state']!='composite':
            self.authorize(actor,row,binding)
        frame = Frame(row['id'],name,digest(canonical(args)),path,approved=True)
        actor.frames.append(frame)
        try:
            with self.connection(actor) as (conn,scope):
                role = self.check(conn,scope,actor,name)
            if control_snapshot(name,args,function)!=binding['control'] or digest(canonical(arguments_for(function,arguments)))!=binding['arguments_digest']:
                raise OfficeUncertain('Approved arguments or runtime configuration changed')
            if row['state']!='composite' and tree_digest(current_context().workspace) != binding['before']:
                raise OfficeUncertain('Workspace changed before tool execution')
            with set_current_role(role):
                value = execute()
            self.record_result(actor,row,value)
            return value
        except OfficeWait:
            with self.connection(actor) as (conn,scope):
                if composite:
                    conn.execute("UPDATE office_calls SET state='composite' WHERE owner=? AND id=?",(owner.key,row['id']))
                else:
                    self.uncertain(conn,scope,actor.ticket.assignment_id,row['id'],'opaque_nested_wait')
            if not composite:
                raise OfficeUncertain('Opaque tool stack cannot be reconnected') from None
            raise
        except BaseException:
            with self.connection(actor) as (conn,scope):
                self.uncertain(conn,scope,actor.ticket.assignment_id,row['id'],'execution_or_ack_failed')
            raise
        finally:
            frame.approved = False
            actor.frames.pop()

    def call_capacity(self,conn,extra):
        self.ledger._capacity(conn,OwnerScope.from_current(),bytes_added=extra)

    @staticmethod
    def uncertain(conn,owner,assignment,call_id,reason):
        conn.execute("UPDATE office_calls SET state='uncertain',reason=? WHERE owner=? AND id=?",(reason,owner.key,call_id))
        conn.execute("UPDATE office_assignments SET state='uncertain' WHERE owner=? AND id=? AND state!='cancelled'",(owner.key,assignment))
        conn.execute("UPDATE office_executions SET state='uncertain' WHERE owner=? AND assignment_id=?",(owner.key,assignment))
        conn.execute("UPDATE office_continuations SET state='uncertain' WHERE owner=? AND assignment_id=?",(owner.key,assignment))
        conn.execute("UPDATE tasks SET state='uncertain',lease_owner=NULL,lease_until=NULL WHERE owner=? AND id=(SELECT task_id FROM office_executions WHERE owner=? AND assignment_id=?) AND state IN ('office_wait','pending')",(owner.key,owner.key,assignment))
        # Like TaskStore crash classification, persist the fence before raising.
        conn.commit()

    def authorize(self,actor,row,binding):
        with self.connection(actor) as (conn,owner):
            self.check(conn,owner,actor,row['name'])
            outbox = dict(conn.execute('SELECT * FROM office_outbox WHERE owner=? AND operation_id=?',(owner.key,row['operation_id'])).fetchone())
            op = conn.execute('SELECT * FROM office_operations WHERE owner=? AND id=?',(owner.key,row['operation_id'])).fetchone()
            if op['payload'] != row['binding'] or op['fingerprint'] != digest(row['binding']) or json.loads(outbox['payload'])['arguments'] != {'binding':binding}:
                raise OfficeUncertain('Approval intent differs from its call binding')
            state = op['approval_state']
            if state in {'denied','expired','cancelled'}:
                conn.execute('UPDATE office_calls SET state=? WHERE owner=? AND id=?',(state,owner.key,row['id']))
            elif state != 'approved':
                raise OfficeWait(row['id'])
        if state in {'denied','expired','cancelled'}:
            raise OfficeWait(row['id'])  # Resume once to render a durable failed tool result.
        if tree_digest(current_context().workspace) != binding['before']:
            with self.connection(actor) as (conn,owner):
                self.uncertain(conn,owner,actor.ticket.assignment_id,row['id'],'precondition_changed')
            raise OfficeUncertain('Approved file preconditions changed')
        token = uuid.uuid4().hex
        with self.connection(actor) as (conn,owner):
            self.check(conn,owner,actor,row['name'])
            target = self.bridge._bind(conn,owner,'approval')
            conn.execute("UPDATE office_calls SET state='consuming',consume_token=? WHERE owner=? AND id=? AND state='waiting'",(token,owner.key,row['id']))
        # No source transaction remains open across target I/O.
        try:
            target.consume_managed(owner,outbox,token)
            with self.connection(actor) as (conn,owner):
                self.check(conn,owner,actor,row['name'])
                changed = conn.execute("UPDATE office_calls SET state='started' WHERE owner=? AND id=? AND state='consuming' AND consume_token=?",(owner.key,row['id'],token)).rowcount
                if changed != 1: raise OfficeUncertain('Lost approval consumption fence')
        except BaseException:
            with self.connection(actor) as (conn,owner):
                self.uncertain(conn,owner,actor.ticket.assignment_id,row['id'],'consume_or_ack_unknown')
            raise

    def park(self,actor,call_id,task_token):
        with self.connection(actor) as (conn,owner):
            role = self.check(conn,owner,actor)
            row = conn.execute('SELECT * FROM tasks WHERE owner=? AND id=?',(owner.key,actor.ticket.task_id)).fetchone()
            if row['state']!='running' or row['lease_owner']!=task_token or row['phase']!='tool_started':
                raise OfficeUncertain('Unverified Office continuation boundary')
            self.ledger._capacity(conn,owner,bytes_added=len(canonical(sorted(actor.tools)).encode())+len(str(current_context().workspace).encode())+100)
            conn.execute('''INSERT INTO office_continuations
                (owner,assignment_id,call_id,state,checkpoint_digest,session_id,turn_id,workspace,tools,role,updated)
                VALUES(?,?,?,'waiting',?,?,?,?,?,?,?)
                ON CONFLICT(owner,assignment_id) DO UPDATE SET call_id=excluded.call_id,state='waiting',
                checkpoint_digest=excluded.checkpoint_digest,workspace=excluded.workspace,tools=excluded.tools,
                role=excluded.role,updated=excluded.updated''',
                (owner.key,actor.ticket.assignment_id,call_id,digest(row['checkpoint']),actor.session_id,
                 actor.turn_id,str(current_context().workspace.resolve()),canonical(sorted(actor.tools)),role,time.time()))
            conn.execute("UPDATE tasks SET state='office_wait',phase='office_wait',lease_owner=NULL,lease_until=NULL WHERE owner=? AND id=?",(owner.key,actor.ticket.task_id))

    def decide(self, call_id, approve, *, reason=''):
        """Authenticated human command. Never register this as a model tool."""
        from .gate import active_actor
        if active_actor() is not None: raise PermissionError('Models cannot decide Office approvals')
        if type(approve) is not bool or not isinstance(reason,str) or len(reason.encode())>2000:
            raise ValueError('Invalid human decision or reason')
        with self.connection() as (conn,owner):
            if self.role(owner) != 'admin': raise PermissionError('Live admin role required')
            row = self.ledger._row(conn,'office_calls',owner,call_id)
            if row['state'] not in {'waiting','denied'}: raise OfficeConflict('Call is not awaiting a human decision')
            outbox = dict(conn.execute('SELECT * FROM office_outbox WHERE owner=? AND operation_id=?',(owner.key,row['operation_id'])).fetchone())
            target = self.bridge._bind(conn,owner,'approval')
        result = target.decide_managed(owner,outbox,approve,reason)
        self.reconcile()
        return result

    def reconcile(self):
        self.bridge.reconcile(limit=100)
        with self.connection() as (conn,owner):
            expired = conn.execute('''SELECT c.assignment_id FROM office_continuations c
                JOIN office_executions e ON e.owner=c.owner AND e.assignment_id=c.assignment_id
                WHERE c.owner=? AND c.state IN ('waiting','ready') AND e.deadline<=?''',(owner.key,time.time())).fetchall()
            for item in expired:
                assignment = item['assignment_id']
                conn.execute("UPDATE tasks SET state='failed',reply='Error: Office deadline expired',lease_owner=NULL,lease_until=NULL WHERE owner=? AND id=(SELECT task_id FROM office_executions WHERE owner=? AND assignment_id=?)",(owner.key,owner.key,assignment))
                conn.execute("UPDATE office_executions SET state='failed' WHERE owner=? AND assignment_id=?",(owner.key,assignment))
                conn.execute("UPDATE office_assignments SET state='completed' WHERE owner=? AND id=?",(owner.key,assignment))
                conn.execute("UPDATE office_continuations SET state='completed' WHERE owner=? AND assignment_id=?",(owner.key,assignment))
                conn.execute("UPDATE office_calls SET state='expired',reason='task_deadline' WHERE owner=? AND assignment_id=? AND state IN ('waiting','composite')",(owner.key,assignment))
                conn.execute("UPDATE office_outbox SET state=CASE WHEN attempts=0 THEN 'cancelled' ELSE 'cancel_pending' END,token=NULL,lease_until=NULL WHERE owner=? AND assignment_id=? AND kind='approval' AND state!='cancelled'",(owner.key,assignment))
            for row in conn.execute('''SELECT c.id,c.state,o.approval_state FROM office_calls c
                JOIN office_operations o ON o.owner=c.owner AND o.id=c.operation_id
                WHERE c.owner=? AND c.state='waiting' ''',(owner.key,)).fetchall():
                state = {'denied':'denied','expired':'expired','cancelled':'cancelled'}.get(row['approval_state'])
                if state:
                    outbox = conn.execute('SELECT payload FROM office_outbox WHERE owner=? AND operation_id=(SELECT operation_id FROM office_calls WHERE owner=? AND id=?)',(owner.key,owner.key,row['id'])).fetchone()
                    payload = json.loads(outbox['payload'])
                    target = self.bridge.targets['approval']
                    with target.transaction() as target_conn:
                        decision = target_conn.execute('SELECT reason FROM office_bridge_managed WHERE approval_id=?',(payload['approval_id'],)).fetchone()
                    conn.execute('UPDATE office_calls SET state=?,reason=? WHERE owner=? AND id=?',(state,decision['reason'] if decision else '',owner.key,row['id']))
            conn.execute("""UPDATE office_continuations SET state='ready',updated=? WHERE owner=? AND state='waiting'
                AND call_id IN (SELECT c.id FROM office_calls c LEFT JOIN office_operations o
                ON o.owner=c.owner AND o.id=c.operation_id WHERE c.owner=? AND
                (o.approval_state='approved' OR c.state IN ('denied','expired','cancelled')))""",(time.time(),owner.key,owner.key))

    def snapshot(self,call_id):
        with self.directory._connection() as (conn,owner):
            row = self.ledger._row(conn,'office_calls',owner,call_id)
            binding = json.loads(row['binding'])
            return {'id':row['id'],'state':row['state'],'reason':row['reason'],'binding':binding}

    def bind_turn(self,ticket,session_id,turn_id,tools,workspace,model):
        with self.connection() as (conn,owner):
            assert_admitted(conn,owner.key,ticket.task_id)
            role = self.role(owner)
            if role != current_role(): raise PermissionError('Role changed before native turn')
            expected = (session_id,turn_id,ticket.generation,str(workspace.resolve()),canonical(sorted(tools)),role,model)
            old = conn.execute('SELECT session_id,turn_id,generation,workspace,tools,role,model FROM office_turns WHERE owner=? AND assignment_id=?',(owner.key,ticket.assignment_id)).fetchone()
            if old is not None:
                if tuple(old)!=expected: raise PermissionError('Native turn binding changed')
            else:
                self.ledger._capacity(conn,owner,bytes_added=sum(len(str(v).encode()) for v in expected))
                conn.execute('INSERT INTO office_turns VALUES(?,?,?,?,?,?,?,?,?)',(owner.key,ticket.assignment_id,*expected))

    def record_result(self,actor,row,value):
        result = self.encode(value)
        with self.connection(actor) as (conn,owner):
            self.check(conn,owner,actor,row['name'])
            self.call_capacity(conn,len(result.encode()))
            changed = conn.execute("UPDATE office_calls SET state='completed',result=? WHERE owner=? AND id=? AND state IN ('started','composite')",(result,owner.key,row['id'])).rowcount
            if changed != 1: raise OfficeUncertain('Tool result fence was lost')

    def is_uncertain(self,ticket):
        with closing(self.store.connect()) as conn:
            row = conn.execute('SELECT state FROM office_executions WHERE owner=? AND assignment_id=?',(ticket.owner,ticket.assignment_id)).fetchone()
            return row is None or row['state']=='uncertain'

    def recovery_status(self,assignment_id):
        with self.directory._connection() as (conn,owner):
            assignment = self.ledger._row(conn,'office_assignments',owner,assignment_id)
            wait = conn.execute('SELECT state FROM office_continuations WHERE owner=? AND assignment_id=?',(owner.key,assignment_id)).fetchone()
            execution = conn.execute('SELECT state FROM office_executions WHERE owner=? AND assignment_id=?',(owner.key,assignment_id)).fetchone()
            kind = ('needs_reconciliation' if assignment['state']=='uncertain' or (execution and execution['state']=='uncertain')
                    else 'durable_wait' if wait and wait['state']=='waiting'
                    else 'resumable_boundary' if wait and wait['state']=='ready' else assignment['state'])
            return {'assignment_id':assignment_id,'classification':kind}

    def claim_ready(self,conn,owner,dispatcher):
        from .admission import Admission
        from agent_execution.sessions import project_workspace
        rows = conn.execute('''SELECT c.*,e.task_id,e.staff_id,e.generation,e.deadline,e.session_id AS execution_session,
            a.staff_revision FROM office_continuations c
            JOIN office_executions e ON e.owner=c.owner AND e.assignment_id=c.assignment_id
            JOIN office_assignments a ON a.owner=c.owner AND a.id=c.assignment_id
            WHERE c.owner=? AND c.state='ready' AND e.state='running' AND a.state='active'
            ORDER BY a.queue_seq''',(owner.key,)).fetchall()
        for saved in rows:
            task = conn.execute('SELECT * FROM tasks WHERE owner=? AND id=?',(owner.key,saved['task_id'])).fetchone()
            staff = conn.execute('SELECT * FROM office_staff WHERE owner=? AND id=?',(owner.key,saved['staff_id'])).fetchone()
            paused = conn.execute('SELECT paused FROM office_staff_controls WHERE owner=? AND staff_id=?',(owner.key,saved['staff_id'])).fetchone()
            frozen_tools = frozenset(json.loads(saved['tools']))
            caller_tools = current_context().allowed_tools
            valid = (task is not None and task['state']=='office_wait' and task['phase']=='office_wait'
                and digest(task['checkpoint'])==saved['checkpoint_digest']
                and saved['session_id']==saved['execution_session'] and staff and staff['enabled'] and not staff['deleted']
                and staff['revision']==saved['staff_revision']
                and not (paused and paused[0]) and self.role(owner)==saved['role']
                and (caller_tools is None or frozen_tools<=caller_tools))
            if saved['deadline'] <= time.time():
                conn.execute("UPDATE tasks SET state='failed',reply='Error: Office deadline expired' WHERE owner=? AND id=?",(owner.key,saved['task_id']))
                conn.execute("UPDATE office_executions SET state='failed' WHERE owner=? AND assignment_id=?",(owner.key,saved['assignment_id']))
                conn.execute("UPDATE office_assignments SET state='completed' WHERE owner=? AND id=?",(owner.key,saved['assignment_id']))
                conn.execute("UPDATE office_continuations SET state='completed' WHERE owner=? AND assignment_id=?",(owner.key,saved['assignment_id']))
                continue
            if not valid:
                self.uncertain(conn,owner,saved['assignment_id'],saved['call_id'],'continuation_binding_changed')
                return None
            token = uuid.uuid4().hex
            conn.execute("UPDATE office_executions SET token=?,lease_until=?,updated=? WHERE owner=? AND assignment_id=?",(token,time.time()+dispatcher.lease_seconds,time.time(),owner.key,saved['assignment_id']))
            conn.execute("UPDATE office_continuations SET state='running',updated=? WHERE owner=? AND assignment_id=?",(time.time(),owner.key,saved['assignment_id']))
            conn.execute("UPDATE tasks SET state='pending',phase=? WHERE owner=? AND id=?",(saved['resume_phase'],owner.key,saved['task_id']))
            return Admission(self.store.path,owner.key,saved['assignment_id'],saved['task_id'],saved['staff_id'],saved['generation'],token)
        return None
