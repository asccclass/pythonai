from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from contextvars import copy_context
from dataclasses import replace
import json

import pytest

from agent_execution.office.contracts import OwnerScope, Provider
from agent_execution.office.directory import OfficeConflict, OfficeDirectory, OfficeNotFound
from agent_execution.office.models import StaffConfig, TeamConfig, TeamRole
from agent_execution.policy import execution_scope
from permissions import current_role, set_current_role


def test_staff_crud_persists_and_projects_no_internal_paths_or_credentials(directory, office_env):
    record = directory.create_staff(StaffConfig("  整理員  ", Provider.CODEX, "gpt-model", frozenset({"read_file"})))
    assert record.config.name == "整理員" and record.revision == 1
    assert record.workspace_project == "default" and not record.config.enabled
    assert directory.list_staff() == (record,)
    reopened = OfficeDirectory(office_env["store"])
    assert reopened.get_staff(record.id) == record
    updated = reopened.update_staff(record.id, replace(record.config, name="寫作員", enabled=True), expected_revision=1)
    assert updated.revision == 2 and updated.config.enabled
    with pytest.raises(OfficeConflict):
        directory.update_staff(record.id, record.config, expected_revision=1)
    with pytest.raises(OfficeConflict):
        directory.delete_staff(record.id, expected_revision=2)
    updated = directory.update_staff(record.id, replace(updated.config, enabled=False), expected_revision=2)
    directory.delete_staff(record.id, expected_revision=updated.revision)
    assert not directory.list_staff()
    with pytest.raises(OfficeNotFound):
        directory.get_staff(record.id)
    with closing(office_env["store"].connect()) as conn:
        assert conn.execute("SELECT deleted,revision FROM office_staff WHERE id=?", (record.id,)).fetchone()[:] == (1, 4)
    public = json.dumps(updated.to_dict(), ensure_ascii=False)
    assert str(office_env["root"]) not in public
    assert "owner" not in updated.to_dict()
    assert set(updated.to_dict()) == {"id", "name", "provider", "model", "allowed_tools", "enabled", "workspace_project", "revision"}


def test_team_membership_crud_lead_transfer_and_deletion_restrictions(directory):
    one = directory.create_staff(StaffConfig("一", enabled=True))
    two = directory.create_staff(StaffConfig("二", enabled=True))
    team = directory.create_team(TeamConfig("開發"))
    with pytest.raises(OfficeConflict):
        directory.update_team(team.id, replace(team.config, enabled=True), expected_revision=1)
    team = directory.set_member(team.id, one.id, TeamRole.LEAD, expected_revision=1)
    team = directory.set_member(team.id, two.id, TeamRole.MEMBER, expected_revision=team.revision)
    assert directory.get_member(team.id, one.id).role == TeamRole.LEAD
    assert len(directory.list_members(team.id)) == 2
    team = directory.update_team(team.id, replace(team.config, enabled=True), expected_revision=team.revision)
    with pytest.raises(OfficeConflict):
        directory.remove_member(team.id, one.id, expected_revision=team.revision)
    with pytest.raises(OfficeConflict):
        directory.set_member(team.id, one.id, TeamRole.MEMBER, expected_revision=team.revision)
    with pytest.raises(OfficeConflict):
        directory.update_staff(one.id, replace(one.config, enabled=False), expected_revision=1)
    assert directory.get_team(team.id) == team
    team = directory.set_member(team.id, two.id, TeamRole.LEAD, expected_revision=team.revision)
    assert directory.get_member(team.id, one.id).role == TeamRole.MEMBER
    assert sum(member.role == TeamRole.LEAD for member in team.members) == 1
    with pytest.raises(OfficeConflict):
        directory.delete_team(team.id, expected_revision=team.revision)
    team = directory.update_team(team.id, replace(team.config, enabled=False), expected_revision=team.revision)
    with pytest.raises(OfficeConflict):
        directory.delete_team(team.id, expected_revision=team.revision)
    team = directory.remove_member(team.id, one.id, expected_revision=team.revision)
    team = directory.remove_member(team.id, two.id, expected_revision=team.revision)
    directory.delete_team(team.id, expected_revision=team.revision)
    assert not directory.list_teams()
    with pytest.raises(OfficeNotFound):
        directory.get_member(team.id, one.id)


