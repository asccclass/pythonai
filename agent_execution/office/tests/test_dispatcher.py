import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from contextvars import copy_context
from dataclasses import replace
import json
import time
import threading
from types import SimpleNamespace

import pytest

from agent_execution.budgets import TaskBudget, active_budget, BudgetExhausted, _budget
from agent_execution.office.admission import admitted, assert_admitted
from agent_execution.office.config import OfficeSettings
from agent_execution.office.contracts import OwnerScope, RuntimeStartRequest
from agent_execution.office.directory import OfficeConflict
from agent_execution.office.dispatcher import OfficeDispatcher
from agent_execution.office.ledger import OfficeLedger
from agent_execution.office.models import StaffConfig
from agent_execution.office.runtimes.base import RuntimeEvent, RuntimeEventType as E
from agent_execution.office.runtimes.pythonai import PythonAIRuntime
from agent_execution.policy import current_context, execution_scope
from agent_execution.tasks import TaskStore, RecoveryRequired, save_checkpoint
from permissions import set_current_role


@pytest.fixture
def dispatcher(directory):
    return OfficeDispatcher(OfficeLedger(directory,settings=OfficeSettings(enabled=True,owner_concurrency=2)))


def employee(dispatcher, name='staff', **kwargs):
    return dispatcher.directory.create_staff(StaffConfig(name,enabled=True,**kwargs))


def submit(dispatcher, staff, key='req', **kwargs):
    return dispatcher.ledger.submit(key,staff.id,key,staff_revision=staff.revision,**kwargs)


@contextmanager
def authority(ticket):
    with admitted(ticket), execution_scope(task_database=ticket.database,task_id=ticket.task_id):
        yield


def sql(dispatcher, query, args=()):
    with closing(dispatcher.store.connect()) as conn:
        return conn.execute(query,args).fetchall()


def test_fifo_same_staff_and_parallel_staff(dispatcher):
    a,b = employee(dispatcher,'a'),employee(dispatcher,'b')
    first,second,third = submit(dispatcher,a,'1'),submit(dispatcher,a,'2'),submit(dispatcher,b,'3')
    one,two = dispatcher.claim(),dispatcher.claim()
    assert (one.assignment_id,two.assignment_id) == (first['id'],third['id'])
    assert dispatcher.claim() is None
    assert dispatcher.ledger.get_assignment(second['id'])['state'] == 'queued'
    assert len(sql(dispatcher,'SELECT * FROM tasks')) == 2


def test_competing_workers_have_one_lease(dispatcher):
    staff = employee(dispatcher)
    submit(dispatcher,staff)
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(copy_context().run,dispatcher.claim) for _ in range(6)]
        tickets = [future.result() for future in futures]
    assert sum(ticket is not None for ticket in tickets) == 1
    assert len(sql(dispatcher,'SELECT * FROM office_executions')) == 1


def test_admission_cannot_be_reconstructed_from_task_id(dispatcher):
    assignment = submit(dispatcher,employee(dispatcher))
    ticket = dispatcher.claim()
    store = TaskStore(dispatcher.store.path)
    with pytest.raises(RecoveryRequired):
        store.claim(ticket.owner,ticket.task_id,'req',[])
    with authority(ticket):
        with closing(store.connect()) as conn:
            assert assert_admitted(conn,ticket.owner,ticket.task_id) == ticket
        token = store.claim(ticket.owner,ticket.task_id,'req',[])
        assert token
    assert assignment['task_id'] == ticket.task_id


@pytest.mark.parametrize('field,value', [('owner','other'),('task_id','other'),('token','old'),('generation',2),('staff_id','other')])
def test_admission_exact_binding(dispatcher, field, value):
    submit(dispatcher,employee(dispatcher))
    ticket = dispatcher.claim()
    with authority(replace(ticket,**{field:value})), closing(dispatcher.store.connect()) as conn:
        with pytest.raises(RecoveryRequired):
            assert_admitted(conn,ticket.owner,ticket.task_id)


