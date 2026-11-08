from contextlib import closing
from dataclasses import replace
import json
import time

import pytest

from agent_execution.approvals import ApprovalStore
from agent_execution.delivery import EventStore
from agent_execution.office.contracts import OwnerScope
from agent_execution.office.directory import OfficeConflict
from agent_execution.office.ledger import OfficeLedger
from agent_execution.office.models import StaffConfig
from agent_execution.office.reconcile import OfficeReconciler
from agent_execution.workspace_lifecycle.erasure import finalize
from agent_execution.policy import execution_scope
from permissions import set_current_role


@pytest.fixture
def bridge(directory,office_env):
    ledger = OfficeLedger(directory)
    staff = directory.create_staff(StaffConfig('員工',enabled=True,allowed_tools=frozenset({'read_file'})))
    assignment = ledger.submit('request',staff.id,'工作',staff_revision=1)
    operation = ledger.prepare_operation(assignment['id'],'op','read_file',{'path':'input.txt'})
    events = EventStore(office_env['root']/'events.db')
    approvals = ApprovalStore(office_env['root']/'approvals.db')
    reconciler = OfficeReconciler(ledger,events=events,approvals=approvals)
    return ledger,assignment,operation,reconciler,events,approvals


def approval(bridge):
    *_,approvals = bridge
    owner = OwnerScope.from_current()
    with closing(approvals.connect()) as conn:
        return dict(conn.execute('SELECT * FROM approvals WHERE identity=? AND session=?',(owner.identity,owner.key)).fetchone())


