"""Explicit, backed-up office migrations in an existing TaskStore database."""

from contextlib import closing, contextmanager
from pathlib import Path
import json
import os
import re
import sqlite3

from agent_execution.tasks import TaskStore, assert_owner_active
from .contracts import OwnerScope


SCHEMA_VERSION = 3


class OfficeSchemaError(RuntimeError):
    pass


# Use individual execute calls: executescript would commit an open transaction.
_OBJECTS = {
    "office_schema": """CREATE TABLE office_schema (
        id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL CHECK(version>=1))""",
    "office_staff": """CREATE TABLE office_staff (
        owner TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL, name_key TEXT NOT NULL,
        provider TEXT NOT NULL CHECK(provider IN ('pythonai','codex','claude')), model TEXT,
        tools TEXT NOT NULL, workspace_project TEXT NOT NULL,
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
        deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1)),
        revision INTEGER NOT NULL DEFAULT 1 CHECK(typeof(revision)='integer' AND revision>=1),
        updated REAL NOT NULL, PRIMARY KEY(owner,id), CHECK(deleted=0 OR enabled=0))""",
    "office_teams": """CREATE TABLE office_teams (
        owner TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL, name_key TEXT NOT NULL,
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
        deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1)),
        revision INTEGER NOT NULL DEFAULT 1 CHECK(typeof(revision)='integer' AND revision>=1),
        updated REAL NOT NULL, PRIMARY KEY(owner,id), CHECK(deleted=0 OR enabled=0))""",
    "office_staff_names": "CREATE UNIQUE INDEX office_staff_names ON office_staff(owner,name_key) WHERE deleted=0",
    "office_team_names": "CREATE UNIQUE INDEX office_team_names ON office_teams(owner,name_key) WHERE deleted=0",
    "office_members": """CREATE TABLE office_members (
        owner TEXT NOT NULL, team_id TEXT NOT NULL, staff_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('member','lead')), PRIMARY KEY(owner,team_id,staff_id),
        FOREIGN KEY(owner,team_id) REFERENCES office_teams(owner,id) ON DELETE RESTRICT,
        FOREIGN KEY(owner,staff_id) REFERENCES office_staff(owner,id) ON DELETE RESTRICT)""",
    "office_one_lead": "CREATE UNIQUE INDEX office_one_lead ON office_members(owner,team_id) WHERE role='lead'",
    "office_staff_memberships": "CREATE INDEX office_staff_memberships ON office_members(owner,staff_id)",
    "office_assignments": """CREATE TABLE office_assignments (
        owner TEXT NOT NULL,id TEXT NOT NULL,request_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
        task_id TEXT NOT NULL,staff_id TEXT NOT NULL,team_id TEXT,staff_revision INTEGER NOT NULL,
        team_revision INTEGER,queue_seq INTEGER NOT NULL,payload TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('queued','active','completed','cancelled','uncertain')),
        created REAL NOT NULL,PRIMARY KEY(owner,id),UNIQUE(owner,request_id),
        UNIQUE(owner,task_id),UNIQUE(owner,queue_seq),
        FOREIGN KEY(owner,staff_id) REFERENCES office_staff(owner,id) ON DELETE RESTRICT,
        FOREIGN KEY(owner,team_id) REFERENCES office_teams(owner,id) ON DELETE RESTRICT)""",
    "office_assignment_queue": "CREATE INDEX office_assignment_queue ON office_assignments(owner,state,queue_seq)",
    "office_assignment_staff": "CREATE INDEX office_assignment_staff ON office_assignments(owner,staff_id,state)",
    "office_assignment_team": "CREATE INDEX office_assignment_team ON office_assignments(owner,team_id,state)",
    "office_operations": """CREATE TABLE office_operations (
        owner TEXT NOT NULL,id TEXT NOT NULL,assignment_id TEXT NOT NULL,request_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL
        CHECK(state IN ('prepared','waiting_approval','ready','denied','expired','cancelled','uncertain')),
        approval_state TEXT NOT NULL DEFAULT 'not_delivered',created REAL NOT NULL,
        PRIMARY KEY(owner,id),UNIQUE(owner,assignment_id,request_id),
        FOREIGN KEY(owner,assignment_id) REFERENCES office_assignments(owner,id) ON DELETE RESTRICT)""",
    "office_outbox": """CREATE TABLE office_outbox (
        owner TEXT NOT NULL,id TEXT NOT NULL,assignment_id TEXT NOT NULL,operation_id TEXT,
        kind TEXT NOT NULL CHECK(kind IN ('event','approval')),fingerprint TEXT NOT NULL,
        payload TEXT NOT NULL,state TEXT NOT NULL
        CHECK(state IN ('pending','sending','delivered','uncertain','cancel_pending','cancelled')),
        attempts INTEGER NOT NULL DEFAULT 0,token TEXT,lease_until REAL,
        error TEXT NOT NULL DEFAULT '',created REAL NOT NULL,
        PRIMARY KEY(owner,id),UNIQUE(owner,kind,operation_id),
        FOREIGN KEY(owner,assignment_id) REFERENCES office_assignments(owner,id) ON DELETE RESTRICT,
        FOREIGN KEY(owner,operation_id) REFERENCES office_operations(owner,id) ON DELETE RESTRICT)""",
    "office_outbox_pending": "CREATE INDEX office_outbox_pending ON office_outbox(owner,state,created,id)",
    "office_outbox_assignment": "CREATE INDEX office_outbox_assignment ON office_outbox(owner,assignment_id)",
    "office_operations_assignment": "CREATE INDEX office_operations_assignment ON office_operations(owner,assignment_id)",
    "office_targets": """CREATE TABLE office_targets (
        kind TEXT PRIMARY KEY CHECK(kind IN ('event','approval')),path TEXT NOT NULL,instance TEXT NOT NULL)""",
}
_MIGRATIONS = {
    1: ("office_schema", "office_staff", "office_teams", "office_staff_names", "office_team_names"),
    2: ("office_members", "office_one_lead", "office_staff_memberships"),
    3: ("office_assignments", "office_assignment_queue", "office_assignment_staff", "office_assignment_team",
        "office_operations", "office_outbox", "office_outbox_pending", "office_outbox_assignment", "office_operations_assignment", "office_targets"),
}