def test_scoped_database_and_budget_never_follow_global_env(dispatcher, office_env, monkeypatch):
    submit(dispatcher,employee(dispatcher))
    ticket = dispatcher.claim()
    wrong = office_env['root']/'wrong.db'
    monkeypatch.setenv('AGENT_TASK_DB',str(wrong))
    with authority(ticket):
        assert TaskStore().path == ticket.database
        assert TaskBudget(ticket.owner,ticket.task_id).path == ticket.database
    assert not wrong.exists()


def test_expired_execution_is_uncertain_and_never_replayed(dispatcher):
    staff = employee(dispatcher)
    assignment = submit(dispatcher,staff)
    submit(dispatcher,staff,'next')
    ticket = dispatcher.claim()
    sql(dispatcher,'UPDATE office_executions SET lease_until=0')
    assert dispatcher.reconcile() == 1
    assert dispatcher.ledger.get_assignment(assignment['id'])['state'] == 'uncertain'
    assert dispatcher.claim() is None
    with authority(ticket), closing(dispatcher.store.connect()) as conn:
        with pytest.raises(RecoveryRequired):
            assert_admitted(conn,ticket.owner,ticket.task_id)
    assert not dispatcher.renew(ticket)


@pytest.mark.parametrize('role',['operator','viewer'])
def test_dispatch_requires_trusted_admin(dispatcher, role):
    submit(dispatcher,employee(dispatcher))
    with set_current_role(role), pytest.raises(PermissionError):
        dispatcher.claim()


def test_disabled_feature_does_not_create_core_task(dispatcher):
    submit(dispatcher,employee(dispatcher))
    dispatcher.ledger.settings = OfficeSettings()
    with pytest.raises(PermissionError):
        dispatcher.claim()
    assert not sql(dispatcher,'SELECT * FROM tasks')


@pytest.mark.parametrize('criteria,error', [({'version':2,'checks':[]},ValueError),
    ({'version':1,'checks':[{'kind':'tests','target':'tests'}]},PermissionError),
    ({'version':1,'checks':[{'kind':'file_exists','path':'../escape'}]},PermissionError)])
def test_untrusted_acceptance_does_not_admit(dispatcher,criteria,error):
    submit(dispatcher,employee(dispatcher),criteria=criteria)
    with pytest.raises(error):
        dispatcher.claim()
    assert not sql(dispatcher,'SELECT * FROM tasks')
    assert not sql(dispatcher,'SELECT * FROM office_executions')


def test_global_limit_includes_other_owners(dispatcher):
    dispatcher.ledger.settings = OfficeSettings(enabled=True,global_concurrency=1)
    submit(dispatcher,employee(dispatcher))
    assert dispatcher.claim()
    with execution_scope(session='other'):
        submit(dispatcher,employee(dispatcher))
        assert dispatcher.claim() is None
        assert not dispatcher.ledger.list_assignments()[0]['state'] == 'active'


def test_owner_limit_does_not_block_other_owner(dispatcher):
    dispatcher.ledger.settings = OfficeSettings(enabled=True,global_concurrency=2)
    submit(dispatcher,employee(dispatcher,'a'))
    submit(dispatcher,employee(dispatcher,'b'),'b')
    assert dispatcher.claim()
    assert dispatcher.claim() is None
    with execution_scope(session='other'):
        submit(dispatcher,employee(dispatcher))
        assert dispatcher.claim()


def test_auto_route_is_explainable_balanced_and_idempotent(dispatcher):
    staff = [employee(dispatcher,'a'),employee(dispatcher,'b')]
    one = dispatcher.submit_auto('auto1','goal')
    two = dispatcher.submit_auto('auto2','goal')
    assert one['assignment']['staff_id'] != two['assignment']['staff_id']
    assert 'Fewest' in one['reason']
    assert dispatcher.submit_auto('auto1','goal')['assignment'] == one['assignment']
    with pytest.raises(OfficeConflict):
        dispatcher.submit_auto('auto1','changed')
    for item in staff:
        dispatcher.set_staff_state(item.id,paused=True)
    with pytest.raises(OfficeConflict):
        dispatcher.submit_auto('auto3','goal')


