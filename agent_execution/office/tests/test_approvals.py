import asyncio
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from openai.types.chat import ChatCompletionMessage

from agent_execution.approvals import ApprovalStore
from agent_execution.delivery import EventStore
from agent_execution.office.approvals import OfficeApprovals
from agent_execution.office.config import OfficeSettings
from agent_execution.office.contracts import OwnerScope
from agent_execution.office.directory import OfficeConflict
from agent_execution.office.dispatcher import OfficeDispatcher
from agent_execution.office.ledger import OfficeLedger
from agent_execution.office.models import StaffConfig
from agent_execution.office.runtimes.pythonai import PythonAIRuntime
from agent_execution.policy import execution_scope
from agent_execution.tasks import TaskStore


def rows(env,query,args=()):
    with closing(env.store.connect()) as conn:
        return [dict(row) for row in conn.execute(query,args)]


def tool(call_id,name,args):
    return ChatCompletionMessage(role='assistant',tool_calls=[{'id':call_id,'type':'function',
        'function':{'name':name,'arguments':json.dumps(args)}}])


@pytest.fixture
def office(directory,office_env,monkeypatch):
    from task_evaluator import TaskEvaluator,EvaluationResult
    roles = office_env['root']/'roles.json'
    roles.write_text(json.dumps({'api:alice':'admin','api:bob':'admin'}),encoding='utf-8')
    monkeypatch.setenv('ROLES_CONFIG',str(roles))
    monkeypatch.setattr(TaskEvaluator,'evaluate',lambda *a,**k:EvaluationResult(True,1,'fixture',verified=False))
    ledger = OfficeLedger(directory,settings=OfficeSettings(enabled=True,owner_concurrency=2))
    service = OfficeApprovals(ledger,events=EventStore(office_env['root']/'events.db'),approvals=ApprovalStore(office_env['root']/'approvals.db'))
    clients,requests = [],[]
    responses = []
    class Client:
        closed = False
        def __init__(self):
            self.chat = NS(completions=NS(create=self.create))
        def create(self,**kwargs):
            requests.append(kwargs)
            assert responses, 'Unexpected model replay or missing fixture response'
            return NS(choices=[NS(message=responses.pop(0))],usage=NS(total_tokens=8,prompt_tokens=4,completion_tokens=4))
        def close(self): self.closed=True
    def factory():
        client=Client();clients.append(client);return client
    dispatcher = OfficeDispatcher(ledger,approval_service=service,
        runtime_factory=lambda:PythonAIRuntime(client_factory=factory))
    return NS(ledger=ledger,service=service,dispatcher=dispatcher,clients=clients,requests=requests,
        responses=responses,store=ledger.store,workspace=office_env['workspace'],root=office_env['root'],roles=roles)


def submit(env,tools=('write_file','read_file'),request='job'):
    staff = env.ledger.directory.create_staff(StaffConfig(request,enabled=True,allowed_tools=frozenset(tools)))
    return env.ledger.submit(request,staff.id,'Create the approved document',staff_revision=staff.revision)


def turn(env):
    return asyncio.run(env.dispatcher.run_once())


def pending(env):
    result = rows(env,"SELECT * FROM office_calls WHERE state='waiting' ORDER BY created")
    assert result
    return result[-1]


def test_native_wait_approve_resume_executes_only_frozen_content_once(office):
    assignment=submit(office)
    office.responses.extend([tool('provider-call','write_file',{'path':'answer.txt','content':'APPROVED'}),
                             ChatCompletionMessage(role='assistant',content='done')])
    assert turn(office)
    assert not (office.workspace/'answer.txt').exists()
    call=pending(office)
    assert rows(office,'SELECT state FROM tasks')[0]['state']=='office_wait'
    assert rows(office,'SELECT COUNT(*) AS n FROM model_slots')[0]['n']==0
    assert office.clients[0].closed
    snapshot=office.service.snapshot(call['id'])
    assert 'APPROVED' in snapshot['binding']['preview']['diff']
    assert snapshot['binding']['provider_tool_call_id']=='provider-call'
    office.service.reconcile()
    assert office.service.decide(call['id'],True,reason='Reviewed exact text')=='approved'
    assert office.service.decide(call['id'],True,reason='Reviewed exact text')=='approved'
    assert turn(office)
    assert (office.workspace/'answer.txt').read_text(encoding='utf-8')=='APPROVED'
    assert rows(office,'SELECT state FROM office_calls')[0]['state']=='completed'
    assert rows(office,'SELECT state FROM tasks')[0]['state']=='completed'
    assert len(office.requests)==2  # The pending model/tool response was restored, not requested again.
    assert len(rows(office,'SELECT * FROM office_assignments'))==1
    assert not turn(office)
    with pytest.raises(OfficeConflict): office.service.decide(call['id'],False)
    assert office.ledger.get_assignment(assignment['id'])['state']=='completed'


