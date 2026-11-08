"""Owner-scoped staff/team CRUD; trusted transports supply execution_scope."""

from contextlib import contextmanager
import json
import time
import sqlite3
import uuid

from permissions import current_role, check_tool_permission
from agent_execution.policy import current_context
from agent_execution.sessions import project_workspace
from agent_execution.workspace_lifecycle.store import LifecycleStore
from .contracts import OwnerScope, Provider, _identifier
from .models import Membership, StaffConfig, StaffRecord, TeamConfig, TeamRecord, TeamRole, name_key
from .store import OfficeSchemaError, OfficeStore, guard_directory_change


MAX_STAFF_PER_OWNER = 200
MAX_TEAMS_PER_OWNER = 100
MAX_MEMBERS_PER_TEAM = 64


class OfficeNotFound(LookupError):
    """Resource does not exist in this owner scope, or has been deleted."""


class OfficeConflict(ValueError):
    """Stale revision, duplicate name, capacity or relationship conflict."""


def _revision(value):
    if type(value) is not int or not 1 <= value < 9223372036854775807:
        raise ValueError("expected_revision must be a positive SQLite integer")


def _page(limit, after_id):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer in 1..100")
    if after_id is not None:
        _identifier(after_id, "after_id")


class OfficeDirectory:
    """Application-level management, not a model tool or a public API route.

    The constructor is explicit application setup: it requires migrated office
    storage and initializes existing lifecycle support in that same TaskStore DB.
    Calls derive owner from the current authenticated scope, never from payloads.
    """

    def __init__(self, store: OfficeStore):
        store.ready()
        self.store = store
        self.lifecycle = LifecycleStore(store.path)

    @contextmanager
    def _connection(self, *, write=False):
        role = current_role()
        if role not in {"admin", "operator", "viewer"} or (write and role != "admin"):
            raise PermissionError("Office changes require admin")
        owner = OwnerScope.from_current()
        # Store a logical project reference, never accept a client filesystem path.
        workspace = project_workspace(owner.project)
        if not workspace.is_dir():
            raise PermissionError("Authorized project workspace is unavailable")
        try:
            with self.store.transaction(owner, write=write) as conn:
                if write:
                    self.lifecycle.ensure_session(conn)
                yield conn, owner
        except sqlite3.IntegrityError as error:
            raise OfficeConflict("Office name or relationship conflict") from error

    @staticmethod
    def _row(conn, table, owner, key, expected_revision=None):
        _identifier(key, "resource ID")
        # table is an internal constant, never caller-controlled SQL.
        row = conn.execute(f"SELECT * FROM {table} WHERE owner=? AND id=? AND deleted=0", (owner.key, key)).fetchone()
        if row is None:
            raise OfficeNotFound("Office resource not found")
        if expected_revision is not None:
            _revision(expected_revision)
            if row["revision"] != expected_revision:
                raise OfficeConflict("Office revision changed; reload before retrying")
        return row

    @staticmethod
    def _staff(row):
        try:
            tools = json.loads(row["tools"])
            if not isinstance(tools, list) or len(set(tools)) != len(tools):
                raise ValueError("Invalid tool list")
            config = StaffConfig(row["name"], Provider(row["provider"]), row["model"], frozenset(tools), bool(row["enabled"]))
            return StaffRecord(row["id"], config, row["workspace_project"], row["revision"])
        except (TypeError, ValueError) as error:
            raise OfficeSchemaError("Invalid stored staff configuration") from error

    @staticmethod
    def _team(conn, owner, row):
        members = tuple(Membership(item["staff_id"], TeamRole(item["role"])) for item in conn.execute(
            "SELECT staff_id,role FROM office_members WHERE owner=? AND team_id=? ORDER BY staff_id", (owner.key, row["id"]),
        ))
        return TeamRecord(row["id"], TeamConfig(row["name"], bool(row["enabled"])), row["revision"], members)

    @staticmethod
    def _tools(config):
        if not isinstance(config, StaffConfig):
            raise ValueError("Expected a StaffConfig")
        from base import TOOLS
        ceiling = current_context().allowed_tools
        if any(tool not in TOOLS or not check_tool_permission(tool) or
               (ceiling is not None and tool not in ceiling) for tool in config.allowed_tools):
            raise PermissionError("Staff tools exceed the registered, authorized tool set")

    @staticmethod
    def _capacity(conn, table, owner, maximum):
        # Tombstones count too: repeated delete/create cannot grow storage forever.
        if conn.execute(f"SELECT COUNT(*) FROM {table} WHERE owner=?", (owner.key,)).fetchone()[0] >= maximum:
            raise OfficeConflict("Office directory capacity reached")

    def create_staff(self, config: StaffConfig) -> StaffRecord:
        with self._connection(write=True) as (conn, owner):
            self._tools(config)
            self._capacity(conn, "office_staff", owner, MAX_STAFF_PER_OWNER)
            key = uuid.uuid4().hex
            conn.execute("""INSERT INTO office_staff
                (owner,id,name,name_key,provider,model,tools,workspace_project,enabled,updated)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (
                owner.key, key, config.name, name_key(config.name), config.provider.value, config.model,
                json.dumps(sorted(config.allowed_tools)), owner.project, int(config.enabled), time.time(),
            ))
            return self._staff(self._row(conn, "office_staff", owner, key))

    def get_staff(self, staff_id: str) -> StaffRecord:
        with self._connection() as (conn, owner):
            return self._staff(self._row(conn, "office_staff", owner, staff_id))

    def list_staff(self, *, limit: int = 50, after_id: str | None = None) -> tuple[StaffRecord, ...]:
        _page(limit, after_id)
        with self._connection() as (conn, owner):
            return tuple(self._staff(row) for row in conn.execute(
                "SELECT * FROM office_staff WHERE owner=? AND deleted=0 AND id>? ORDER BY id LIMIT ?",
                (owner.key, after_id or "", limit),
            ))

    def update_staff(self, staff_id: str, config: StaffConfig, *, expected_revision: int) -> StaffRecord:
        _revision(expected_revision)
        with self._connection(write=True) as (conn, owner):
            self._tools(config)
            self._row(conn, "office_staff", owner, staff_id, expected_revision)
            guard_directory_change(conn, owner, staff_id=staff_id)
            if not config.enabled and conn.execute("""SELECT 1 FROM office_members m JOIN office_teams t
                ON t.owner=m.owner AND t.id=m.team_id WHERE m.owner=? AND m.staff_id=?
                AND m.role='lead' AND t.enabled=1 AND t.deleted=0 LIMIT 1""", (owner.key, staff_id)).fetchone():
                raise OfficeConflict("Transfer leadership or disable the team before disabling its lead")
            conn.execute("""UPDATE office_staff SET name=?,name_key=?,provider=?,model=?,tools=?,enabled=?,
                revision=revision+1,updated=? WHERE owner=? AND id=? AND revision=?""", (
                config.name, name_key(config.name), config.provider.value, config.model,
                json.dumps(sorted(config.allowed_tools)), int(config.enabled), time.time(), owner.key, staff_id, expected_revision,
            ))
            return self._staff(self._row(conn, "office_staff", owner, staff_id))

    def delete_staff(self, staff_id: str, *, expected_revision: int) -> None:
        _revision(expected_revision)
        with self._connection(write=True) as (conn, owner):
            row = self._row(conn, "office_staff", owner, staff_id, expected_revision)
            guard_directory_change(conn, owner, staff_id=staff_id)
            if row["enabled"] or conn.execute("SELECT 1 FROM office_members WHERE owner=? AND staff_id=? LIMIT 1", (owner.key, staff_id)).fetchone():
                raise OfficeConflict("Disable staff and remove all memberships before deletion")
            conn.execute("UPDATE office_staff SET deleted=1,revision=revision+1,updated=? WHERE owner=? AND id=?", (time.time(), owner.key, staff_id))

    def create_team(self, config: TeamConfig) -> TeamRecord:
        if not isinstance(config, TeamConfig):
            raise ValueError("Expected a TeamConfig")
        with self._connection(write=True) as (conn, owner):
            if config.enabled:
                raise OfficeConflict("Create a disabled team, assign an enabled lead, then enable it")
            self._capacity(conn, "office_teams", owner, MAX_TEAMS_PER_OWNER)
            key = uuid.uuid4().hex
            conn.execute("INSERT INTO office_teams(owner,id,name,name_key,enabled,updated) VALUES(?,?,?,?,0,?)",
                         (owner.key, key, config.name, name_key(config.name), time.time()))
            return self._team(conn, owner, self._row(conn, "office_teams", owner, key))

    def get_team(self, team_id: str) -> TeamRecord:
        with self._connection() as (conn, owner):
            return self._team(conn, owner, self._row(conn, "office_teams", owner, team_id))

    def list_teams(self, *, limit: int = 50, after_id: str | None = None) -> tuple[TeamRecord, ...]:
        _page(limit, after_id)
        with self._connection() as (conn, owner):
            rows = conn.execute("SELECT * FROM office_teams WHERE owner=? AND deleted=0 AND id>? ORDER BY id LIMIT ?",
                                (owner.key, after_id or "", limit)).fetchall()
            return tuple(self._team(conn, owner, row) for row in rows)

    def update_team(self, team_id: str, config: TeamConfig, *, expected_revision: int) -> TeamRecord:
        if not isinstance(config, TeamConfig):
            raise ValueError("Expected a TeamConfig")
        _revision(expected_revision)
        with self._connection(write=True) as (conn, owner):
            self._row(conn, "office_teams", owner, team_id, expected_revision)
            guard_directory_change(conn, owner, team_id=team_id)
            if config.enabled and not conn.execute("""SELECT 1 FROM office_members m JOIN office_staff s
                ON s.owner=m.owner AND s.id=m.staff_id WHERE m.owner=? AND m.team_id=?
                AND m.role='lead' AND s.enabled=1 AND s.deleted=0""", (owner.key, team_id)).fetchone():
                raise OfficeConflict("An enabled team requires an enabled lead")
            conn.execute("UPDATE office_teams SET name=?,name_key=?,enabled=?,revision=revision+1,updated=? WHERE owner=? AND id=?",
                         (config.name, name_key(config.name), int(config.enabled), time.time(), owner.key, team_id))
            return self._team(conn, owner, self._row(conn, "office_teams", owner, team_id))

    def delete_team(self, team_id: str, *, expected_revision: int) -> None:
        _revision(expected_revision)
        with self._connection(write=True) as (conn, owner):
            row = self._row(conn, "office_teams", owner, team_id, expected_revision)
            guard_directory_change(conn, owner, team_id=team_id)
            if row["enabled"] or conn.execute("SELECT 1 FROM office_members WHERE owner=? AND team_id=? LIMIT 1", (owner.key, team_id)).fetchone():
                raise OfficeConflict("Disable the team and remove all members before deletion")
            conn.execute("UPDATE office_teams SET deleted=1,revision=revision+1,updated=? WHERE owner=? AND id=?", (time.time(), owner.key, team_id))

    def set_member(self, team_id: str, staff_id: str, role: TeamRole, *, expected_revision: int) -> TeamRecord:
        _revision(expected_revision)
        if not isinstance(role, TeamRole):
            raise ValueError("role must be a TeamRole")
        with self._connection(write=True) as (conn, owner):
            team = self._row(conn, "office_teams", owner, team_id, expected_revision)
            guard_directory_change(conn, owner, team_id=team_id)
            staff = self._row(conn, "office_staff", owner, staff_id)
            current = conn.execute("SELECT role FROM office_members WHERE owner=? AND team_id=? AND staff_id=?", (owner.key, team_id, staff_id)).fetchone()
            if current is None and conn.execute("SELECT COUNT(*) FROM office_members WHERE owner=? AND team_id=?", (owner.key, team_id)).fetchone()[0] >= MAX_MEMBERS_PER_TEAM:
                raise OfficeConflict("Team member capacity reached")
            if role == TeamRole.LEAD:
                if team["enabled"] and not staff["enabled"]:
                    raise OfficeConflict("An enabled team requires an enabled lead")
                conn.execute("UPDATE office_members SET role='member' WHERE owner=? AND team_id=? AND role='lead'", (owner.key, team_id))
            elif current is not None and current["role"] == "lead" and team["enabled"]:
                raise OfficeConflict("Transfer leadership or disable the team before demoting its lead")
            conn.execute("""INSERT INTO office_members VALUES(?,?,?,?)
                ON CONFLICT(owner,team_id,staff_id) DO UPDATE SET role=excluded.role""", (owner.key, team_id, staff_id, role.value))
            conn.execute("UPDATE office_teams SET revision=revision+1,updated=? WHERE owner=? AND id=?", (time.time(), owner.key, team_id))
            return self._team(conn, owner, self._row(conn, "office_teams", owner, team_id))

    def get_member(self, team_id: str, staff_id: str) -> Membership:
        with self._connection() as (conn, owner):
            self._row(conn, "office_teams", owner, team_id)
            _identifier(staff_id, "staff_id")
            row = conn.execute("SELECT role FROM office_members WHERE owner=? AND team_id=? AND staff_id=?", (owner.key, team_id, staff_id)).fetchone()
            if row is None:
                raise OfficeNotFound("Office membership not found")
            return Membership(staff_id, TeamRole(row["role"]))

    def list_members(self, team_id: str) -> tuple[Membership, ...]:
        return self.get_team(team_id).members

    def remove_member(self, team_id: str, staff_id: str, *, expected_revision: int) -> TeamRecord:
        _revision(expected_revision)
        with self._connection(write=True) as (conn, owner):
            team = self._row(conn, "office_teams", owner, team_id, expected_revision)
            guard_directory_change(conn, owner, team_id=team_id)
            _identifier(staff_id, "staff_id")
            member = conn.execute("SELECT role FROM office_members WHERE owner=? AND team_id=? AND staff_id=?", (owner.key, team_id, staff_id)).fetchone()
            if member is None:
                raise OfficeNotFound("Office membership not found")
            if member["role"] == "lead" and team["enabled"]:
                raise OfficeConflict("Transfer leadership or disable the team before removing its lead")
            conn.execute("DELETE FROM office_members WHERE owner=? AND team_id=? AND staff_id=?", (owner.key, team_id, staff_id))
            conn.execute("UPDATE office_teams SET revision=revision+1,updated=? WHERE owner=? AND id=?", (time.time(), owner.key, team_id))
            return self._team(conn, owner, self._row(conn, "office_teams", owner, team_id))