@pytest.mark.parametrize("scope", [
    {"identity": "api:bob"}, {"project": "other"},
    {"session": "another"}, {"source": "websocket"},
])
def test_every_owner_dimension_isolates_reads_and_mutations(directory, scope):
    staff = directory.create_staff(StaffConfig("私有"))
    team = directory.create_team(TeamConfig("私有團隊"))
    with execution_scope(**scope):
        assert directory.list_staff() == directory.list_teams() == ()
        operations = [
            lambda: directory.get_staff(staff.id), lambda: directory.get_team(team.id),
            lambda: directory.update_staff(staff.id, staff.config, expected_revision=1),
            lambda: directory.delete_staff(staff.id, expected_revision=1),
            lambda: directory.update_team(team.id, team.config, expected_revision=1),
            lambda: directory.delete_team(team.id, expected_revision=1),
            lambda: directory.set_member(team.id, staff.id, TeamRole.LEAD, expected_revision=1),
            lambda: directory.remove_member(team.id, staff.id, expected_revision=1),
            lambda: directory.get_member(team.id, staff.id),
        ]
        for operation in operations:
            with pytest.raises(OfficeNotFound):
                operation()
    assert directory.get_staff(staff.id) == staff and directory.get_team(team.id) == team


def test_cross_owner_membership_cannot_attach_foreign_staff(directory):
    foreign = directory.create_staff(StaffConfig("他人員工"))
    with execution_scope(identity="api:bob"):
        team = directory.create_team(TeamConfig("Bob"))
        with pytest.raises(OfficeNotFound):
            directory.set_member(team.id, foreign.id, TeamRole.MEMBER, expected_revision=1)
        assert directory.get_team(team.id).members == ()


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_non_admin_can_read_own_directory_but_cannot_change_it(directory, role):
    staff = directory.create_staff(StaffConfig("一般員工"))
    team = directory.create_team(TeamConfig("一般團隊"))
    with set_current_role(role):
        assert directory.get_staff(staff.id) == staff
        assert directory.list_teams() == (team,)
        operations = [
            lambda: directory.create_staff(StaffConfig("新員工")),
            lambda: directory.create_team(TeamConfig("新團隊")),
            lambda: directory.update_staff(staff.id, staff.config, expected_revision=1),
            lambda: directory.delete_staff(staff.id, expected_revision=1),
            lambda: directory.update_team(team.id, team.config, expected_revision=1),
            lambda: directory.delete_team(team.id, expected_revision=1),
            lambda: directory.set_member(team.id, staff.id, TeamRole.LEAD, expected_revision=1),
            lambda: directory.remove_member(team.id, staff.id, expected_revision=1),
        ]
        for operation in operations:
            with pytest.raises(PermissionError):
                operation()


def test_team_lead_name_and_membership_do_not_grant_admin(directory):
    staff = directory.create_staff(StaffConfig("admin", enabled=True))
    team = directory.create_team(TeamConfig("system_admin"))
    directory.set_member(team.id, staff.id, TeamRole.LEAD, expected_revision=1)
    with set_current_role("viewer"):
        assert directory.get_member(team.id, staff.id).role == TeamRole.LEAD
        assert current_role() == "viewer"
        with pytest.raises(PermissionError):
            directory.create_staff(StaffConfig("越權"))