def test_pause_disable_cancel_and_reassign(dispatcher):
    a,b = employee(dispatcher,'a'),employee(dispatcher,'b')
    first = submit(dispatcher,a)
    second = submit(dispatcher,a,'second')
    ticket = dispatcher.claim()
    dispatcher.set_staff_state(a.id,paused=True,disable=True)
    assert dispatcher.ledger.get_assignment(first['id'])['state'] == 'cancelled'
    assert TaskStore().get(ticket.owner,ticket.task_id)['state'] == 'cancelled'
    assert not dispatcher.renew(ticket)
    assert dispatcher.claim() is None
    dispatcher.reassign(second['id'],b.id,expected_staff_id=a.id,staff_revision=b.revision)
    assert dispatcher.claim().staff_id == b.id
    with pytest.raises(OfficeConflict):
        dispatcher.reassign(second['id'],a.id,expected_staff_id=b.id,staff_revision=a.revision)


@pytest.mark.parametrize('session_cancel',[False,True])
def test_core_cancel_propagates_to_office_atomically(dispatcher,session_cancel):
    a = submit(dispatcher,employee(dispatcher))
    ticket = dispatcher.claim()
    if session_cancel:
        TaskStore().cancel_session(ticket.owner)
    else:
        TaskStore().cancel(ticket.owner,ticket.task_id)
    assert dispatcher.ledger.get_assignment(a['id'])['state'] == 'cancelled'
    assert sql(dispatcher,'SELECT state FROM office_executions')[0][0] == 'draining'
    assert not dispatcher.renew(ticket)


@pytest.mark.parametrize('mismatch',['session','generation','turn','cancel'])
def test_late_event_fencing(dispatcher,mismatch):
    assignment = submit(dispatcher,employee(dispatcher))
    ticket = dispatcher.claim()
    with authority(ticket):
        dispatcher._bind_session(ticket,'session')
        event = RuntimeEvent('event',E.TURN_COMPLETED,'session',1,'turn','{"artifact":"late"}')
        if mismatch == 'session': event = replace(event,session_id='old')
        if mismatch == 'generation': event = replace(event,generation=2)
        if mismatch == 'turn': event = replace(event,turn_id='old')
        if mismatch == 'cancel': dispatcher.cancel(assignment['id'])
        with pytest.raises((OfficeConflict,RecoveryRequired)):
            dispatcher.accept_event(ticket,event,'turn')
    assert len(dispatcher.ledger.list_outbox()) == 1


class Client:
    closed = False
    def close(self): self.closed = True


def test_native_adapter_reuses_core_budget_and_isolates_history(dispatcher, monkeypatch):
    monkeypatch.setenv('AGENT_TASK_MAX_REQUESTS','2')
    calls,clients = [],[]
    def factory():
        client = Client()
        clients.append(client)
        return client
    def runner(messages,**kwargs):
        assert active_budget() is not None
        assert current_context().allowed_tools == frozenset({'read_file'})
        assert kwargs['client_factory']().raw is clients[-1]
        active_budget().reserve(kwargs['model'],1,1)
        calls.append((kwargs['model'],[dict(item) for item in messages]))
        messages.append({'role':'assistant','content':'done'})
        save_checkpoint('model_received',messages)
        return 'done'
    dispatcher.runtime_factory = lambda:PythonAIRuntime(client_factory=factory,runner=runner)
    a,b = employee(dispatcher,'a',model='model-a',allowed_tools=frozenset({'read_file','run_command'})),employee(dispatcher,'b',model='model-b',allowed_tools=frozenset({'read_file'}))
    one,two = submit(dispatcher,a,'private-a'),submit(dispatcher,b,'private-b')
    assert asyncio.run(dispatcher.run_once())
    assert asyncio.run(dispatcher.run_once())
    assert not asyncio.run(dispatcher.run_once())
    assert [call[0] for call in calls] == ['model-a','model-b']
    assert 'private-a' not in str(calls[1][1])
    assert all(client.closed for client in clients)
    for assignment in (one,two):
        row = TaskStore().get(OwnerScope.from_current().key,assignment['task_id'])
        assert row['state'] == 'completed' and row['verification'] == 'unverified'
        budget = TaskBudget(OwnerScope.from_current().key,assignment['task_id'])
        assert budget.snapshot()['requests'] == 1
        monkeypatch.setenv('AGENT_TASK_MAX_REQUESTS','100')
        restarted = TaskBudget(OwnerScope.from_current().key,assignment['task_id'])
        assert restarted.max_requests == 2
        restarted.reserve('model',1,1)
        with pytest.raises(BudgetExhausted): restarted.reserve('model',1,1)
    assert len(dispatcher.ledger.list_outbox()) == 4