def _normalized(sql):
    return re.sub(r"\s+", " ", sql.strip()).rstrip(";")


def schema_version(conn) -> int:
    """Reject partial, future or altered office schemas before any mutation."""
    rows = conn.execute("SELECT name,sql FROM sqlite_master WHERE name GLOB 'office_*'").fetchall()
    objects = {row[0]: row[1] for row in rows}
    if not objects:
        return 0
    try:
        if _normalized(objects.get("office_schema") or "") != _normalized(_OBJECTS["office_schema"]):
            raise OfficeSchemaError("Missing or incompatible office schema metadata")
        versions = conn.execute("SELECT id,version FROM office_schema").fetchall()
        if len(versions) != 1 or versions[0][0] != 1 or type(versions[0][1]) is not int:
            raise OfficeSchemaError("Invalid office schema version")
        version = versions[0][1]
        if version not in _MIGRATIONS:
            raise OfficeSchemaError("Unsupported office schema version")
        expected = {name for step in range(1, version + 1) for name in _MIGRATIONS[step]}
        if objects.keys() != expected or any(
            _normalized(objects[name] or "") != _normalized(_OBJECTS[name]) for name in expected
        ):
            raise OfficeSchemaError("Office schema does not match its declared version")
        return version
    except sqlite3.DatabaseError as error:
        raise OfficeSchemaError("Cannot inspect office schema") from error


def erase_owner(conn, owner: str) -> None:
    """Called by session erasure in the same TaskStore transaction."""
    version = schema_version(conn)
    if not version:
        return
    if version >= 3:
        if office_busy(conn, [owner]):
            raise OfficeSchemaError("Resolve office assignments and deliveries before erasure")
        conn.execute("DELETE FROM office_outbox WHERE owner=?", (owner,))
        conn.execute("DELETE FROM office_operations WHERE owner=?", (owner,))
        conn.execute("DELETE FROM office_assignments WHERE owner=?", (owner,))
    if version >= 2:
        conn.execute("DELETE FROM office_members WHERE owner=?", (owner,))
    conn.execute("DELETE FROM office_teams WHERE owner=?", (owner,))
    conn.execute("DELETE FROM office_staff WHERE owner=?", (owner,))


def office_busy(conn, owners):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='office_assignments'").fetchone():
        return []
    if schema_version(conn) != SCHEMA_VERSION:
        raise OfficeSchemaError("Office migration required before inspecting work")
    result = []
    for owner in owners:
        if conn.execute("SELECT 1 FROM office_assignments WHERE owner=? AND state NOT IN ('cancelled','completed') LIMIT 1", (owner,)).fetchone():
            result.append('office:assignment')
        if conn.execute("SELECT 1 FROM office_outbox WHERE owner=? AND state NOT IN ('delivered','cancelled') LIMIT 1", (owner,)).fetchone():
            result.append('office:delivery')
    return result