def test_denial_and_reason_survive_native_continuation_without_writes(office):
    submit(office)
    office.responses.extend([tool('call','write_file',{'path':'no.txt','content':'denied'}),
                             ChatCompletionMessage(role='assistant',content='stopped')])
    turn(office);call=pending(office);office.service.reconcile()
    office.service.decide(call['id'],False,reason='Not acceptable')
    assert turn(office)
    assert not (office.workspace/'no.txt').exists()
    saved=rows(office,'SELECT * FROM office_calls')[0]
    assert saved['state']=='denied' and saved['reason']=='Not acceptable'


def test_approved_file_change_fences_execution(office):
    (office.workspace/'answer.txt').write_text('original',encoding='utf-8')
    submit(office)
    office.responses.extend([tool('read','read_file',{'path':'answer.txt'}),
        tool('call','write_file',{'path':'answer.txt','content':'approved'})])
    turn(office);call=pending(office);office.service.reconcile();office.service.decide(call['id'],True)
    (office.workspace/'answer.txt').write_text('human changes',encoding='utf-8')
    assert turn(office)
    assert (office.workspace/'answer.txt').read_text(encoding='utf-8')=='human changes'
    assert any(row['state']=='uncertain' for row in rows(office,'SELECT state FROM office_calls'))


def test_legacy_approval_apis_cannot_decide_or_consume_managed_grants(office):
    submit(office)
    office.responses.append(tool('call','write_file',{'path':'no.txt','content':'no'}))
    turn(office);call=pending(office);office.service.reconcile()
    target=office.service.bridge.targets['approval'].store
    owner=OwnerScope.from_current()
    grant=target.pending(owner.identity,owner.key)[0]
    with pytest.raises(PermissionError): target.decide(grant['id'],owner.identity,owner.key,True)
    office.service.decide(call['id'],True)
    with pytest.raises(PermissionError):
        target.consume(grant['id'],owner.identity,owner.key,grant['action'],json.loads(grant['arguments']))
    assert not (office.workspace/'no.txt').exists()


@pytest.mark.parametrize('change',[{'identity':'api:bob'},{'session':'other'},{'project':'other'},{'source':'websocket'}])
def test_other_owner_cannot_borrow_human_decision(office,change):
    from agent_execution.office.directory import OfficeNotFound
    submit(office)
    office.responses.append(tool('call','write_file',{'path':'no.txt','content':'no'}))
    turn(office);call=pending(office);office.service.reconcile()
    with execution_scope(**change),pytest.raises(OfficeNotFound):
        office.service.decide(call['id'],True)


def test_waiting_cancel_releases_known_parked_slot_and_revokes_grant(office):
    assignment=submit(office)
    office.responses.append(tool('call','write_file',{'path':'no.txt','content':'no'}))
    turn(office);call=pending(office);office.service.reconcile()
    office.dispatcher.cancel(assignment['id'])
    office.service.reconcile()
    assert rows(office,'SELECT state FROM office_executions')[0]['state']=='cancelled'
    assert rows(office,'SELECT state FROM office_continuations')[0]['state']=='cancelled'
    with pytest.raises(OfficeConflict): office.service.decide(call['id'],True)
    assert not turn(office)
    assert not (office.workspace/'no.txt').exists()


def approved_wait(env,path='answer.txt'):
    assignment=submit(env)
    env.responses.append(tool('call','write_file',{'path':path,'content':'APPROVED'}))
    turn(env);call=pending(env);env.service.reconcile();env.service.decide(call['id'],True)
    return assignment,call


def worker(mode):
    import subprocess,sys
    return subprocess.run([sys.executable,'-m','agent_execution.office.tests.resume_worker',mode],
        capture_output=True,text=True,timeout=60)


def test_real_process_restart_restores_native_wait_without_provider_callback(office):
    assignment,call=approved_wait(office)
    session=rows(office,'SELECT session_id FROM office_executions')[0]['session_id']
    result=worker('resume')
    assert result.returncode==0,result.stderr
    assert (office.workspace/'answer.txt').read_text(encoding='utf-8')=='APPROVED'
    assert rows(office,'SELECT session_id FROM office_executions')[0]['session_id']==session
    assert rows(office,'SELECT state FROM office_calls')[0]['state']=='completed'
    assert office.ledger.get_assignment(assignment['id'])['state']=='completed'
    assert len(office.requests)==1  # All resumed execution occurred in a fresh process.


@pytest.mark.parametrize('mode,expected_file',[('consume',False),('effect',True)])
def test_real_crash_after_consumption_or_effect_is_never_replayed(office,mode,expected_file):
    assignment,call=approved_wait(office)
    result=worker(mode)
    assert result.returncode==23,result.stderr
    assert (office.workspace/'answer.txt').exists()==expected_file
    rows(office,'UPDATE office_executions SET lease_until=0')
    office.dispatcher.reconcile()
    assert office.service.recovery_status(assignment['id'])['classification']=='needs_reconciliation'
    assert not turn(office)
    assert rows(office,'SELECT state FROM office_calls')[0]['state']=='uncertain'