@pytest.mark.parametrize('change',['workspace','model','tools','generation','staff'])
def test_native_start_checks_frozen_admission(dispatcher, office_env, change):
    staff = employee(dispatcher)
    assignment = submit(dispatcher,staff)
    ticket = dispatcher.claim()
    request = RuntimeStartRequest(OwnerScope.from_current(),staff.id,assignment['task_id'],1,office_env['workspace'])
    changes = {'workspace':{'workspace':office_env['root']},'model':{'model':'other'},
        'tools':{'allowed_tools':frozenset({'read_file'})},'generation':{'generation':2},'staff':{'staff_id':'other'}}
    with authority(ticket), pytest.raises(PermissionError):
        asyncio.run(PythonAIRuntime().start(replace(request,**changes[change])))


def test_native_send_idempotence_and_close(dispatcher, office_env):
    staff = employee(dispatcher)
    assignment = submit(dispatcher,staff)
    ticket = dispatcher.claim()
    async def scenario():
        runtime = PythonAIRuntime(client_factory=Client,runner=lambda *args,**kwargs:'done')
        request = RuntimeStartRequest(OwnerScope.from_current(),staff.id,assignment['task_id'],1,office_env['workspace'])
        await runtime.start(request)
        receipt = await runtime.send('req',request_id='req')
        assert await runtime.send('req',request_id='req') == receipt
        with pytest.raises(ValueError): await runtime.send('other',request_id='req')
        with pytest.raises(ValueError): await runtime.send('req',request_id='another')
        events = [event async for event in runtime.events()]
        assert [event.type for event in events] == [E.SESSION_STARTED,E.USAGE_UPDATED,E.TURN_COMPLETED]
        await runtime.close()
        await runtime.close()
        assert runtime.stopped
    with authority(ticket):
        asyncio.run(scenario())


@pytest.mark.parametrize('value',[0,-1,float('nan'),float('inf'),True])
def test_invalid_dispatcher_timeouts(dispatcher,value):
    with pytest.raises(ValueError): OfficeDispatcher(dispatcher.ledger,lease_seconds=value)
    with pytest.raises(ValueError): OfficeDispatcher(dispatcher.ledger,timeout_seconds=value)


def test_actual_native_model_step_uses_injected_model_and_budget(dispatcher, monkeypatch):
    from openai.types.chat import ChatCompletionMessage
    from task_evaluator import TaskEvaluator, EvaluationResult
    requests = []
    def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=ChatCompletionMessage(role='assistant',content='done'))],
            usage=SimpleNamespace(total_tokens=8,prompt_tokens=4,completion_tokens=4))
    raw = Client()
    raw.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
    def evaluate(self,**kwargs):
        assert self.model == 'office-model'
        assert active_budget() is not None
        active_budget().reserve(self.model,1,1)
        return EvaluationResult(True,1,'ok')
    monkeypatch.setattr(TaskEvaluator,'evaluate',evaluate)
    # A pre-admitted native row must remain SQLite even if a rollout enables new Temporal tasks.
    from agent_execution.temporal_backend.admission import owner_key
    monkeypatch.setenv('AGENT_EXECUTION_BACKEND','temporal')
    monkeypatch.setenv('AGENT_TEMPORAL_TRIAL_OWNERS',json.dumps([owner_key(OwnerScope.from_current().key)]))
    staff = employee(dispatcher,model='office-model',allowed_tools=frozenset({'read_file'}))
    assignment = submit(dispatcher,staff)
    dispatcher.runtime_factory = lambda:PythonAIRuntime(client_factory=lambda:raw)
    assert asyncio.run(dispatcher.run_once())
    assert requests[0]['model'] == 'office-model'
    assert requests[0]['tools'][0]['function']['name'] == 'read_file'
    assert len(requests) == 1
    budget = TaskBudget(OwnerScope.from_current().key,assignment['task_id'])
    assert budget.snapshot()['requests'] == 2
    assert raw.closed