def guard_directory_change(conn, owner, *, staff_id=None, team_id=None):
    field, key = ('staff_id', staff_id) if staff_id is not None else ('team_id', team_id)
    if conn.execute(f"""SELECT 1 FROM office_assignments a WHERE a.owner=? AND a.{field}=? AND (
        a.state NOT IN ('cancelled','completed') OR EXISTS (
          SELECT 1 FROM office_outbox b WHERE b.owner=a.owner AND b.assignment_id=a.id
          AND b.state NOT IN ('delivered','cancelled')) OR EXISTS (
          SELECT 1 FROM office_operations o WHERE o.owner=a.owner AND o.assignment_id=a.id AND o.state='uncertain')) LIMIT 1""", (owner.key, key)).fetchone():
        from .directory import OfficeConflict
        raise OfficeConflict("Cancel or resolve assignments before changing their directory binding")


def guard_reserved_task(conn, owner, task_id):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='office_assignments'").fetchone():
        if conn.execute("SELECT 1 FROM office_assignments WHERE owner=? AND task_id=?", (owner, task_id)).fetchone():
            from agent_execution.tasks import RecoveryRequired
            raise RecoveryRequired("Office task IDs are reserved for the office dispatcher; R2 execution is not enabled")


class OfficeStore:
    """Trusted storage handle; constructors/imports do not migrate office tables.

    Pass the application's existing TaskStore. Only migrate() installs schema;
    no actual user database is migrated by importing or starting legacy flows.
    """

    def __init__(self, tasks: TaskStore):
        if not isinstance(tasks, TaskStore):
            raise TypeError("OfficeStore requires an existing TaskStore")
        self.path = Path(tasks.path).absolute()
        if self.path.is_symlink() or not self.path.is_file():
            raise OfficeSchemaError("Task database must be an existing regular file")

    def connect(self):
        conn = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def ready(self) -> None:
        with closing(self.connect()) as conn:
            if schema_version(conn) != SCHEMA_VERSION:
                raise OfficeSchemaError("Office migration required")

    def migrate(self, backup_directory: Path, *, offline: bool = False) -> dict:
        from permissions import current_role
        if current_role() != "admin":
            raise PermissionError("Office migrations require admin")
        if offline is not True:
            raise ValueError("Stop workers and declare offline=True before office migration")
        from base import workspace_root
        from agent_execution.sessions import project_workspace
        destination = Path(backup_directory).absolute()
        mappings = json.loads(os.environ.get("AGENT_PROJECT_WORKSPACES", "{}"))
        if not isinstance(mappings, dict):
            raise ValueError("AGENT_PROJECT_WORKSPACES must be an object")
        workspaces = [workspace_root(), *(project_workspace(project) for project in mappings)]
        if any(destination.resolve().is_relative_to(root.resolve()) for root in workspaces):
            raise ValueError("Migration backups must be outside the agent workspace")
        from agent_execution.operations.backup import backup
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                original = schema_version(conn)
                if original == SCHEMA_VERSION:
                    conn.rollback()
                    return {"from_version": original, "to_version": original, "changed": False, "backup": None}
                # A reserved write lock prevents mutation between snapshot and DDL.
                backup([self.path], destination)
                for version in range(original + 1, SCHEMA_VERSION + 1):
                    for name in _MIGRATIONS[version]:
                        conn.execute(_OBJECTS[name])
                    if version == 1:
                        conn.execute("INSERT INTO office_schema VALUES(1,1)")
                    else:
                        conn.execute("UPDATE office_schema SET version=? WHERE id=1", (version,))
                if schema_version(conn) != SCHEMA_VERSION:
                    raise OfficeSchemaError("Office migration validation failed")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return {"from_version": original, "to_version": SCHEMA_VERSION, "changed": True, "backup": str(destination)}

    @contextmanager
    def transaction(self, owner: OwnerScope, *, write: bool = False):
        if not isinstance(owner, OwnerScope):
            raise TypeError("Expected an OwnerScope")
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                if schema_version(conn) != SCHEMA_VERSION:
                    raise OfficeSchemaError("Office migration required")
                assert_owner_active(conn, owner.key)
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
