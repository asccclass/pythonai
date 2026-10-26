"""V3 read-operation approval gates. All authority stays in local stores."""
from contextlib import closing
from datetime import timedelta
import json
import time
import uuid

from agent_execution.approvals import ApprovalStore, canonical as approval_canonical
from .approval_outbox import binding_data, pending, sent
from .contracts import APPROVAL_WORKFLOW_NAME, APPROVAL_SIGNAL, READ_TOOLS, validate_snapshot
from .ledger import OperationUncertain
from .store import Store, canonical, digest


def payload(name, arguments, plan=None):
    clean = dict(arguments)
    for field in ('trusted_asset_roots', 'memory', 'episode_id'):
        clean.pop(field, None)
    value = {'tool': name, 'arguments': json.loads(approval_canonical(clean))}
    if plan is not None:
        value['file_precondition'] = plan
    return value


class ApprovalGate:
    def __init__(self, store):
        self.store = store
        with closing(store.tasks.connect()) as conn, conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS temporal_approval_waits(
                submission_id TEXT NOT NULL, revision INTEGER NOT NULL, operation_id TEXT NOT NULL,
                call_id TEXT NOT NULL, name TEXT NOT NULL, payload TEXT NOT NULL,
                fingerprint TEXT NOT NULL, binding TEXT NOT NULL, action TEXT NOT NULL,
                expires REAL NOT NULL, approval_id TEXT, state TEXT NOT NULL,
                PRIMARY KEY(submission_id,revision), UNIQUE(submission_id,operation_id))""")

    def probe(self, cursor, ledger, steps):
        from agent_execution.policy import current_context
        from agent_execution.budgets import active_budget
        from agent_execution.turn_control.continuation import assistant_frontier
        row = self.store.get(ledger.key)
        if cursor['submission_id'] != ledger.key or cursor['next_kind'] != 'tool' or self.store.workflow_type(row) != APPROVAL_WORKFLOW_NAME:
            raise PermissionError('Approval gate requires this V3 tool cursor')
        snapshot = validate_snapshot(ledger.load(cursor['state_ref']))
        from .file_writes import for_cursor, capabilities
        plan = for_cursor(ledger,snapshot)
        tools = capabilities(json.loads(row['context']),self.store.workflow_type(row))
        contract = self.store.verify_budget_contract(row)
        with closing(self.store.tasks.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            ledger._scope(conn, active=False)
            run = steps._row(conn, cursor)
            if run['status'] == 'completed':
                result = json.loads(run['output'])
                ledger.load(result['state_ref'])
                return {'cursor': result, 'ready': True, 'expires': 0}
            if run['status'] != 'ready':
                raise OperationUncertain('Tool already started; approval cannot restart it')
            task = self.store.tasks.get(ledger.owner, ledger.task)
            if task['state'] not in {'pending', 'suspended'}:
                raise PermissionError('Task is not at an approval boundary')
            message, finished = assistant_frontier(snapshot['checkpoint']['messages'])
            call = next((c for c in message.get('tool_calls') or [] if c['id'] not in finished), None)
            if not call:
                raise ValueError('No pending tool at approval boundary')
            name = call['function']['name']
            if name == 'ask_user':
                return {'cursor': cursor, 'ready': True, 'expires': 0}
            context = current_context()
            from permissions import check_tool_permission
            if name not in tools or not check_tool_permission(name) or name not in context.allowed_tools:
                raise PermissionError('Tool is not authorized for V3')
            arguments = json.loads(call['function']['arguments'])
            if not isinstance(arguments, dict):
                raise ValueError('Tool arguments must be an object')
            data = payload(name, arguments, plan)
            operation = f"model:{snapshot['checkpoint']['iteration']}:tool:{call['id']}"
            target = {'submission_id': ledger.key, 'queue': row['queue'], 'operation_id': operation,
                      'owner': ledger.owner, 'task_id': ledger.task}
            binding = binding_data(target)
            category = "temporal_file_write" if plan is not None else "temporal_read"
            action = f"{context.project}:{category}:{ledger.key}:{operation}"
            if contract is None:
                raise PermissionError('Approval requires a pinned root deadline')
            old = conn.execute('SELECT * FROM temporal_approval_waits WHERE submission_id=? AND revision=?',
                               (ledger.key, cursor['revision'])).fetchone()
            known_key = old['approval_id'] if old else None
            if old:
                if (old['fingerprint'], old['binding'], old['action']) != (digest(data), binding, action):
                    raise PermissionError('Approval operation changed')
                expires = old['expires']
            else:
                ledger._capacity(conn, len(canonical(data).encode())+len(binding.encode())+2048)
                expires = min(time.time()+300, contract['deadline'])
                conn.execute("INSERT INTO temporal_approval_waits VALUES(?,?,?,?,?,?,?,?,?,?,NULL,'preparing')",
                    (ledger.key, cursor['revision'], operation, call['id'], name, canonical(data), digest(data), binding, action, expires))
        # The durable task intent precedes this other-database write. A retry
        # finds the same binding even if the decision arrived before waiting.
        active_budget().check()
        approvals = ApprovalStore()
        if str(approvals.path.resolve()) != json.loads(row['context']).get('approval_db'):
            raise PermissionError('Approval database configuration changed')
        if known_key is None:
            key = approvals.request(context.identity, context.session, action, data,
                                    ttl=max(.001, expires-time.time()), binding=target)
        else:
            key = known_key
            from .approval_outbox import get_binding
            with closing(approvals.connect()) as conn:
                if get_binding(conn, key) != binding:
                    raise PermissionError('Persisted approval authority is missing; do not reissue')
        with closing(approvals.connect()) as conn:
            grant = conn.execute('SELECT * FROM approvals WHERE id=? AND identity=? AND session=?',
                                 (key, context.identity, context.session)).fetchone()
            if (not grant or grant['action'] != action or grant['arguments'] != approval_canonical(data)
                    or grant['digest'] != digest(data)):
                raise PermissionError('Approval authority does not match the intent')
            state = 'expired' if min(expires, grant['expires']) <= time.time() else grant['state']
        with closing(self.store.tasks.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            ledger._scope(conn, active=False)
            run = steps._row(conn, cursor)
            if run['status'] != 'ready':
                raise OperationUncertain('Approval boundary was already claimed')
            task = self.store.tasks.get(ledger.owner, ledger.task)
            if task['state'] not in {'pending', 'suspended'}:
                raise PermissionError('Approval result fenced by task state')
            saved = json.loads(canonical(snapshot['checkpoint']))
            phase, reply = snapshot['phase'], None
            if state == 'pending':
                saved.update(suspended_tool_call_id=call['id'],
                             approval_wait={'id': key, 'operation_id': operation, 'revision': cursor['revision']})
                phase = 'waiting_approval'
                reply = f"approval_required: {key}; {name} {canonical(data['arguments'])[:4000]}\nUse /approve {key} or /deny {key}."
                task_state = 'suspended'
            elif state == 'approved':
                task_state = 'pending'
            elif state in {'denied', 'expired', 'cancelled'}:
                task_state, reply = 'failed', f'Error: Operation approval {state}; tool was not executed'
            else:
                raise OperationUncertain('Approval was consumed without confirmed tool completion')
            serialized = canonical(saved)
            self.store.tasks.capacity(conn, len(serialized.encode())+len((reply or '').encode())
                                      -len(canonical(task['checkpoint']).encode())-len((task['reply'] or '').encode()))
            conn.execute('UPDATE tasks SET state=?,phase=?,checkpoint=?,reply=?,lease_owner=NULL,lease_until=NULL,updated=? WHERE owner=? AND id=?',
                         (task_state, phase, serialized, reply, time.time(), ledger.owner, ledger.task))
            conn.execute('UPDATE temporal_approval_waits SET approval_id=?,state=? WHERE submission_id=? AND revision=?',
                         (key, state, ledger.key, cursor['revision']))
            if task_state == 'failed':
                fence = uuid.uuid4().hex
                conn.execute("UPDATE temporal_step_runs SET status='running',fence=? WHERE submission_id=? AND revision=?",
                             (fence, ledger.key, cursor['revision']))
                task = {**task, 'state': task_state, 'phase': phase, 'checkpoint': saved, 'reply': reply}
                result = steps._commit(conn, cursor, fence, ledger, 'finalize', task)
                return {'cursor': result, 'ready': True, 'expires': 0}
        return {'cursor': cursor, 'ready': state == 'approved', 'expires': expires if state == 'pending' else 0}

    async def dispatch(self, client, event):
        target = json.loads(event['binding'])
        binding_data(target)
        row = self.store.get(target['submission_id'], target['owner'])
        if (self.store.workflow_type(row) != APPROVAL_WORKFLOW_NAME or row['queue'] != target['queue']
                or row['task_id'] != target['task_id']):
            raise PermissionError('Decision notification belongs to another execution')
        with closing(self.store.tasks.connect()) as conn:
            intent = conn.execute('SELECT binding FROM temporal_approval_waits WHERE submission_id=? AND operation_id=?',
                                  (row['id'], target['operation_id'])).fetchone()
            if not intent or intent['binding'] != event['binding']:
                raise PermissionError('Decision notification has no matching task intent')
        if row['state'] in {'pending', 'dispatched', 'running'}:
            await client.get_workflow_handle(row['workflow_id']).signal(APPROVAL_SIGNAL,
                {'schema_version': 1, 'submission_id': row['id'], 'event_id': event['id']},
                rpc_timeout=timedelta(seconds=10))
        sent(ApprovalStore(), event['id'])


def authorize(ledger, operation, name, arguments):
    """Called by the existing policy dispatcher after its policy/path checks."""
    row = ledger.store.get(ledger.key)
    if ledger.store.workflow_type(row) != APPROVAL_WORKFLOW_NAME or name == 'ask_user':
        return
    from agent_execution.policy import current_context, _validate_paths
    from agent_execution.budgets import active_budget
    from permissions import current_role, get_user_role, set_current_role, check_tool_permission
    context = current_context()
    from .file_writes import capabilities, load, verify
    from .contracts import FILE_WRITE_TOOLS
    tools = capabilities(json.loads(row['context']),ledger.store.workflow_type(row))
    plan = load(ledger,operation,name,arguments) if name in FILE_WRITE_TOOLS else None
    if plan is not None:
        verify(plan)
    role = current_role()
    if context.identity != 'local':
        levels = {'viewer': 0, 'operator': 1, 'admin': 2}
        role = min((role, get_user_role(context.identity)), key=lambda v: levels.get(v, -1))
    with set_current_role(role):
        if name not in tools or not check_tool_permission(name) or name not in context.allowed_tools:
            raise PermissionError('Role or tool capability changed before approval consumption')
        _validate_paths(arguments, (), plan is not None)
    active_budget().check()
    with closing(ledger.store.tasks.connect()) as conn:
        ledger._scope(conn)
        grant = conn.execute('SELECT * FROM temporal_approval_waits WHERE submission_id=? AND operation_id=?',
                             (ledger.key, operation)).fetchone()
        op = conn.execute('SELECT state FROM operation_ledger WHERE owner=? AND task_id=? AND step_id=?',
                          (ledger.owner, ledger.task, operation)).fetchone()
        if (not grant or grant['state'] != 'approved' or grant['expires'] <= time.time()
                or not op or op['state'] != 'running' or grant['fingerprint'] != digest(payload(name, arguments, plan))):
            raise PermissionError('No exact approved operation at this execution boundary')
    approvals = ApprovalStore()
    if str(approvals.path.resolve()) != json.loads(row['context']).get('approval_db'):
        raise PermissionError('Approval database configuration changed')
    approvals.consume(grant['approval_id'], context.identity, context.session, grant['action'],
                            payload(name, arguments, plan), binding=json.loads(grant['binding']))
    # Consumption is irreversible. A crash/fence after here must never mint a
    # replacement grant or replay the unknown operation.
    active_budget().check()
    with closing(ledger.store.tasks.connect()) as conn:
        ledger._scope(conn)



def assert_consumed(ledger,operation,name,arguments,plan):
    """The file adapter independently requires the exact irreversible grant."""
    from agent_execution.policy import current_context
    from .approval_outbox import get_binding
    context = current_context()
    row = ledger.store.get(ledger.key)
    expected = payload(name,arguments,plan)
    with closing(ledger.store.tasks.connect()) as conn:
        grant = conn.execute('SELECT * FROM temporal_approval_waits WHERE submission_id=? AND operation_id=?',
                             (ledger.key,operation)).fetchone()
        if not grant or grant['fingerprint']!=digest(expected):
            raise PermissionError('File adapter requires an exact consumed approval')
    approvals = ApprovalStore()
    if str(approvals.path.resolve())!=json.loads(row['context']).get('approval_db'):
        raise PermissionError('Approval database configuration changed')
    with closing(approvals.connect()) as conn:
        approval = conn.execute('SELECT * FROM approvals WHERE id=?',(grant['approval_id'],)).fetchone()
        if (not approval or approval['state']!='consumed' or approval['identity']!=context.identity
                or approval['session']!=context.session or approval['action']!=grant['action']
                or approval['arguments']!=approval_canonical(expected) or approval['digest']!=digest(expected)
                or get_binding(conn,grant['approval_id'])!=grant['binding']):
            raise PermissionError('File adapter requires an exact consumed approval')


def cancel_waits(store, row):
    """Best-effort grant retirement; task/submission fences are authoritative."""
    if store.workflow_type(row) != APPROVAL_WORKFLOW_NAME:
        return
    with closing(store.tasks.connect()) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_approval_waits'").fetchone():
            return
        keys = [r[0] for r in conn.execute('SELECT approval_id FROM temporal_approval_waits WHERE submission_id=? AND approval_id IS NOT NULL',
                                         (row['id'],))]
    scope = json.loads(row['context'])
    approvals = ApprovalStore()
    if str(approvals.path.resolve()) != scope.get('approval_db'):
        return
    for key in keys:
        approvals.cancel_wait(key, scope['identity'], scope['session'])

def notify_wait(callback):
    """Project a pending V3 approval through the existing transport event sink."""
    if callback is None:
        return
    from agent_execution.policy import current_context
    from agent_execution.tasks import TaskStore, task_owner, assert_owner_active
    context = current_context()
    if context.task_id is None:
        return
    tasks = TaskStore()
    owner = task_owner(context)
    try:
        task = tasks.get(owner, context.task_id)
    except KeyError:
        return
    if task['state'] != 'suspended' or task['phase'] != 'waiting_approval':
        return
    with closing(tasks.connect()) as conn:
        assert_owner_active(conn, owner)
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='execution_bindings'").fetchone():
            return
        binding = conn.execute('SELECT backend FROM execution_bindings WHERE owner=? AND task_id=?',
                               (owner, context.task_id)).fetchone()
        if not binding or binding[0] != 'temporal':
            return
    store = Store(tasks.path)
    row = store.for_task(owner, context.task_id)
    if row is None or store.workflow_type(row) != APPROVAL_WORKFLOW_NAME:
        return
    from .publication import binding
    delivery = (binding(row) or {}).get('delivery')
    if delivery and delivery.get('kind','websocket') == 'websocket':
        # The durable Worker journal also covers subsequent waits and socket replay.
        return
    key = task['checkpoint']['approval_wait']['id']
    with closing(store.tasks.connect()) as conn:
        intent = conn.execute("SELECT * FROM temporal_approval_waits WHERE submission_id=? AND approval_id=? AND state='pending'",
                              (row['id'], key)).fetchone()
    if intent is None:
        return
    data = json.loads(intent['payload'])
    callback('approval.required', {'approval_id': key, 'name': data['tool'], 'arguments': data['arguments']})