def test_parallel_native_workers_keep_same_employee_fifo(dispatcher):
    barrier = threading.Barrier(2)
    seen = []
    def runner(messages,**kwargs):
        seen.append((kwargs['model'],current_context().task_id))
        barrier.wait(timeout=10)
        return 'done'
    dispatcher.runtime_factory = lambda:PythonAIRuntime(client_factory=Client,runner=runner)
    a,b = employee(dispatcher,'a',model='a'),employee(dispatcher,'b',model='b')
    first = submit(dispatcher,a,'first')
    second = submit(dispatcher,a,'second')
    third = submit(dispatcher,b,'third')
    async def scenario():
        assert await asyncio.gather(dispatcher.run_once(),dispatcher.run_once()) == [True,True]
    asyncio.run(scenario())
    assert {item[1] for item in seen} == {first['task_id'],third['task_id']}
    assert dispatcher.claim().assignment_id == second['id']


def test_cancellation_during_native_turn_fences_late_output(dispatcher):
    entered = threading.Event()
    def runner(messages,**kwargs):
        entered.set()
        end = time.monotonic()+5
        while not kwargs['check_cancelled']() and time.monotonic()<end:
            time.sleep(.01)
        return 'late output must not be published'
    dispatcher.runtime_factory = lambda:PythonAIRuntime(client_factory=Client,runner=runner)
    assignment = submit(dispatcher,employee(dispatcher))
    async def scenario():
        job = asyncio.create_task(dispatcher.run_once())
        for _ in range(500):
            if entered.is_set(): break
            await asyncio.sleep(.01)
        assert entered.is_set()
        dispatcher.ledger.cancel_assignment(assignment['id'])
        assert await asyncio.wait_for(job,5)
    asyncio.run(scenario())
    assert dispatcher.ledger.get_assignment(assignment['id'])['state'] == 'cancelled'
    assert len(dispatcher.ledger.list_outbox()) == 1
    assert sql(dispatcher,'SELECT state FROM office_executions')[0][0] == 'cancelled'


def test_timeout_retains_uncertain_capacity(dispatcher):
    from agent_execution.office.contracts import TurnReceipt
    class SilentRuntime:
        stopped = False
        async def start(self,request): return 'silent'
        async def send(self,*args,**kwargs): return TurnReceipt('turn')
        async def events(self):
            await asyncio.Future()
            yield
        async def interrupt(self,turn): pass
        async def close(self): pass
    dispatcher.runtime_factory = SilentRuntime
    dispatcher.timeout_seconds = 2
    dispatcher.lease_seconds = 1
    assignment = submit(dispatcher,employee(dispatcher))
    assert asyncio.run(dispatcher.run_once())
    assert dispatcher.ledger.get_assignment(assignment['id'])['state'] == 'uncertain'
    assert sql(dispatcher,'SELECT state FROM office_executions')[0][0] == 'uncertain'


def test_runtime_start_timeout_and_failed_shutdown_are_fenced(dispatcher):
    class BrokenRuntime:
        async def start(self,request): await asyncio.Future()
        async def close(self): raise RuntimeError('disconnect')
    dispatcher.runtime_factory = BrokenRuntime
    dispatcher.lease_seconds = .05
    assignment = submit(dispatcher,employee(dispatcher))
    with pytest.raises(TimeoutError): asyncio.run(dispatcher.run_once())
    assert dispatcher.ledger.get_assignment(assignment['id'])['state'] == 'uncertain'


def test_untrusted_terminal_payload_cannot_override_core_identity(dispatcher):
    assignment = submit(dispatcher,employee(dispatcher))
    ticket = dispatcher.claim()
    with authority(ticket):
        dispatcher._bind_session(ticket,'session')
        store = TaskStore()
        token = store.claim(ticket.owner,ticket.task_id,'req',[])
        store.finish(ticket.owner,ticket.task_id,token,'completed','core reply')
        event = RuntimeEvent('event',E.TURN_COMPLETED,'session',1,'turn',
            '{"task_id":"other","generation":99,"verification":"verified_completed","text":"fake"}')
        assert dispatcher.accept_event(ticket,event,'turn')
    payload = json.loads(sql(dispatcher,"SELECT payload FROM office_outbox WHERE id='event'")[0][0])['payload']
    assert payload['task_id'] == assignment['task_id']
    assert payload['generation'] == 1 and payload['verification'] == 'unverified'
    assert payload['text'] == 'core reply'


