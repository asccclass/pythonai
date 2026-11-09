"""Explicit owner-scoped native dispatch; no server startup or background thread.

All worker processes for one installation must share its TaskStore and settings.
SQLite write transactions serialize admission, FIFO selection and capacity checks.
Expired/unknown executions keep their employee and capacity slot until resolved;
they are never automatically retried across an unknown side-effect boundary.
"""
import asyncio
from contextlib import closing
import json
import math
import time
import uuid

from agent_execution.acceptance import validate_contract
from agent_execution.budgets import TaskBudget
from agent_execution.policy import current_context, execution_scope, resolve_path
from agent_execution.sessions import project_workspace
from agent_execution.tasks import TaskStore, RecoveryRequired
from .admission import Admission, admitted, assert_admitted
from .contracts import OwnerScope, Provider, RuntimeStartRequest
from .directory import OfficeConflict
from .runtimes.base import RuntimeEventType
from .runtimes.pythonai import PythonAIRuntime, READ_TOOLS


class OfficeDispatcher:
    def __init__(self, ledger, *, runtime_factory=PythonAIRuntime, lease_seconds=30, timeout_seconds=600, approval_service=None):
        for name,value in (('lease_seconds',lease_seconds),('timeout_seconds',timeout_seconds)):
            if type(value) not in (int,float) or not math.isfinite(value) or not 0 < value <= 86400:
                raise ValueError(f'{name} must be finite in (0,86400]')
        self.ledger = ledger
        self.directory = ledger.directory
        self.store = ledger.store
        self.runtime_factory = runtime_factory
        self.lease_seconds = lease_seconds
        self.timeout_seconds = timeout_seconds
        if approval_service is not None and approval_service.store.path != self.store.path:
            raise ValueError('Office approval service belongs to another TaskStore')
        self.approval_service = approval_service

    def submit_auto(self, request_id, goal, *, criteria=None):
        """Explainable routing: least unfinished assignments, stable staff-ID tie.

        Explicit ledger.submit remains the normal named-employee route. Automatic
        selection never expands provider/tool capabilities or changes an old ID.
        """
        with self.directory._connection() as (conn,owner):
            old = conn.execute('SELECT * FROM office_assignments WHERE owner=? AND request_id=?', (owner.key,request_id)).fetchone()
            if old:
                request = json.loads(old['payload'])['request']
                if request['goal'] != goal or request['criteria'] != criteria:
                    raise OfficeConflict('Request ID reused with different assignment content')
                result = self.ledger._assignment(old)
                return {'assignment':result,'reason':'Existing request retains its selected staff'}
            staff = conn.execute('''SELECT s.*,COUNT(a.id) AS load FROM office_staff s
                LEFT JOIN office_assignments a ON a.owner=s.owner AND a.staff_id=s.id AND a.state NOT IN ('completed','cancelled')
                LEFT JOIN office_staff_controls c ON c.owner=s.owner AND c.staff_id=s.id
                WHERE s.owner=? AND s.deleted=0 AND s.enabled=1 AND s.provider='pythonai' AND COALESCE(c.paused,0)=0
                GROUP BY s.owner,s.id ORDER BY load,s.id LIMIT 1''', (owner.key,)).fetchone()
            if staff is None:
                raise OfficeConflict('No enabled native staff is available')
        try:
            result = self.ledger.submit(request_id,staff['id'],goal,staff_revision=staff['revision'],criteria=criteria)
        except OfficeConflict:
            # Another router may commit this same request while load changes
            # between selection and submit. Its committed choice wins.
            with self.directory._connection() as (conn,owner):
                old = conn.execute('SELECT * FROM office_assignments WHERE owner=? AND request_id=?', (owner.key,request_id)).fetchone()
                if old is None:
                    raise
                request = json.loads(old['payload'])['request']
                if request['goal'] != goal or request['criteria'] != criteria:
                    raise
                return {'assignment':self.ledger._assignment(old),'reason':'Concurrent request retains its committed staff'}
        return {'assignment':result,'reason':f"Fewest unfinished assignments ({staff['load']}); staff ID breaks ties"}

    def _expire(self, conn, owner):
        rows = conn.execute("SELECT * FROM office_executions WHERE owner=? AND state IN ('running','draining') AND (lease_until<=? OR deadline<=?)",
                            (owner.key,time.time(),time.time())).fetchall()
        for row in rows:
            from .gate import parked
            if parked(conn,owner.key,row['assignment_id'],row['task_id']):
                continue
            from .recovery import recover_boundary
            if recover_boundary(conn,owner.key,row):
                continue
            conn.execute("UPDATE office_calls SET state='uncertain',reason='actor_lost_after_start' WHERE owner=? AND assignment_id=? AND state IN ('consuming','started')",(owner.key,row['assignment_id']))
            conn.execute("UPDATE office_continuations SET state='uncertain' WHERE owner=? AND assignment_id=?",(owner.key,row['assignment_id']))
            conn.execute("UPDATE office_executions SET state='uncertain',updated=? WHERE owner=? AND assignment_id=?", (time.time(),owner.key,row['assignment_id']))
            conn.execute("UPDATE office_assignments SET state='uncertain' WHERE owner=? AND id=? AND state!='cancelled'", (owner.key,row['assignment_id']))
            conn.execute("UPDATE tasks SET state='uncertain',lease_owner=NULL,lease_until=NULL WHERE owner=? AND id=? AND state NOT IN ('completed','failed','cancelled')", (owner.key,row['task_id']))
        return len(rows)

    def reconcile(self):
        """Restart reconciliation never replays an execution or adopts old output."""
        with self.directory._connection(write=True) as (conn,owner):
            return self._expire(conn,owner)

    def claim(self):
        if not self.ledger.settings.enabled:
            raise PermissionError('Office execution is disabled')
        if self.approval_service is not None:
            self.approval_service.reconcile()
        with self.directory._connection(write=True) as (conn,owner):
            self._expire(conn,owner)
            if self.approval_service is not None:
                resumed = self.approval_service.claim_ready(conn,owner,self)
                if resumed is not None:
                    return resumed
            states = "('running','draining','uncertain')"
            if conn.execute(f'SELECT COUNT(*) FROM office_executions WHERE state IN {states}').fetchone()[0] >= self.ledger.settings.global_concurrency:
                return None
            if conn.execute(f'SELECT COUNT(*) FROM office_executions WHERE owner=? AND state IN {states}', (owner.key,)).fetchone()[0] >= self.ledger.settings.owner_concurrency:
                return None
            row = conn.execute(f'''SELECT a.* FROM office_assignments a
                JOIN office_staff s ON s.owner=a.owner AND s.id=a.staff_id
                LEFT JOIN office_staff_controls c ON c.owner=s.owner AND c.staff_id=s.id
                WHERE a.owner=? AND a.state='queued' AND s.enabled=1 AND s.deleted=0
                AND s.revision=a.staff_revision AND s.provider='pythonai' AND COALESCE(c.paused,0)=0
                AND NOT EXISTS (SELECT 1 FROM office_executions e WHERE e.owner=a.owner AND e.staff_id=a.staff_id AND e.state IN {states})
                AND NOT EXISTS (SELECT 1 FROM office_assignments older WHERE older.owner=a.owner AND older.staff_id=a.staff_id AND older.state='queued' AND older.queue_seq<a.queue_seq)
                ORDER BY a.queue_seq LIMIT 1''', (owner.key,)).fetchone()
            if row is None:
                return None
            if row['team_id'] is not None:
                team = self.directory._row(conn,'office_teams',owner,row['team_id'],row['team_revision'])
                if not team['enabled'] or not conn.execute('SELECT 1 FROM office_members WHERE owner=? AND team_id=? AND staff_id=?', (owner.key,row['team_id'],row['staff_id'])).fetchone():
                    raise OfficeConflict('Team admission changed')
            payload = json.loads(row['payload'])
            goal = payload['request']['goal']
            if len(goal) > 16000:
                raise ValueError('Native task goal exceeds 16000 characters')
            criteria = validate_contract(payload['request']['criteria'] or {'version':1,'checks':[]})
            with execution_scope(workspace=project_workspace(owner.project),repository_workspace=None):
                for check in criteria['checks']:
                    if check['kind'] == 'tests':
                        raise PermissionError('Managed test execution requires R3 approval')
                    resolve_path(check['path'])
            criteria['goal'] = goal
            from agent_execution.delivery import positive_limit, CapacityError
            if conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] >= positive_limit('AGENT_TASK_LIMIT',10000):
                raise CapacityError('Task record capacity reached')
            TaskStore.capacity(conn,len(row['payload'].encode())+len(json.dumps(criteria).encode()))
            token,now = uuid.uuid4().hex,time.time()
            # A pending core row fixes the backend, goal and acceptance contract
            # before any runtime/provider call, including with Temporal enabled.
            conn.execute('''INSERT INTO tasks(owner,id,goal,criteria,state,phase,checkpoint,updated)
                VALUES(?,?,?,?,'pending','model_pending',?,?)''',
                (owner.key,row['task_id'],goal,json.dumps(criteria),json.dumps({'messages':[{'role':'user','content':goal}]}),now))
            conn.execute('''INSERT INTO office_executions(owner,assignment_id,staff_id,task_id,generation,token,lease_until,deadline,state,updated)
                VALUES(?,?,?,?,1,?,?,?,'running',?)''',
                (owner.key,row['id'],row['staff_id'],row['task_id'],token,now+self.lease_seconds,now+self.timeout_seconds,now))
            conn.execute("UPDATE office_assignments SET state='active' WHERE owner=? AND id=?", (owner.key,row['id']))
            return Admission(self.store.path,owner.key,row['id'],row['task_id'],row['staff_id'],1,token)

    def renew(self, ticket):
        with self.directory._connection(write=True) as (conn,owner):
            if owner.key != ticket.owner:
                return False
            return conn.execute("""UPDATE office_executions SET lease_until=?,updated=?
                WHERE owner=? AND assignment_id=? AND token=? AND generation=? AND state='running' AND lease_until>? AND deadline>?""",
                (time.time()+self.lease_seconds,time.time(),owner.key,ticket.assignment_id,ticket.token,ticket.generation,time.time(),time.time())).rowcount == 1

    def _valid(self, ticket):
        try:
            with closing(self.store.connect()) as conn:
                assert_admitted(conn,ticket.owner,ticket.task_id)
            return True
        except (RecoveryRequired,PermissionError):
            return False

    def _bind_session(self, ticket, session):
        with self.directory._connection(write=True) as (conn,owner):
            assert_admitted(conn,owner.key,ticket.task_id)
            conn.execute('UPDATE office_executions SET session_id=? WHERE owner=? AND assignment_id=?', (session,owner.key,ticket.assignment_id))

    def accept_event(self, ticket, event, turn_id):
        """Fence every provider event before publishing durable terminal output."""
        with self.directory._connection(write=True) as (conn,owner):
            assert_admitted(conn,owner.key,ticket.task_id)
            row = conn.execute('SELECT * FROM office_executions WHERE owner=? AND assignment_id=?', (owner.key,ticket.assignment_id)).fetchone()
            if event.session_id != row['session_id'] or event.generation != ticket.generation or (event.turn_id is not None and event.turn_id != turn_id):
                raise OfficeConflict('Late or mismatched runtime event')
            if row['event_seq'] >= 10000:
                raise OfficeConflict('Runtime event capacity reached')
            conn.execute('UPDATE office_executions SET event_seq=event_seq+1 WHERE owner=? AND assignment_id=?', (owner.key,ticket.assignment_id))
            if event.type == RuntimeEventType.APPROVAL_WAITING:
                wait = conn.execute("SELECT 1 FROM office_continuations WHERE owner=? AND assignment_id=? AND state='waiting' AND session_id=? AND turn_id=?",(owner.key,ticket.assignment_id,event.session_id,turn_id)).fetchone()
                core = conn.execute('SELECT state FROM tasks WHERE owner=? AND id=?',(owner.key,ticket.task_id)).fetchone()
                if not wait or core['state']!='office_wait':
                    raise OfficeConflict('Unverified durable approval boundary')
                return 'waiting'
            if event.type not in {RuntimeEventType.TURN_COMPLETED,RuntimeEventType.TURN_FAILED}:
                return False
            core = conn.execute('SELECT state,verification,reply FROM tasks WHERE owner=? AND id=?', (owner.key,ticket.task_id)).fetchone()
            unknown = self.approval_service is not None and event.type==RuntimeEventType.TURN_FAILED and core['state']=='uncertain'
            if core['state'] not in {'completed','failed'} and not unknown:
                raise OfficeConflict('Provider completion lacks a terminal core task')
            message = self.ledger._json({'v':1,'id':event.event_id,'type':'office.assignment.finished',
                'payload':{'assignment_id':ticket.assignment_id,'task_id':ticket.task_id,'staff_id':ticket.staff_id,
                    'generation':ticket.generation,'outcome':core['state'],'verification':core['verification'],'text':'' if unknown else core['reply'] or ''}})
            self.ledger._capacity(conn,owner,outbox=1,bytes_added=len(message.encode()))
            self.ledger._outbox(conn,owner,event.event_id,ticket.assignment_id,None,'event',message)
            conn.execute("UPDATE office_executions SET state=?,updated=? WHERE owner=? AND assignment_id=?", (core['state'],time.time(),owner.key,ticket.assignment_id))
            conn.execute('UPDATE office_assignments SET state=? WHERE owner=? AND id=?', ('uncertain' if unknown else 'completed',owner.key,ticket.assignment_id))
            conn.execute('UPDATE office_continuations SET state=? WHERE owner=? AND assignment_id=?',('uncertain' if unknown else 'completed',owner.key,ticket.assignment_id))
            return True

    def _cancel(self, conn, owner, key):
        row = self.ledger._row(conn,'office_assignments',owner,key)
        if row['state'] == 'completed':
            raise OfficeConflict('Completed assignment cannot be cancelled')
        from .cancellation import cancel_calls
        cancel_calls(conn,owner.key,key)
        conn.execute("UPDATE office_assignments SET state='cancelled' WHERE owner=? AND id=?", (owner.key,key))
        conn.execute("UPDATE office_executions SET state='draining',updated=? WHERE owner=? AND assignment_id=? AND state='running'", (time.time(),owner.key,key))
        conn.execute("UPDATE tasks SET state='cancelled',lease_owner=NULL,lease_until=NULL,reply='Task cancelled' WHERE owner=? AND id=? AND state NOT IN ('completed','failed','cancelled')", (owner.key,row['task_id']))
        conn.execute("UPDATE office_operations SET state='cancelled' WHERE owner=? AND assignment_id=?", (owner.key,key))
        conn.execute("UPDATE office_outbox SET state=CASE WHEN attempts=0 THEN 'cancelled' ELSE 'cancel_pending' END,token=NULL,lease_until=NULL WHERE owner=? AND assignment_id=? AND state!='cancelled'", (owner.key,key))

    def cancel(self, assignment_id):
        with self.directory._connection(write=True) as (conn,owner):
            self._cancel(conn,owner,assignment_id)

    def set_staff_state(self, staff_id, *, paused, disable=False):
        if type(paused) is not bool or type(disable) is not bool or (disable and not paused):
            raise ValueError('Disable requires paused=True; flags must be booleans')
        with self.directory._connection(write=True) as (conn,owner):
            self.directory._row(conn,'office_staff',owner,staff_id)
            if disable and conn.execute("""SELECT 1 FROM office_members m JOIN office_teams t
                ON t.owner=m.owner AND t.id=m.team_id WHERE m.owner=? AND m.staff_id=?
                AND m.role='lead' AND t.enabled=1 AND t.deleted=0 LIMIT 1""", (owner.key,staff_id)).fetchone():
                raise OfficeConflict('Transfer leadership or disable the team before disabling its lead')
            conn.execute('INSERT INTO office_staff_controls VALUES(?,?,?) ON CONFLICT(owner,staff_id) DO UPDATE SET paused=excluded.paused', (owner.key,staff_id,int(paused)))
            if disable:
                conn.execute('UPDATE office_staff SET enabled=0,revision=revision+1,updated=? WHERE owner=? AND id=? AND enabled=1', (time.time(),owner.key,staff_id))
            if paused:
                for row in conn.execute("SELECT id FROM office_assignments WHERE owner=? AND staff_id=? AND state='active'", (owner.key,staff_id)).fetchall():
                    self._cancel(conn,owner,row['id'])

    def reassign(self, assignment_id, staff_id, *, expected_staff_id, staff_revision):
        with self.directory._connection(write=True) as (conn,owner):
            row = self.ledger._row(conn,'office_assignments',owner,assignment_id)
            if row['state'] != 'queued' or row['staff_id'] != expected_staff_id or row['team_id'] is not None:
                raise OfficeConflict('Only unchanged queued individual assignments can be reassigned')
            if conn.execute('SELECT 1 FROM office_operations WHERE owner=? AND assignment_id=?', (owner.key,assignment_id)).fetchone():
                raise OfficeConflict('Resolve frozen tool intents before reassignment')
            staff = self.directory._row(conn,'office_staff',owner,staff_id,staff_revision)
            if not staff['enabled'] or staff['provider'] != 'pythonai':
                raise OfficeConflict('Target staff is not enabled/native')
            payload = json.loads(row['payload'])
            payload['staff'] = self.directory._staff(staff).to_dict()
            payload['request'].update(staff_id=staff_id,staff_revision=staff_revision)
            original_ceiling = payload.get('tool_ceiling',[])
            caller_ceiling = current_context().allowed_tools
            if caller_ceiling is not None:
                payload['tool_ceiling'] = sorted(caller_ceiling if original_ceiling is None else frozenset(original_ceiling) & caller_ceiling)
            serialized = self.ledger._json(payload)
            self.ledger._capacity(conn,owner,bytes_added=max(0,len(serialized.encode())-len(row['payload'].encode())))
            sequence = conn.execute('SELECT MAX(queue_seq)+1 FROM office_assignments WHERE owner=?', (owner.key,)).fetchone()[0]
            conn.execute('UPDATE office_assignments SET staff_id=?,staff_revision=?,payload=?,queue_seq=? WHERE owner=? AND id=?', (staff_id,staff_revision,serialized,sequence,owner.key,assignment_id))

    async def run_once(self):
        ticket = self.claim()
        if ticket is None:
            return False
        runtime,turn = None,None
        finished = False
        original_cancel = current_context().check_cancelled
        with admitted(ticket), execution_scope(task_database=ticket.database,task_id=ticket.task_id,
                workspace=project_workspace(OwnerScope.from_current().project),repository_workspace=None,
                memory=None,episode_id=None,coordinator_node=None,coordinator_owner=None,lifecycle_owner=None,
                check_cancelled=lambda:bool(original_cancel and original_cancel()) or not self._valid(ticket)):
            try:
                # A root budget is fixed before the provider/client factory runs.
                TaskBudget(ticket.owner,ticket.task_id,path=ticket.database)
                with self.directory._connection() as (conn,owner):
                    row = self.ledger._row(conn,'office_assignments',owner,ticket.assignment_id)
                    payload = json.loads(row['payload'])
                    frozen_turn = conn.execute('SELECT tools FROM office_turns WHERE owner=? AND assignment_id=?',(owner.key,ticket.assignment_id)).fetchone()
                staff = payload['staff']
                tools = frozenset(staff['allowed_tools'])
                if self.approval_service is None:
                    tools &= READ_TOOLS
                submitted_ceiling = payload.get('tool_ceiling',[])
                if submitted_ceiling is not None:
                    tools &= frozenset(submitted_ceiling)
                ceiling = current_context().allowed_tools
                if ceiling is not None:
                    tools &= ceiling
                if frozen_turn is not None:
                    tools &= frozenset(json.loads(frozen_turn['tools']))
                request = RuntimeStartRequest(OwnerScope.from_current(),ticket.staff_id,ticket.task_id,ticket.generation,
                    project_workspace(OwnerScope.from_current().project),Provider.PYTHONAI,staff['model'],tools)
                runtime = self.runtime_factory()
                if self.approval_service is not None:
                    if not isinstance(runtime,PythonAIRuntime):
                        raise PermissionError('R3 writes require the managed native adapter')
                    runtime.attach_approvals(self.approval_service,ticket)
                session = await asyncio.wait_for(runtime.start(request),min(self.lease_seconds,self.timeout_seconds))
                self._bind_session(ticket,session)
                turn = await asyncio.wait_for(runtime.send(payload['request']['goal'],request_id=row['request_id']),min(self.lease_seconds,self.timeout_seconds))
                stream = runtime.events().__aiter__()
                pending = asyncio.create_task(anext(stream))
                try:
                    while True:
                        done,_ = await asyncio.wait({pending},timeout=min(self.lease_seconds/3,.25))
                        if original_cancel and original_cancel():
                            self.cancel(ticket.assignment_id)
                            break
                        if not self.renew(ticket):
                            break
                        if pending in done:
                            try:
                                event = pending.result()
                            except StopAsyncIteration:
                                break
                            if self.accept_event(ticket,event,turn.turn_id):
                                finished = True
                                break
                            pending = asyncio.create_task(anext(stream))
                finally:
                    pending.cancel()
                    await asyncio.gather(pending,return_exceptions=True)
            finally:
                stopped = runtime is None
                if runtime is not None:
                    try:
                        if not finished and turn is not None:
                            await asyncio.wait_for(runtime.interrupt(turn.turn_id),min(self.lease_seconds,5))
                        await asyncio.wait_for(runtime.close(),min(self.lease_seconds,5))
                        stopped = getattr(runtime,'stopped',False)
                    except Exception:
                        # Failed shutdown retains its capacity slot; never treat
                        # disconnect or a provider ACK as proof of termination.
                        stopped = False
                with self.directory._connection(write=True) as (conn,owner):
                    if not finished:
                        # Acknowledged stopped cancellation releases capacity;
                        # uncooperative/in-flight workers retain an uncertain slot.
                        state = conn.execute('SELECT state FROM office_assignments WHERE owner=? AND id=?', (owner.key,ticket.assignment_id)).fetchone()[0]
                        cancelled = state == 'cancelled' and stopped
                        conn.execute("UPDATE office_executions SET state=?,updated=? WHERE owner=? AND assignment_id=? AND token=? AND state IN ('running','draining')", ('cancelled' if cancelled else 'uncertain',time.time(),owner.key,ticket.assignment_id,ticket.token))
                        if not cancelled:
                            conn.execute("UPDATE office_assignments SET state='uncertain' WHERE owner=? AND id=? AND state!='cancelled'", (owner.key,ticket.assignment_id))
                            conn.execute("UPDATE tasks SET state='uncertain',lease_owner=NULL,lease_until=NULL WHERE owner=? AND id=? AND state NOT IN ('completed','failed','cancelled')", (owner.key,ticket.task_id))
        return True
