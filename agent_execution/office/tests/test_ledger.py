from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from contextvars import copy_context
from dataclasses import replace

import pytest

from agent_execution.office.config import OfficeSettings
from agent_execution.office.contracts import OwnerScope
from agent_execution.office.directory import OfficeConflict, OfficeNotFound
from agent_execution.office.ledger import OfficeLedger, OfficeCapacityError
from agent_execution.office.models import StaffConfig, TeamConfig, TeamRole
from agent_execution.office.persistence import PersistenceLimits, canonical
from agent_execution.policy import execution_scope
from agent_execution.tasks import RecoveryRequired
from permissions import set_current_role


@pytest.fixture
def ledger(directory):
    return OfficeLedger(directory)


@pytest.fixture
def staff(directory):
    return directory.create_staff(StaffConfig('員工',enabled=True,allowed_tools=frozenset({'read_file'})))


def submit(ledger,staff,key='request',**kwargs):
    return ledger.submit(key,staff.id,'整理資料',staff_revision=staff.revision,**kwargs)


def test_submission_is_atomic_idempotent_and_reserves_one_task_id(ledger,staff,office_env):
    first = submit(ledger,staff)
    assert submit(ledger,staff) == first
    assert OfficeLedger(ledger.directory).get_assignment(first['id']) == first
    assert len(ledger.list_outbox()) == 1
    owner = OwnerScope.from_current()
    with closing(ledger.store.connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM office_assignments WHERE owner=?',(owner.key,)).fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM tasks WHERE owner=?',(owner.key,)).fetchone()[0] == 0
    with pytest.raises(RecoveryRequired):
        office_env['tasks'].claim(owner.key,first['task_id'],'bypass',[])
    # Legacy unrelated tasks still work with the office schema present.
    assert office_env['tasks'].claim(owner.key,'legacy','legacy goal',[])
    with pytest.raises(OfficeConflict):
        ledger.submit('request',staff.id,'不同需求',staff_revision=1)


def test_concurrent_same_request_creates_one_assignment_and_one_outbox(ledger,staff):
    contexts = [copy_context(),copy_context()]
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(context.run,submit,ledger,staff) for context in contexts]
        results = [future.result(20) for future in futures]
    assert results[0] == results[1]
    assert len(ledger.list_assignments()) == len(ledger.list_outbox()) == 1


def test_operation_fingerprint_includes_nested_confirm_and_preserves_snapshot(ledger,staff):
    assignment = submit(ledger,staff)
    args = {'path':'input.txt','nested':{'confirm':False}}
    operation = ledger.prepare_operation(assignment['id'],'tool-request','read_file',args)
    args['nested']['confirm'] = True
    with pytest.raises(OfficeConflict):
        ledger.prepare_operation(assignment['id'],'tool-request','read_file',args)
    assert ledger.prepare_operation(assignment['id'],'tool-request','read_file',{'nested':{'confirm':False},'path':'input.txt'}) == operation
    assert len(ledger.list_outbox()) == 2
    assert 'payload' not in ledger.get_operation(operation['id'])


@pytest.mark.parametrize('scope',[{'identity':'api:bob'},{'source':'websocket'},{'session':'other'},{'project':'other'}])
def test_requests_and_reads_are_isolated_by_every_owner_dimension(ledger,staff,scope):
    assignment = submit(ledger,staff)
    operation = ledger.prepare_operation(assignment['id'],'op','read_file',{})
    with execution_scope(**scope):
        assert ledger.list_assignments() == ledger.list_outbox() == ()
        for callback in (lambda:ledger.get_assignment(assignment['id']),lambda:ledger.get_operation(operation['id']),
                         lambda:ledger.cancel_assignment(assignment['id'])):
            with pytest.raises(OfficeNotFound): callback()
        other = ledger.directory.create_staff(StaffConfig('other',enabled=True))
        assert submit(ledger,other)['id'] != assignment['id']


def test_live_assignments_guard_directory_settings_and_team_membership(ledger,staff):
    team = ledger.directory.create_team(TeamConfig('team'))
    team = ledger.directory.set_member(team.id,staff.id,TeamRole.LEAD,expected_revision=1)
    team = ledger.directory.update_team(team.id,TeamConfig('team',True),expected_revision=team.revision)
    assignment = submit(ledger,staff,team_id=team.id,team_revision=team.revision)
    for callback in (
        lambda:ledger.directory.update_staff(staff.id,replace(staff.config,name='changed'),expected_revision=1),
        lambda:ledger.directory.update_team(team.id,TeamConfig('changed',True),expected_revision=team.revision),
        lambda:ledger.directory.remove_member(team.id,staff.id,expected_revision=team.revision),
        lambda:ledger.directory.set_member(team.id,staff.id,TeamRole.MEMBER,expected_revision=team.revision),
    ):
        with pytest.raises(OfficeConflict): callback()
    ledger.cancel_assignment(assignment['id'])
    assert submit(ledger,staff,team_id=team.id,team_revision=team.revision)['state'] == 'cancelled'
    assert ledger.directory.update_staff(staff.id,replace(staff.config,name='changed'),expected_revision=1).revision == 2