def test_crashed_claim_is_not_reexecuted_by_restarted_dispatcher(dispatcher):
    import subprocess
    import sys
    assignment = submit(dispatcher,employee(dispatcher))
    script = '''
import os
from agent_execution.tasks import TaskStore
from agent_execution.office.store import OfficeStore
from agent_execution.office.directory import OfficeDirectory
from agent_execution.office.ledger import OfficeLedger
from agent_execution.office.dispatcher import OfficeDispatcher
from agent_execution.office.config import OfficeSettings
from agent_execution.policy import execution_scope
from permissions import set_current_role
with execution_scope(source='api',identity='api:alice',project='default',session='directory'),set_current_role('admin'):
    dispatcher=OfficeDispatcher(OfficeLedger(OfficeDirectory(OfficeStore(TaskStore())),settings=OfficeSettings(enabled=True)))
    assert dispatcher.claim()
    os._exit(23)
'''
    result = subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,timeout=30)
    assert result.returncode == 23, result.stderr
    assert dispatcher.ledger.get_assignment(assignment['id'])['state'] == 'active'
    sql(dispatcher,'UPDATE office_executions SET lease_until=0')
    restarted = OfficeDispatcher(dispatcher.ledger)
    assert restarted.reconcile() == 1
    assert restarted.claim() is None
    assert len(sql(dispatcher,'SELECT * FROM tasks')) == 1


def test_v3_upgrade_preserves_intents_and_v4_is_empty(office_env):
    from agent_execution.office.store import _MIGRATIONS, _OBJECTS, schema_version
    store = office_env['store']
    with closing(store.connect()) as conn:
        for version in (1,2,3):
            for name in _MIGRATIONS[version]: conn.execute(_OBJECTS[name])
        conn.execute('INSERT INTO office_schema VALUES(1,3)')
        conn.execute("INSERT INTO office_staff(owner,id,name,name_key,provider,tools,workspace_project,enabled,updated) VALUES('o','s','S','s','pythonai','[]','default',1,1)")
        conn.execute("INSERT INTO office_assignments VALUES('o','a','r','hash','office:a','s',NULL,1,NULL,1,'{}','queued',1)")
        before = tuple(conn.execute('SELECT * FROM office_assignments').fetchone())
    store.migrate(office_env['root']/'v4-backup',offline=True)
    with closing(store.connect()) as conn:
        from agent_execution.office.store import SCHEMA_VERSION
        assert schema_version(conn) == SCHEMA_VERSION
        assert tuple(conn.execute('SELECT * FROM office_assignments').fetchone()) == before
        assert not conn.execute('SELECT * FROM office_executions').fetchall()


def test_completed_execution_session_erasure_removes_v4_metadata(dispatcher, office_env):
    from agent_execution.approvals import ApprovalStore
    from agent_execution.delivery import EventStore
    from agent_execution.office.reconcile import OfficeReconciler
    from agent_execution.workspace_lifecycle.erasure import finalize
    staff = employee(dispatcher)
    dispatcher.set_staff_state(staff.id,paused=False)
    submit(dispatcher,staff)
    dispatcher.runtime_factory = lambda:PythonAIRuntime(client_factory=Client,runner=lambda *a,**k:'done')
    assert asyncio.run(dispatcher.run_once())
    reconciler = OfficeReconciler(dispatcher.ledger,events=EventStore(office_env['root']/'events.db'),approvals=ApprovalStore(office_env['root']/'approvals.db'))
    reconciler.reconcile()
    plan = dispatcher.directory.lifecycle.preview('delete_session')
    dispatcher.directory.lifecycle.confirm(plan['plan_id'])
    finalize(dispatcher.directory.lifecycle,plan['plan_id'])
    assert not sql(dispatcher,'SELECT * FROM office_executions')
    assert not sql(dispatcher,'SELECT * FROM office_staff_controls')