def test_workspace_mapping_and_registered_tool_ceiling_are_rechecked(directory, office_env, monkeypatch):
    with pytest.raises(PermissionError):
        directory.create_staff(StaffConfig("未知工具", allowed_tools=frozenset({"not-registered"})))
    with execution_scope(allowed_tools=frozenset({"read_file"})), pytest.raises(PermissionError):
        directory.create_staff(StaffConfig("擴權", allowed_tools=frozenset({"write_file"})))
    record = directory.create_staff(StaffConfig("讀取", allowed_tools=frozenset({"read_file"})))
    monkeypatch.setenv("AGENT_PROJECT_WORKSPACES", '{"unrelated":"C:/missing"}')
    with pytest.raises(PermissionError):
        directory.get_staff(record.id)
    with pytest.raises(PermissionError):
        directory.update_staff(record.id, record.config, expected_revision=1)


def test_normalized_names_unique_per_owner_and_deleted_name_can_be_reused(directory):
    one = directory.create_staff(StaffConfig("Ａlice"))
    with pytest.raises(OfficeConflict):
        directory.create_staff(StaffConfig("alice"))
    with execution_scope(identity="api:bob"):
        assert directory.create_staff(StaffConfig("alice")).revision == 1
    directory.delete_staff(one.id, expected_revision=1)
    two = directory.create_staff(StaffConfig("Alice"))
    assert two.id != one.id
    with pytest.raises(OfficeNotFound):
        directory.update_staff(one.id, one.config, expected_revision=1)


def test_disabled_staff_referenced_by_team_cannot_be_deleted(directory):
    staff = directory.create_staff(StaffConfig("停用成員"))
    team = directory.create_team(TeamConfig("保留成員參照"))
    team = directory.set_member(team.id, staff.id, TeamRole.MEMBER, expected_revision=1)
    with pytest.raises(OfficeConflict):
        directory.delete_staff(staff.id, expected_revision=1)
    assert directory.get_staff(staff.id) == staff
    directory.remove_member(team.id, staff.id, expected_revision=team.revision)
    directory.delete_staff(staff.id, expected_revision=1)
    with pytest.raises(OfficeNotFound):
        directory.get_staff(staff.id)


def test_sql_like_names_are_literal_and_resource_ids_do_not_bypass_scope(directory):
    staff = directory.create_staff(StaffConfig("'; DROP TABLE office_staff; --"))
    assert directory.get_staff(staff.id) == staff
    with pytest.raises(OfficeNotFound):
        directory.get_staff("' OR 1=1 --")
    assert len(directory.list_staff()) == 1


def test_team_names_pagination_and_tombstone_capacity(directory, monkeypatch):
    monkeypatch.setattr("agent_execution.office.directory.MAX_TEAMS_PER_OWNER", 2)
    first = directory.create_team(TeamConfig("Team"))
    with pytest.raises(OfficeConflict):
        directory.create_team(TeamConfig("ＴＥＡＭ"))
    second = directory.create_team(TeamConfig("Another"))
    page = directory.list_teams(limit=1)
    following = directory.list_teams(limit=1, after_id=page[0].id)
    assert {row.id for row in (*page, *following)} == {first.id, second.id}
    directory.delete_team(first.id, expected_revision=1)
    with pytest.raises(OfficeConflict):
        directory.create_team(TeamConfig("No capacity"))


def test_member_changes_invalidate_stale_team_revision(directory):
    staff = directory.create_staff(StaffConfig("成員"))
    team = directory.create_team(TeamConfig("團隊"))
    changed = directory.set_member(team.id, staff.id, TeamRole.MEMBER, expected_revision=1)
    for operation in [
        lambda: directory.set_member(team.id, staff.id, TeamRole.LEAD, expected_revision=1),
        lambda: directory.remove_member(team.id, staff.id, expected_revision=1),
        lambda: directory.update_team(team.id, TeamConfig("覆蓋"), expected_revision=1),
    ]:
        with pytest.raises(OfficeConflict):
            operation()
    assert directory.get_team(team.id) == changed