@pytest.mark.parametrize('field,value', [('assignments',1),('owner_assignments',1),('outbox',1),('storage_bytes',1)])
def test_capacity_failures_never_partially_admit_records(ledger,staff,field,value):
    ledger.limits = replace(ledger.limits,**{field:value},owner_assignments=1) if field=='assignments' else replace(ledger.limits,**{field:value})
    if field != 'storage_bytes':
        original = submit(ledger,staff)
        assert submit(ledger,staff) == original
    with pytest.raises(OfficeCapacityError): submit(ledger,staff,'new')
    assert len(ledger.list_assignments()) == (0 if field=='storage_bytes' else 1)
    assert len(ledger.list_outbox()) == (0 if field=='storage_bytes' else 1)


def test_owner_queue_limit_and_terminal_rows_count_towards_storage(ledger,staff):
    ledger.settings = replace(ledger.settings,queue_limit=1)
    first = submit(ledger,staff)
    with pytest.raises(OfficeCapacityError): submit(ledger,staff,'second')
    ledger.cancel_assignment(first['id'])
    assert submit(ledger,staff,'second')['queue_seq'] == 2


def test_operation_caps_and_tool_ceiling_roll_back_intents(ledger,staff):
    assignment = submit(ledger,staff)
    with pytest.raises(PermissionError): ledger.prepare_operation(assignment['id'],'bad','write_file',{})
    ledger.limits = replace(ledger.limits,operations=1)
    first = ledger.prepare_operation(assignment['id'],'first','read_file',{})
    assert ledger.prepare_operation(assignment['id'],'first','read_file',{}) == first
    with pytest.raises(OfficeCapacityError): ledger.prepare_operation(assignment['id'],'second','read_file',{})
    assert len(ledger.list_outbox()) == 2


@pytest.mark.parametrize('role',['viewer','operator'])
def test_only_admin_mutates_intents(ledger,staff,role):
    assignment = submit(ledger,staff)
    with set_current_role(role):
        assert ledger.get_assignment(assignment['id']) == assignment
        with pytest.raises(PermissionError): submit(ledger,staff,'another')
        with pytest.raises(PermissionError): ledger.prepare_operation(assignment['id'],'op','read_file',{})
        with pytest.raises(PermissionError): ledger.cancel_assignment(assignment['id'])


@pytest.mark.parametrize('value',[{'x':float('nan')},{1:'not a string'},{'x':object()},{'x':(1,2)}])
def test_intent_json_rejects_ambiguous_or_nonfinite_values(value):
    with pytest.raises(ValueError): canonical(value)


def test_persistence_limits_validate_types_and_environment():
    with pytest.raises(ValueError): PersistenceLimits(delivery_lease_seconds=True)
    with pytest.raises(ValueError): PersistenceLimits(assignments=1,owner_assignments=2)
    with pytest.raises(ValueError): PersistenceLimits.load({'AGENT_OFFICE_PERSIST_OUTBOX':'-1'})
    assert PersistenceLimits.load({'AGENT_OFFICE_PERSIST_OUTBOX':'20'}).outbox == 20


def test_ddl_failure_rolls_back_v2_to_v3_upgrade(office_env,monkeypatch):
    from agent_execution.office.store import _MIGRATIONS,_OBJECTS,schema_version
    store = office_env['store']
    with closing(store.connect()) as conn:
        for version in (1,2):
            for name in _MIGRATIONS[version]: conn.execute(_OBJECTS[name])
        conn.execute('INSERT INTO office_schema VALUES(1,2)')
    monkeypatch.setitem(_OBJECTS,'office_outbox','CREATE TABLE office_outbox (invalid !)')
    import sqlite3
    with pytest.raises(sqlite3.DatabaseError): store.migrate(office_env['root']/'v3-failed',offline=True)
    with closing(store.connect()) as conn:
        assert schema_version(conn) == 2
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='office_assignments'").fetchone()


def test_v2_upgrade_preserves_directory_and_membership_revisions(office_env):
    from agent_execution.office.store import _MIGRATIONS,_OBJECTS,schema_version
    store=office_env['store']
    with closing(store.connect()) as conn:
        for version in (1,2):
            for name in _MIGRATIONS[version]: conn.execute(_OBJECTS[name])
        conn.execute('INSERT INTO office_schema VALUES(1,2)')
        conn.execute("""INSERT INTO office_staff(owner,id,name,name_key,provider,tools,workspace_project,enabled,revision,updated)
            VALUES('owner','staff','S','s','pythonai','[]','default',1,6,1)""")
        conn.execute("INSERT INTO office_teams(owner,id,name,name_key,enabled,revision,updated) VALUES('owner','team','T','t',1,9,1)")
        conn.execute("INSERT INTO office_members VALUES('owner','team','staff','lead')")
    result=store.migrate(office_env['root']/'v3-backup',offline=True)
    assert result['from_version']==2 and result['to_version']==3
    with closing(store.connect()) as conn:
        assert schema_version(conn)==3
        assert conn.execute('SELECT revision FROM office_staff').fetchone()[0]==6
        assert conn.execute('SELECT revision FROM office_teams').fetchone()[0]==9
        assert conn.execute('SELECT role FROM office_members').fetchone()[0]=='lead'


def test_concurrent_different_requests_cannot_exceed_capacity(ledger,staff):
    ledger.limits=replace(ledger.limits,assignments=1,owner_assignments=1)
    contexts=[copy_context(),copy_context()]
    def attempt(key):
        try: return submit(ledger,staff,key)['id']
        except OfficeCapacityError: return None
    with ThreadPoolExecutor(2) as pool:
        results=[pool.submit(context.run,attempt,key) for context,key in zip(contexts,('one','two'))]
        assert sum(future.result(20) is not None for future in results)==1
    assert len(ledger.list_assignments())==len(ledger.list_outbox())==1
