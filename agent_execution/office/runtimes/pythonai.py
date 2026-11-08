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

from ..admission import assert_admitted
from ..contracts import Provider, RuntimeCapabilities, TurnReceipt
from .base import RuntimeEvent, RuntimeEventType


READ_TOOLS = frozenset({'read_file','list_files','workspace_info'})


class PythonAIRuntime:
    capabilities = RuntimeCapabilities(usage=True)

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
                or not request.allowed_tools <= (READ_TOOLS & frozenset(staff['allowed_tools']))
                or (ceiling is not None and not request.allowed_tools <= frozenset(ceiling))
                or (current_context().allowed_tools is not None and not request.allowed_tools <= current_context().allowed_tools)):
            raise PermissionError('Runtime request exceeds native R2 admission')
        self._request = request
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
        self._turn = uuid.uuid4().hex
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
        except Exception:
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
            with execution_scope(allowed_tools=request.allowed_tools,
                    check_cancelled=lambda:self._cancel.is_set() or bool(original_cancel and original_cancel())):
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
            if event.type in {RuntimeEventType.TURN_COMPLETED,RuntimeEventType.TURN_FAILED,RuntimeEventType.SESSION_CLOSED}:
                return

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
