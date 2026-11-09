"""Native PythonAI loop with isolated task history and injected model/client.

R2 deliberately exposes only local read tools. Hooks, automatic skills, MCP,
and write/exec tools remain unavailable until R3 has complete approval gating.
No provider session or durable side-effect resumption is claimed.
"""
import asyncio
from contextlib import closing
import json
import threading
import uuid
from contextlib import nullcontext

from ..admission import assert_admitted
from ..contracts import Provider, RuntimeCapabilities, TurnReceipt, ResumeMode
from .base import RuntimeEvent, RuntimeEventType


READ_TOOLS = frozenset({'read_file','list_files','workspace_info'})


class PythonAIRuntime:
    @property
    def capabilities(self):
        if self._approval_service is not None:
            return RuntimeCapabilities(usage=True,resume=ResumeMode.DURABLE,tool_approval=True,all_tools_gated=True)
        return RuntimeCapabilities(usage=True)

    def __init__(self, *, client_factory=None, runner=None):
        self._client_factory = client_factory
        self._runner = runner
        self._request = None
        self._session = uuid.uuid4().hex
        self._turn = None
        self._requests = {}
        self._queue = asyncio.Queue(maxsize=16)
        self._cancel = threading.Event()
        self._job = None
        self._closed = False
        self._approval_service = None
        self._admission = None
        self._resume_turn = None

    def attach_approvals(self,service,ticket):
        if self._request is not None or service.store.path != ticket.database:
            raise ValueError('Invalid native approval attachment')
        self._approval_service,self._admission = service,ticket

    async def start(self, request):
        from agent_execution.tasks import TaskStore
        from agent_execution.sessions import project_workspace
        from agent_execution.policy import current_context
        from ..contracts import OwnerScope
        if self._request is not None or self._closed:
            raise ValueError('Runtime is already started or closed')
        with closing(TaskStore().connect()) as conn:
            ticket = assert_admitted(conn,request.owner.key,request.task_id)
            row = conn.execute('SELECT payload,request_id FROM office_assignments WHERE owner=? AND id=?', (ticket.owner,ticket.assignment_id)).fetchone()
            payload = json.loads(row['payload'])
            staff = payload['staff']
            ceiling = payload.get('tool_ceiling',[])
        if (request.provider != Provider.PYTHONAI or request.staff_id != ticket.staff_id
                or request.owner != OwnerScope.from_current()
                or request.workspace != project_workspace(request.owner.project)
                or request.model != staff['model']
                or request.generation != ticket.generation
                or not request.allowed_tools <= (frozenset(staff['allowed_tools']) if self._approval_service else READ_TOOLS & frozenset(staff['allowed_tools']))
                or (ceiling is not None and not request.allowed_tools <= frozenset(ceiling))
                or (current_context().allowed_tools is not None and not request.allowed_tools <= current_context().allowed_tools)):
            raise PermissionError('Runtime request exceeds native R2 admission')
        self._request = request
        if self._approval_service is not None:
            from permissions import current_role
            self._office_role = self._approval_service.role(request.owner)
            if self._office_role != current_role():
                raise PermissionError('Role changed before native admission')
            with closing(TaskStore().connect()) as conn:
                saved = conn.execute("SELECT * FROM office_continuations WHERE owner=? AND assignment_id=? AND state='running'",(ticket.owner,ticket.assignment_id)).fetchone()
                if saved:
                    self._session,self._resume_turn = saved['session_id'],saved['turn_id']
        self._goal = json.loads(row['payload'])['request']['goal']
        self._request_id = row['request_id']
        self._emit(RuntimeEventType.SESSION_STARTED)
        return self._session

    def _emit(self, kind, payload=None):
        self._queue.put_nowait(RuntimeEvent(uuid.uuid4().hex,kind,self._session,
            self._request.generation,self._turn,json.dumps(payload or {},ensure_ascii=False)))

    async def send(self, text, *, request_id):
        from ..contracts import _identifier, _text
        _identifier(request_id,'request_id')
        _text(text,'text')
        if not self._request or self._closed:
            raise ValueError('Runtime is not open')
        if text != self._goal or request_id != self._request_id:
            raise ValueError('Turn does not match the admitted assignment request')
        previous = self._requests.get(request_id)
        if previous:
            if previous[0] != text:
                raise ValueError('Turn request ID reused with different text')
            return previous[1]
        if self._turn is not None:
            raise ValueError('One native runtime is bound to exactly one durable task')
        self._turn = self._resume_turn or uuid.uuid4().hex
        if self._approval_service:
            import server
            self._approval_service.bind_turn(self._admission,self._session,self._turn,self._request.allowed_tools,
                self._request.workspace,self._request.model or server.OLLAMA_MODEL)
        receipt = TurnReceipt(self._turn)
        self._requests[request_id] = (text,receipt)
        self._job = asyncio.create_task(self._run(text))
        return receipt

    async def _run(self, text):
        try:
            reply, usage = await asyncio.to_thread(self._invoke,text)
            if not self._closed and not self._cancel.is_set():
                self._emit(RuntimeEventType.USAGE_UPDATED,usage)
                self._emit(RuntimeEventType.TURN_COMPLETED,{'text':reply})
        except BaseException as error:
            from ..gate import OfficeWait, OfficeUncertain
            if isinstance(error,OfficeWait):
                self._emit(RuntimeEventType.APPROVAL_WAITING,{'call_id':error.call_id})
                return
            if not isinstance(error,(Exception,OfficeUncertain)):
                raise
            if not self._closed:
                # Provider diagnostics stay in core task state, not public events.
                self._emit(RuntimeEventType.TURN_FAILED,{'code':'native_execution_failed'})

    def _invoke(self, text):
        import server
        from agent_execution.budgets import budget_scope, budget_client, TaskBudget, _budget
        from agent_execution.policy import execution_scope, current_context
        from agent_execution.tasks import durable_invoke
        from hooks import HookManager
        from task_evaluator import TaskEvaluator
        request = self._request
        factory = self._client_factory or server.create_ollama_client
        model = request.model or server.OLLAMA_MODEL
        messages = [{'role':'system','content':server.build_system_prompt()},
                    {'role':'user','content':text}]
        original_cancel = current_context().check_cancelled
        budget_token = _budget.set(TaskBudget(request.owner.key,request.task_id))
        raw = None
        try:
            raw = factory()
            client = budget_client(raw)
            from ..gate import actor_scope
            office = actor_scope(self._approval_service,self._admission,self._session,self._turn,request.allowed_tools,model) if self._approval_service else nullcontext()
            with execution_scope(allowed_tools=request.allowed_tools,
                    check_cancelled=lambda:self._cancel.is_set() or bool(original_cancel and original_cancel()) or
                    bool(self._approval_service and self._approval_service.role(request.owner)!=self._office_role)), office:
                with budget_scope() as budget:
                    evaluator = TaskEvaluator(request.workspace,client=client,model=model)
                    runner = self._runner or server.run_agent
                    def invoke(saved):
                        if not any(message.get('role') == 'system' for message in messages):
                            messages.insert(0,{'role':'system','content':server.build_system_prompt()})
                        return runner(messages,resume_state=saved,client_factory=lambda:client,
                            model=model,hook_manager=HookManager(),task_evaluator=evaluator,
                            check_cancelled=current_context().check_cancelled)
                    reply = durable_invoke(invoke,messages,None)
                    return str(reply), {key:budget.snapshot()[key] for key in ('requests','tokens','cost')}
        finally:
            _budget.reset(budget_token)
            close = getattr(raw,'close',None)
            if close:
                close()

    async def events(self):
        while True:
            event = await self._queue.get()
            yield event
            if event.type in {RuntimeEventType.TURN_COMPLETED,RuntimeEventType.TURN_FAILED,RuntimeEventType.SESSION_CLOSED,RuntimeEventType.APPROVAL_WAITING}:
                return

    async def resume(self,session_id,request):
        if self._approval_service is None:
            raise PermissionError('Durable native resume requires Office approval service')
        session = await self.start(request)
        if session != session_id or self._resume_turn is None:
            raise PermissionError('No matching durable native continuation')
        return session

    async def decide_tool(self,decision):
        """Acknowledge an exact *already durable* human decision.

        Native tool_call_id is the gate call ID from approval.waiting; the
        provider's ID is separately bound inside its immutable call record.
        This never manufactures a provider callback or resumes a lost stack.
        OfficeApprovals.decide authenticates the human, and dispatch resumes.
        """
        from ..gate import active_actor
        from ..contracts import OwnerScope
        if (self._approval_service is None or self._request is None or active_actor() is not None
                or OwnerScope.from_current()!=self._request.owner
                or decision.session_id!=self._session or decision.generation!=self._request.generation
                or decision.turn_id!=self._turn):
            raise PermissionError('Decision does not match this native continuation')
        self._approval_service.reconcile()
        with self._approval_service.directory._connection() as (conn,owner):
            row=conn.execute('''SELECT o.approval_state,c.binding FROM office_continuations w
                JOIN office_calls c ON c.owner=w.owner AND c.id=w.call_id
                JOIN office_operations o ON o.owner=c.owner AND o.id=c.operation_id
                WHERE w.owner=? AND w.assignment_id=? AND w.call_id=? AND w.state='ready' ''',
                (owner.key,self._admission.assignment_id,decision.tool_call_id)).fetchone()
            expected='approved' if decision.allow else 'denied'
            if row is None or row['approval_state']!=expected:
                raise PermissionError('No matching durable human decision receipt')

    async def interrupt(self, turn_id):
        if turn_id != self._turn:
            raise ValueError('Turn does not match this runtime')
        self._cancel.set()

    async def close(self):
        if self._closed:
            return
        self._closed = True
        self._cancel.set()
        if self._job is not None:
            # Wait briefly without cancelling the to_thread wrapper: cancelling
            # it would conceal an SDK thread which is still performing I/O.
            await asyncio.wait({self._job},timeout=.5)
        if self._request:
            self._emit(RuntimeEventType.SESSION_CLOSED)

    @property
    def stopped(self):
        return self._job is None or self._job.done()