def test_cancelled_unknown_runtime_blocks_directory_and_erasure(dispatcher):
    staff = employee(dispatcher)
    assignment = submit(dispatcher,staff)
    dispatcher.claim()
    dispatcher.set_staff_state(staff.id,paused=True,disable=True)
    current = dispatcher.directory.get_staff(staff.id)
    with pytest.raises(OfficeConflict):
        dispatcher.directory.delete_staff(staff.id,expected_revision=current.revision)
    assert dispatcher.directory.lifecycle.preview('delete_session')['blocking_tasks']
    assert dispatcher.ledger.get_assignment(assignment['id'])['state'] == 'cancelled'


def test_core_task_capacity_failure_rolls_back_admission(dispatcher,monkeypatch):
    from agent_execution.delivery import CapacityError
    monkeypatch.setenv('AGENT_TASK_LIMIT','1')
    TaskStore().claim(OwnerScope.from_current().key,'legacy','goal',[])
    assignment = submit(dispatcher,employee(dispatcher))
    with pytest.raises(CapacityError): dispatcher.claim()
    assert dispatcher.ledger.get_assignment(assignment['id'])['state'] == 'queued'
    assert not sql(dispatcher,'SELECT * FROM office_executions')


def test_outbox_capacity_failure_cannot_publish_terminal_output(dispatcher):
    from agent_execution.office.ledger import OfficeCapacityError
    dispatcher.ledger.limits = replace(dispatcher.ledger.limits,outbox=1)
    assignment = submit(dispatcher,employee(dispatcher))
    dispatcher.runtime_factory = lambda:PythonAIRuntime(client_factory=Client,runner=lambda *a,**k:'done')
    with pytest.raises(OfficeCapacityError): asyncio.run(dispatcher.run_once())
    assert dispatcher.ledger.get_assignment(assignment['id'])['state'] == 'uncertain'
    assert len(dispatcher.ledger.list_outbox()) == 1


def test_staff_disable_preserves_enabled_team_lead_invariant(dispatcher):
    from agent_execution.office.models import TeamConfig, TeamRole
    staff = employee(dispatcher)
    team = dispatcher.directory.create_team(TeamConfig('team'))
    team = dispatcher.directory.set_member(team.id,staff.id,TeamRole.LEAD,expected_revision=team.revision)
    dispatcher.directory.update_team(team.id,TeamConfig('team',enabled=True),expected_revision=team.revision)
    with pytest.raises(OfficeConflict): dispatcher.set_staff_state(staff.id,paused=True,disable=True)
    assert dispatcher.directory.get_staff(staff.id).config.enabled


def test_native_root_budget_does_not_charge_callers_unrelated_task(dispatcher):
    outer = TaskBudget(OwnerScope.from_current().key,'outer')
    token = _budget.set(outer)
    try:
        def runner(*args,**kwargs):
            assert active_budget().key != outer.key
            active_budget().reserve('model',1,1)
            return 'done'
        dispatcher.runtime_factory = lambda:PythonAIRuntime(client_factory=Client,runner=runner)
        submit(dispatcher,employee(dispatcher))
        assert asyncio.run(dispatcher.run_once())
        assert outer.snapshot()['requests'] == 0
        assert active_budget() is outer
    finally:
        _budget.reset(token)


def test_core_cancel_can_cancel_reserved_queued_task_without_core_row(dispatcher):
    assignment = submit(dispatcher,employee(dispatcher))
    store = TaskStore()
    owner = OwnerScope.from_current()
    assert store.cancel(owner.key,assignment['task_id'])
    assert not store.cancel(owner.key,assignment['task_id'])
    assert dispatcher.ledger.get_assignment(assignment['id'])['state'] == 'cancelled'
    assert not sql(dispatcher,'SELECT * FROM tasks')