def test_concurrent_updates_only_one_cas_succeeds(directory):
    record = directory.create_staff(StaffConfig("並行"))
    contexts = [copy_context(), copy_context()]
    def change(name):
        try:
            return directory.update_staff(record.id, replace(record.config, name=name), expected_revision=1).revision
        except OfficeConflict:
            return "conflict"
    with ThreadPoolExecutor(2) as pool:
        results = [pool.submit(context.run, change, name) for context, name in zip(contexts, ("一", "二"))]
        assert sorted([future.result(15) for future in results], key=str) == [2, "conflict"]
    assert directory.get_staff(record.id).revision == 2


def test_failed_lead_transfer_rolls_back_demotion_and_team_revision(directory):
    first = directory.create_staff(StaffConfig("主管一", enabled=True))
    second = directory.create_staff(StaffConfig("主管二", enabled=True))
    team = directory.create_team(TeamConfig("原子移轉"))
    team = directory.set_member(team.id, first.id, TeamRole.LEAD, expected_revision=1)
    with closing(directory.store.connect()) as conn:
        conn.execute("""CREATE TRIGGER injected_failure BEFORE INSERT ON office_members
            WHEN NEW.role='lead' BEGIN SELECT RAISE(ABORT,'injected failure'); END""")
    with pytest.raises(OfficeConflict):
        directory.set_member(team.id, second.id, TeamRole.LEAD, expected_revision=team.revision)
    assert directory.get_team(team.id) == team
    assert directory.get_member(team.id, first.id).role == TeamRole.LEAD


def test_disabled_member_cannot_replace_enabled_team_lead(directory):
    lead = directory.create_staff(StaffConfig("主管", enabled=True))
    disabled = directory.create_staff(StaffConfig("停用"))
    team = directory.create_team(TeamConfig("服務中團隊"))
    team = directory.set_member(team.id, lead.id, TeamRole.LEAD, expected_revision=1)
    team = directory.update_team(team.id, replace(team.config, enabled=True), expected_revision=team.revision)
    with pytest.raises(OfficeConflict):
        directory.set_member(team.id, disabled.id, TeamRole.LEAD, expected_revision=team.revision)
    assert directory.get_team(team.id) == team


def test_pagination_capacity_and_member_capacity(directory, monkeypatch):
    monkeypatch.setattr("agent_execution.office.directory.MAX_STAFF_PER_OWNER", 3)
    staff = [directory.create_staff(StaffConfig(str(index))) for index in range(3)]
    first = directory.list_staff(limit=2)
    rest = directory.list_staff(limit=2, after_id=first[-1].id)
    assert {row.id for row in (*first, *rest)} == {row.id for row in staff}
    directory.delete_staff(staff[0].id, expected_revision=1)
    with pytest.raises(OfficeConflict):
        directory.create_staff(StaffConfig("超過 tombstone 上限"))
    team = directory.create_team(TeamConfig("容量"))
    monkeypatch.setattr("agent_execution.office.directory.MAX_MEMBERS_PER_TEAM", 1)
    team = directory.set_member(team.id, staff[1].id, TeamRole.MEMBER, expected_revision=1)
    with pytest.raises(OfficeConflict):
        directory.set_member(team.id, staff[2].id, TeamRole.MEMBER, expected_revision=team.revision)
    # Changing an existing member at capacity is allowed.
    assert directory.set_member(team.id, staff[1].id, TeamRole.LEAD, expected_revision=team.revision).revision == 3


@pytest.mark.parametrize("revision", [None, True, 0, -1, "1", 2**63])
def test_invalid_revision_is_never_an_unconditional_update(directory, revision):
    staff = directory.create_staff(StaffConfig("版本"))
    with pytest.raises(ValueError):
        directory.update_staff(staff.id, StaffConfig("不應改寫"), expected_revision=revision)
    assert directory.get_staff(staff.id) == staff


@pytest.mark.parametrize("limit", [0, 101, True, "2"])
def test_invalid_page_size_rejected(directory, limit):
    with pytest.raises(ValueError):
        directory.list_staff(limit=limit)
    with pytest.raises(ValueError):
        directory.list_teams(limit=limit)
