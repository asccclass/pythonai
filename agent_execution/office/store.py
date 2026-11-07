"""Explicit, backed-up office migrations in an existing TaskStore database."""

from contextlib import closing, contextmanager
from pathlib import Path
import json
import os
import re
import sqlite3

from agent_execution.tasks import TaskStore, assert_owner_active
from .contracts import OwnerScope


SCHEMA_VERSION = 2


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
}
_MIGRATIONS = {
    1: ("office_schema", "office_staff", "office_teams", "office_staff_names", "office_team_names"),
    2: ("office_members", "office_one_lead", "office_staff_memberships"),
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
    if version >= 2:
        conn.execute("DELETE FROM office_members WHERE owner=?", (owner,))
    conn.execute("DELETE FROM office_teams WHERE owner=?", (owner,))
    conn.execute("DELETE FROM office_staff WHERE owner=?", (owner,))


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
