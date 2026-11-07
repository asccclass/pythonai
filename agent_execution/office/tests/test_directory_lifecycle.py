from contextlib import closing

import pytest

from agent_execution.office.contracts import OwnerScope
from agent_execution.office.models import StaffConfig, TeamConfig, TeamRole
from agent_execution.office.store import OfficeSchemaError, erase_owner
from agent_execution.policy import execution_scope
from agent_execution.workspace_lifecycle.store import session_key
from agent_execution.workspace_lifecycle.erasure import finalize


def test_pending_or_deleted_conversation_cannot_read_or_mutate_directory(directory):
    record = directory.create_staff(StaffConfig("刪除中的員工"))
    with closing(directory.store.connect()) as conn:
        conn.execute("UPDATE wl_sessions SET state='deleting' WHERE id=?", (session_key(),))
    for operation in [
        lambda: directory.get_staff(record.id), lambda: directory.list_staff(),
        lambda: directory.list_teams(), lambda: directory.create_staff(StaffConfig("晚到寫入")),
        lambda: directory.update_staff(record.id, StaffConfig("改寫"), expected_revision=1),
        lambda: directory.delete_staff(record.id, expected_revision=1),
        lambda: directory.create_team(TeamConfig("晚到團隊")),
    ]:
        with pytest.raises(PermissionError):
            operation()


def test_actual_session_delete_erases_own_metadata_preserves_other_owner_and_fences_late_write(directory):
    staff = directory.create_staff(StaffConfig("Alice 的私人角色"))
    team = directory.create_team(TeamConfig("Alice 的團隊"))
    directory.set_member(team.id, staff.id, TeamRole.LEAD, expected_revision=1)
    alice = OwnerScope.from_current()
    with execution_scope(identity="api:bob"):
        bob = directory.create_staff(StaffConfig("Bob 保留"))
    plan = directory.lifecycle.preview("delete_session")
    result = directory.lifecycle.confirm(plan["plan_id"])
    assert result["state"] == "completed"
    # Match the authenticated HTTP/command handler's outbox consumption phase.
    assert finalize(directory.lifecycle, plan["plan_id"])["state"] == "deleted"
    with closing(directory.store.connect()) as conn:
        for table in ("office_members", "office_staff", "office_teams"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE owner=?", (alice.key,)).fetchone()[0] == 0
    with pytest.raises(PermissionError):
        directory.create_staff(StaffConfig("不得復活"))
    with execution_scope(identity="api:bob"):
        assert directory.get_staff(bob.id) == bob


def test_unknown_office_version_blocks_scrub_instead_of_silently_leaving_private_data(directory):
    directory.create_staff(StaffConfig("不可遺漏的資料"))
    owner = OwnerScope.from_current()
    with closing(directory.store.connect()) as conn:
        conn.execute("UPDATE office_schema SET version=999")
        with pytest.raises(OfficeSchemaError):
            erase_owner(conn, owner.key)
        assert conn.execute("SELECT COUNT(*) FROM office_staff WHERE owner=?", (owner.key,)).fetchone()[0] == 1