def test_event_and_approval_reconcile_idempotently_across_restarts_and_ack(bridge):
    ledger,assignment,operation,reconciler,events,approvals = bridge
    assert {r['state'] for r in reconciler.reconcile()} == {'delivered'}
    grant = approval(bridge)
    assert grant['state'] == 'pending'
    assert ledger.get_operation(operation['id'])['approval_state'] == 'pending'
    owner = OwnerScope.from_current()
    sequence,_,messages = events.snapshot(owner.key)
    assert len(messages) == 1 and messages[0]['payload']['assignment_id'] == assignment['id']
    events.ack(owner.key,sequence)
    again = OfficeReconciler(ledger,events=events,approvals=approvals)
    again.reconcile()
    assert events.snapshot(owner.key)[2] == []
    with closing(approvals.connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM approvals').fetchone()[0] == 1
    assert approval(bridge)['id'] == grant['id']


@pytest.mark.parametrize('decision,expected',[(True,'ready'),(False,'denied')])
def test_human_decisions_are_observed_but_do_not_execute_or_consume(bridge,decision,expected):
    ledger,assignment,operation,reconciler,_,approvals = bridge
    reconciler.reconcile()
    grant = approval(bridge)
    owner = OwnerScope.from_current()
    approvals.decide(grant['id'],owner.identity,owner.key,decision)
    reconciler.reconcile()
    assert ledger.get_operation(operation['id'])['state'] == expected
    assert ledger.get_assignment(assignment['id'])['state'] == 'queued'
    assert approval(bridge)['state'] == ('approved' if decision else 'denied')


def test_crash_after_target_commit_recovers_from_receipt_without_new_grant(bridge,monkeypatch):
    ledger,assignment,operation,reconciler,_,approvals = bridge
    original = reconciler.targets['approval'].publish
    def committed_then_crashed(owner,row):
        original(owner,row)
        raise RuntimeError('simulated lost acknowledgement')
    monkeypatch.setattr(reconciler.targets['approval'],'publish',committed_then_crashed)
    assert 'uncertain' in {r['state'] for r in reconciler.reconcile()}
    original_id = approval(bridge)['id']
    monkeypatch.setattr(reconciler.targets['approval'],'publish',original)
    assert {r['state'] for r in reconciler.reconcile()} == {'delivered'}
    assert approval(bridge)['id'] == original_id


def test_missing_receipt_after_started_attempt_is_not_automatically_republished(bridge,monkeypatch):
    ledger,_,_,reconciler,_,approvals = bridge
    calls = []
    def failed_before_commit(*args):
        calls.append(1)
        raise OSError('private credential must not leak')
    monkeypatch.setattr(reconciler.targets['approval'],'publish',failed_before_commit)
    reconciler.reconcile()
    reconciler.reconcile()
    assert calls == [1]
    assert any(row['state']=='uncertain' and row['error']=='missing_receipt' for row in ledger.list_outbox())
    with closing(approvals.connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM approvals').fetchone()[0] == 0


def test_lost_publisher_lease_probes_existing_receipt(bridge):
    ledger,_,_,reconciler,_,_ = bridge
    reconciler.reconcile()
    with closing(ledger.store.connect()) as conn:
        conn.execute("UPDATE office_outbox SET state='sending',token='old',lease_until=?", (time.time()+30,))
    assert {r['state'] for r in reconciler.reconcile()} == {'busy'}
    with closing(ledger.store.connect()) as conn:
        conn.execute('UPDATE office_outbox SET lease_until=0')
    assert {r['state'] for r in reconciler.reconcile()} == {'delivered'}


def test_expired_original_request_never_creates_fresh_approval(bridge):
    ledger,assignment,operation,reconciler,_,_ = bridge
    with closing(ledger.store.connect()) as conn:
        row = conn.execute("SELECT id,payload FROM office_outbox WHERE kind='approval'").fetchone()
        payload = json.loads(row['payload']); payload['expires']=time.time()-1
        from agent_execution.office.persistence import canonical,digest
        text=canonical(payload)
        conn.execute('UPDATE office_outbox SET payload=?,fingerprint=? WHERE id=?',(text,digest(text),row['id']))
    reconciler.reconcile()
    original_id=approval(bridge)['id']
    assert ledger.get_operation(operation['id'])['state']=='expired'
    assert ledger.prepare_operation(assignment['id'],'op','read_file',{'path':'input.txt'})['id']==operation['id']
    reconciler.reconcile()
    assert approval(bridge)['id']==original_id


def test_cancel_installs_target_barriers_and_fences_late_publishers(bridge):
    ledger,assignment,_,reconciler,events,_ = bridge
    reconciler.reconcile()
    with closing(ledger.store.connect()) as conn:
        old_rows=[dict(row) for row in conn.execute('SELECT * FROM office_outbox')]
    owner=OwnerScope.from_current()
    ledger.cancel_assignment(assignment['id'])
    assert {r['state'] for r in reconciler.reconcile()}=={'cancelled'}
    assert approval(bridge)['state']=='cancelled'
    for row in old_rows: reconciler.targets[row['kind']].publish(owner,row)
    assert len(events.snapshot(owner.key)[2])==1
    assert approval(bridge)['state']=='cancelled'


def test_consumed_grant_is_unknown_effect_and_blocks_cancel_and_cleanup(bridge):
    ledger,assignment,operation,reconciler,_,approvals=bridge
    reconciler.reconcile()
    owner=OwnerScope.from_current(); grant=approval(bridge)
    approvals.decide(grant['id'],owner.identity,owner.key,True)
    approvals.consume(grant['id'],owner.identity,owner.key,grant['action'],json.loads(grant['arguments']))
    reconciler.reconcile()
    assert ledger.get_operation(operation['id'])['state']=='uncertain'
    assert ledger.get_assignment(assignment['id'])['state']=='uncertain'
    with pytest.raises(OfficeConflict): ledger.cancel_assignment(assignment['id'])
    assert ledger.directory.lifecycle.preview('delete_session')['blocking_tasks']


def test_wrong_path_or_replaced_target_identity_is_rejected(bridge,tmp_path):
    ledger,_,_,reconciler,events,approvals=bridge
    reconciler.reconcile()
    other=ApprovalStore(tmp_path/'other-approval.db')
    with pytest.raises(OfficeConflict): OfficeReconciler(ledger,events=events,approvals=other).reconcile()
    with closing(approvals.connect()) as conn:
        conn.execute("UPDATE office_bridge_meta SET instance='replaced'")
        conn.commit()
    with pytest.raises(OfficeConflict): reconciler.reconcile()


def test_target_receipt_capacity_does_not_create_unreceipted_grant(bridge):
    ledger,assignment,_,reconciler,_,approvals=bridge
    reconciler.reconcile()
    ledger.prepare_operation(assignment['id'],'op-two','read_file',{'path':'second'})
    reconciler.targets['approval'].limits=replace(ledger.limits,target_receipts=1)
    reconciler.reconcile()
    with closing(approvals.connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM approvals').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM office_bridge_receipts').fetchone()[0]==1


def test_all_db_paths_must_be_distinct_and_private(bridge):
    ledger,_,_,_,events,approvals=bridge
    with pytest.raises(ValueError): OfficeReconciler(ledger,events=events,approvals=ApprovalStore(events.path))
    with pytest.raises(ValueError): OfficeReconciler(ledger,events=EventStore(ledger.store.path),approvals=approvals)


def test_reconciliation_is_owner_scoped_and_requires_admin(bridge):
    ledger,_,_,reconciler,_,_=bridge
    with execution_scope(identity='api:bob'):
        assert reconciler.reconcile()==()
    with set_current_role('viewer'),pytest.raises(PermissionError): reconciler.reconcile()


def test_actual_session_delete_scrubs_all_three_databases_and_retains_hash_fence(bridge):
    ledger,assignment,_,reconciler,events,approvals=bridge
    reconciler.reconcile()
    owner=OwnerScope.from_current()
    with closing(ledger.store.connect()) as conn:
        rows=[dict(row) for row in conn.execute('SELECT * FROM office_outbox')]
    ledger.cancel_assignment(assignment['id']); reconciler.reconcile()
    plan=ledger.directory.lifecycle.preview('delete_session')
    assert not plan['blocking_tasks']
    ledger.directory.lifecycle.confirm(plan['plan_id'])
    assert finalize(ledger.directory.lifecycle,plan['plan_id'])['state']=='deleted'
    assert events.snapshot(owner.key)[2]==[]
    with closing(approvals.connect()) as conn:
        # The native session-delete authorization retains its existing audit row.
        assert conn.execute('SELECT COUNT(*) FROM approvals WHERE session=?',(owner.key,)).fetchone()[0]==0
        assert conn.execute('SELECT COUNT(*) FROM office_bridge_receipts').fetchone()[0]==0
    for row in rows:
        with pytest.raises(PermissionError): reconciler.targets[row['kind']].publish(owner,row)
    with pytest.raises(PermissionError): ledger.get_assignment(assignment['id'])


def test_cancellation_retry_keeps_cancel_intent_after_transient_error(bridge,monkeypatch):
    ledger,assignment,_,reconciler,_,_=bridge
    reconciler.reconcile(); ledger.cancel_assignment(assignment['id'])
    old=reconciler.targets['approval'].cancel
    def fail(*args): raise OSError('temporary')
    monkeypatch.setattr(reconciler.targets['approval'],'cancel',fail)
    reconciler.reconcile()
    assert any(row['state']=='cancel_pending' for row in ledger.list_outbox())
    monkeypatch.setattr(reconciler.targets['approval'],'cancel',old)
    assert {r['state'] for r in reconciler.reconcile()}=={'cancelled'}


@pytest.mark.parametrize('phase',['before_commit','after_commit'])
def test_actual_process_exit_at_target_transaction_boundary(bridge,phase):
    import subprocess
    import sys
    ledger,_,operation,reconciler,_,approvals=bridge
    script='''
import os
from pathlib import Path
from agent_execution.policy import execution_scope
from agent_execution.tasks import TaskStore
from agent_execution.office.store import OfficeStore
from agent_execution.office.directory import OfficeDirectory
from agent_execution.office.ledger import OfficeLedger
from agent_execution.office.reconcile import OfficeReconciler
from agent_execution.delivery import EventStore
from agent_execution.approvals import ApprovalStore
with execution_scope(source='api',identity='api:alice',project='default',session='directory',workspace=Path(os.environ['AGENT_WORKSPACE_ROOT'])):
    ledger=OfficeLedger(OfficeDirectory(OfficeStore(TaskStore())))
    reconciler=OfficeReconciler(ledger,events=EventStore(),approvals=ApprovalStore())
    target=reconciler.targets['approval']
    if PHASE=='before_commit':
        def stop(*args): os._exit(23)
        target.store.audit=stop
    else:
        original=target.publish
        def stop(owner,row):
            original(owner,row)
            os._exit(23)
        target.publish=stop
    reconciler.reconcile()
'''
    result=subprocess.run([sys.executable,'-c',"PHASE="+repr(phase)+'\n'+script],capture_output=True,text=True,timeout=45)
    assert result.returncode==23,result.stderr
    with closing(ledger.store.connect()) as conn:
        conn.execute("UPDATE office_outbox SET lease_until=0 WHERE kind='approval'")
    reconciler.reconcile()
    with closing(approvals.connect()) as conn:
        count=conn.execute('SELECT COUNT(*) FROM approvals').fetchone()[0]
    assert count==(0 if phase=='before_commit' else 1)
    if phase=='before_commit':
        assert any(row['state']=='uncertain' for row in ledger.list_outbox())
    else:
        assert ledger.get_operation(operation['id'])['approval_state']=='pending'


def test_cancellation_during_publication_fences_stale_source_ack(bridge,monkeypatch):
    ledger,assignment,_,reconciler,_,_=bridge
    original=reconciler.targets['approval'].publish
    def cancel_after_commit(owner,row):
        original(owner,row)
        ledger.cancel_assignment(assignment['id'])
    monkeypatch.setattr(reconciler.targets['approval'],'publish',cancel_after_commit)
    assert 'fenced' in {item['state'] for item in reconciler.reconcile()}
    reconciler.reconcile()
    assert ledger.get_assignment(assignment['id'])['state']=='cancelled'
    assert approval(bridge)['state']=='cancelled'
    assert all(row['state']=='cancelled' for row in ledger.list_outbox())


def test_event_and_receipt_rollback_together_if_receipt_write_fails(bridge):
    ledger,_,_,reconciler,events,_=bridge
    with ledger.directory._connection(write=True) as (conn,owner):
        reconciler._bind(conn,owner,'event')
    with closing(events.connect()) as conn,conn:
        conn.execute("""CREATE TRIGGER injected_receipt_failure BEFORE INSERT ON office_bridge_receipts
            BEGIN SELECT RAISE(ABORT,'injected receipt failure'); END""")
    reconciler.reconcile()
    assert events.snapshot(OwnerScope.from_current().key)==(0,0,[])
    with closing(events.connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM office_bridge_receipts').fetchone()[0]==0


def test_session_erasure_preserves_other_owner_in_source_and_both_targets(bridge):
    ledger,assignment,_,reconciler,events,approvals=bridge
    reconciler.reconcile()
    with execution_scope(identity='api:bob'):
        bob=OwnerScope.from_current()
        staff=ledger.directory.create_staff(StaffConfig('Bob',enabled=True,allowed_tools=frozenset({'read_file'})))
        work=ledger.submit('request',staff.id,'Bob goal',staff_revision=1)
        ledger.prepare_operation(work['id'],'op','read_file',{})
        reconciler.reconcile()
    ledger.cancel_assignment(assignment['id']); reconciler.reconcile()
    plan=ledger.directory.lifecycle.preview('delete_session')
    ledger.directory.lifecycle.confirm(plan['plan_id']); finalize(ledger.directory.lifecycle,plan['plan_id'])
    assert len(events.snapshot(bob.key)[2])==1
    assert len(approvals.pending(bob.identity,bob.key))==1
    with execution_scope(identity='api:bob'):
        assert ledger.get_assignment(work['id'])['state']=='queued'


def test_tampered_source_payload_is_not_published(bridge):
    ledger,_,_,reconciler,events,_=bridge
    with closing(ledger.store.connect()) as conn:
        conn.execute("UPDATE office_outbox SET payload=payload||' ' WHERE kind='event'")
    with pytest.raises(OfficeConflict): reconciler.reconcile()
    assert events.snapshot(OwnerScope.from_current().key)[2]==[]


def test_event_transaction_helper_requires_a_transaction(bridge):
    *_,events,approvals=bridge
    with closing(events.connect()) as conn,pytest.raises(ValueError):
        events.append_transaction(conn,'owner',{'type':'notify'})


def test_cancel_pending_holds_staff_guard_until_target_compensation_settles(bridge):
    ledger,assignment,_,reconciler,_,_=bridge
    reconciler.reconcile()
    staff=ledger.directory.get_staff(assignment['staff_id'])
    ledger.cancel_assignment(assignment['id'])
    with pytest.raises(OfficeConflict):
        ledger.directory.update_staff(staff.id,replace(staff.config,name='new'),expected_revision=1)
    reconciler.reconcile()
    assert ledger.directory.update_staff(staff.id,replace(staff.config,name='new'),expected_revision=1).revision==2


def test_target_helpers_refuse_cross_owner_row_reuse(bridge):
    ledger,_,_,reconciler,_,_=bridge
    reconciler.reconcile()
    with closing(ledger.store.connect()) as conn:
        row=dict(conn.execute("SELECT * FROM office_outbox WHERE kind='approval'").fetchone())
    row['owner']='another owner'
    with pytest.raises(PermissionError):
        reconciler.targets['approval'].publish(OwnerScope.from_current(),row)


def test_bounded_reconciliation_does_not_starve_new_intents_with_old_approval_polls(bridge):
    ledger,assignment,_,reconciler,_,_=bridge
    reconciler.reconcile()
    new=ledger.prepare_operation(assignment['id'],'new','read_file',{})
    reconciler.reconcile(limit=1)
    assert ledger.get_operation(new['id'])['approval_state']=='pending'


def test_corrupt_receipt_cannot_cancel_another_approval(bridge):
    ledger,assignment,_,reconciler,_,approvals=bridge
    reconciler.reconcile()
    foreign=approvals.request('foreign','foreign','foreign',{})
    with closing(approvals.connect()) as conn,conn:
        conn.execute('UPDATE office_bridge_receipts SET record_id=?',(foreign,))
    ledger.cancel_assignment(assignment['id'])
    reconciler.reconcile()
    with closing(approvals.connect()) as conn:
        assert conn.execute('SELECT state FROM approvals WHERE id=?',(foreign,)).fetchone()[0]=='pending'
    assert any(row['state']=='cancel_pending' for row in ledger.list_outbox())


def test_consumption_racing_cancellation_becomes_uncertain(bridge):
    ledger,assignment,operation,reconciler,_,approvals=bridge
    reconciler.reconcile()
    owner=OwnerScope.from_current(); grant=approval(bridge)
    approvals.decide(grant['id'],owner.identity,owner.key,True)
    ledger.cancel_assignment(assignment['id'])
    approvals.consume(grant['id'],owner.identity,owner.key,grant['action'],json.loads(grant['arguments']))
    reconciler.reconcile()
    assert ledger.get_assignment(assignment['id'])['state']=='uncertain'
    assert ledger.get_operation(operation['id'])['state']=='uncertain'
    assert ledger.directory.lifecycle.preview('delete_session')['blocking_tasks']


def test_office_grants_do_not_use_the_legacy_session_namespace(bridge):
    *_,reconciler,events,approvals=bridge
    reconciler.reconcile()
    owner=OwnerScope.from_current(); grant=approval(bridge)
    assert approvals.pending(owner.identity,owner.session)==[]
    with pytest.raises(PermissionError): approvals.decide(grant['id'],owner.identity,owner.session,True)
    assert events.snapshot(EventStore.owner(owner.identity,owner.project,owner.session))[2]==[]


def test_different_task_databases_cannot_share_office_targets(bridge,office_env):
    from agent_execution.tasks import TaskStore
    from agent_execution.office.store import OfficeStore
    from agent_execution.office.directory import OfficeDirectory
    _,_,_,reconciler,events,approvals=bridge
    reconciler.reconcile()
    second=OfficeStore(TaskStore(office_env['root']/'second-tasks.db'))
    second.migrate(office_env['root']/'second-backup',offline=True)
    other=OfficeLedger(OfficeDirectory(second))
    staff=other.directory.create_staff(StaffConfig('other',enabled=True))
    other.submit('other',staff.id,'other source',staff_revision=1)
    with pytest.raises(OfficeConflict): OfficeReconciler(other,events=events,approvals=approvals).reconcile()
    assert all(row['attempts']==0 for row in other.list_outbox())