def test_result_receipt_survives_crash_before_core_checkpoint_without_repeating_effect(office):
    assignment,call=approved_wait(office)
    result=worker('result')
    assert result.returncode==23,result.stderr
    target=office.workspace/'answer.txt'
    before=target.stat().st_mtime_ns
    assert rows(office,'SELECT state FROM office_calls')[0]['state']=='completed'
    rows(office,'UPDATE office_executions SET lease_until=0')
    office.dispatcher.reconcile()
    assert office.service.recovery_status(assignment['id'])['classification']=='resumable_boundary'
    office.responses.append(ChatCompletionMessage(role='assistant',content='done'))
    assert turn(office)
    assert target.stat().st_mtime_ns==before
    assert office.ledger.get_assignment(assignment['id'])['state']=='completed'


def test_skill_parent_and_nested_calls_are_approved_once_and_reconstruct_completed_steps(office,monkeypatch):
    import base
    from skills import Skill,SkillRegistry
    asset=office.root/'skill';asset.mkdir();(asset/'SKILL.md').write_text('Fixture',encoding='utf-8')
    skill=Skill('fixture','fixture',[],{'type':'object'},['write_file'],{'mode':'tool_sequence','steps':[
        {'tool':'write_file','args':{'path':'a.txt','content':'A'}},
        {'tool':'write_file','args':{'path':'b.txt','content':'B'}}]},asset,'Fixture')
    monkeypatch.setattr(SkillRegistry,'get',lambda *a:skill)
    effects=[]
    def counted(path,content,**kwargs):
        effects.append(path)
        return base.write_file(path,content,**kwargs)
    monkeypatch.setitem(base.TOOLS,'write_file',counted)
    submit(office,tools=('run_skill','write_file'))
    office.responses.extend([tool('skill-call','run_skill',{'name':'fixture','inputs':{}}),ChatCompletionMessage(role='assistant',content='done')])
    turn(office)
    for count in range(3):
        call=pending(office)
        office.service.reconcile();office.service.decide(call['id'],True)
        assert turn(office)
    assert effects==['a.txt','b.txt']
    assert (office.workspace/'a.txt').read_text(encoding='utf-8')=='A'
    assert (office.workspace/'b.txt').read_text(encoding='utf-8')=='B'
    assert len(office.requests)==2
    assert len(rows(office,"SELECT * FROM office_calls WHERE state='completed'"))==3


def test_role_revoked_while_waiting_stops_before_consumption(office):
    assignment,call=approved_wait(office)
    office.roles.write_text(json.dumps({'api:alice':'viewer'}),encoding='utf-8')
    assert not turn(office)
    assert not (office.workspace/'answer.txt').exists()
    assert office.service.recovery_status(assignment['id'])['classification']=='needs_reconciliation'


@pytest.mark.parametrize('field',['arguments','checkpoint','generation','staff'])
def test_mutated_task_binding_cannot_reuse_approval(office,field):
    assignment,call=approved_wait(office)
    if field in {'arguments','checkpoint'}:
        row=rows(office,'SELECT checkpoint FROM tasks')[0]
        checkpoint=json.loads(row['checkpoint'])
        if field=='arguments':
            checkpoint['messages'][-1]['tool_calls'][0]['function']['arguments']=json.dumps({'path':'evil.txt','content':'evil'})
        else: checkpoint['unexpected']='tampered'
        rows(office,'UPDATE tasks SET checkpoint=?',(json.dumps(checkpoint),))
    elif field=='generation': rows(office,'UPDATE office_executions SET generation=2')
    else: rows(office,'UPDATE office_staff SET revision=revision+1')
    try: turn(office)
    except (PermissionError,OfficeConflict): pass
    assert not (office.workspace/'answer.txt').exists()
    assert not (office.workspace/'evil.txt').exists()


def test_approval_expiry_is_not_renewed_by_reconcile_or_retry(office):
    office.ledger.settings=replace(office.ledger.settings,approval_ttl_seconds=1)
    assignment=submit(office)
    office.responses.extend([tool('call','write_file',{'path':'no.txt','content':'no'}),ChatCompletionMessage(role='assistant',content='stopped')])
    turn(office);call=pending(office);office.service.reconcile()
    import time
    time.sleep(1.05)
    office.service.reconcile()
    with pytest.raises(OfficeConflict):office.service.decide(call['id'],True)
    assert turn(office)
    assert not (office.workspace/'no.txt').exists()
    assert rows(office,'SELECT state FROM office_calls')[0]['state']=='expired'


def test_wait_deadline_stops_without_holding_capacity_forever(office):
    assignment=submit(office)
    office.responses.append(tool('call','write_file',{'path':'no.txt','content':'no'}))
    turn(office)
    rows(office,'UPDATE office_executions SET deadline=0')
    office.service.reconcile()
    assert not turn(office)
    assert rows(office,'SELECT state FROM office_executions')[0]['state']=='failed'
    assert not (office.workspace/'no.txt').exists()