def test_auto_route_adopts_concurrent_committed_choice(dispatcher,monkeypatch):
    staff = sorted([employee(dispatcher,'a'),employee(dispatcher,'b')],key=lambda item:item.id)
    original = dispatcher.ledger.submit
    def race(request_id,staff_id,goal,**kwargs):
        original(request_id,staff[1].id,goal,staff_revision=staff[1].revision)
        return original(request_id,staff_id,goal,**kwargs)
    monkeypatch.setattr(dispatcher.ledger,'submit',race)
    result = dispatcher.submit_auto('auto','goal')
    assert result['assignment']['staff_id'] == staff[1].id
    assert len(dispatcher.ledger.list_assignments()) == 1


def test_native_reuses_existing_durable_worktree_binding(dispatcher,office_env,monkeypatch):
    import subprocess
    workspace = office_env['workspace']
    def git(*args):
        subprocess.run(['git','-C',str(workspace),*args],check=True,capture_output=True,text=True,timeout=30)
    git('init')
    (workspace/'input.txt').write_text('office input',encoding='utf-8')
    git('add','input.txt')
    git('-c','user.name=Office Test','-c','user.email=office@example.invalid','commit','-m','initial')
    monkeypatch.setenv('AGENT_TASK_WORKTREE','1')
    monkeypatch.setenv('AGENT_WORKTREE_ROOT',str(office_env['root']/'worktrees'))
    seen = []
    def runner(*args,**kwargs):
        path = current_context().workspace
        assert path != workspace
        assert (path/'input.txt').read_text(encoding='utf-8') == 'office input'
        seen.append(path)
        return 'done'
    dispatcher.runtime_factory = lambda:PythonAIRuntime(client_factory=Client,runner=runner)
    assignment = submit(dispatcher,employee(dispatcher))
    assert asyncio.run(dispatcher.run_once())
    binding = TaskStore().workspace_binding(OwnerScope.from_current().key,assignment['task_id'])
    assert binding['repository'] == str(workspace)
    assert len(seen) == 1


def test_submitter_tool_ceiling_survives_broader_worker_and_reassignment(dispatcher):
    staff = employee(dispatcher,'a',allowed_tools=frozenset({'read_file','list_files'}))
    other = employee(dispatcher,'b',allowed_tools=frozenset({'read_file','list_files'}))
    with execution_scope(allowed_tools=frozenset({'read_file'})):
        assignment = submit(dispatcher,staff)
    dispatcher.reassign(assignment['id'],other.id,expected_staff_id=staff.id,staff_revision=other.revision)
    def runner(*args,**kwargs):
        assert current_context().allowed_tools == frozenset({'read_file'})
        return 'done'
    dispatcher.runtime_factory = lambda:PythonAIRuntime(client_factory=Client,runner=runner)
    assert asyncio.run(dispatcher.run_once())


def test_reassignment_can_narrow_but_never_expand_original_tool_ceiling(dispatcher):
    staff = employee(dispatcher,'a',allowed_tools=frozenset({'read_file','list_files'}))
    other = employee(dispatcher,'b',allowed_tools=frozenset({'read_file','list_files'}))
    with execution_scope(allowed_tools=frozenset({'read_file'})):
        assignment = submit(dispatcher,staff)
    with execution_scope(allowed_tools=frozenset({'list_files'})):
        dispatcher.reassign(assignment['id'],other.id,expected_staff_id=staff.id,staff_revision=other.revision)
    payload = json.loads(sql(dispatcher,'SELECT payload FROM office_assignments')[0][0])
    assert payload['tool_ceiling'] == []
    with pytest.raises(PermissionError): dispatcher.ledger.prepare_operation(assignment['id'],'op','read_file',{})


def test_legacy_intent_without_submitter_ceiling_gets_no_tools(dispatcher):
    staff = employee(dispatcher,allowed_tools=frozenset({'read_file'}))
    assignment = submit(dispatcher,staff)
    payload = json.loads(sql(dispatcher,'SELECT payload FROM office_assignments')[0][0])
    payload.pop('tool_ceiling')
    sql(dispatcher,'UPDATE office_assignments SET payload=?',(json.dumps(payload),))
    def runner(*args,**kwargs):
        assert current_context().allowed_tools == frozenset()
        return 'done'
    dispatcher.runtime_factory = lambda:PythonAIRuntime(client_factory=Client,runner=runner)
    assert asyncio.run(dispatcher.run_once())
