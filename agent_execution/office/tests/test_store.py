from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

import pytest

from agent_execution.office.store import OfficeSchemaError, OfficeStore, SCHEMA_VERSION, _MIGRATIONS, _OBJECTS, schema_version
from agent_execution.operations.backup import restore, verify
from agent_execution.tasks import TaskStore
from permissions import set_current_role


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_taskstore_and_imports_do_not_install_office_schema(office_env):
    store = office_env["store"]
    before = digest(store.path)
    with pytest.raises(OfficeSchemaError, match="migration"):
        store.ready()
    assert digest(store.path) == before
    with closing(store.connect()) as conn:
        assert schema_version(conn) == 0


def test_new_and_existing_task_db_migration_backup_restore_and_legacy_compatibility(office_env):
    store = office_env["store"]
    with closing(store.connect()) as conn:
        conn.execute("CREATE TABLE legacy_private (value TEXT)")
        conn.execute("INSERT INTO legacy_private VALUES('保留內容')")
        conn.execute("PRAGMA user_version=42")
    result = store.migrate(office_env["root"] / "backup", offline=True)
    assert result["from_version"] == 0 and result["to_version"] == SCHEMA_VERSION
    manifest = verify(result["backup"])
    assert "office_schema" not in manifest["databases"][0]["tables"]
    with closing(store.connect()) as conn:
        assert schema_version(conn) == SCHEMA_VERSION
        assert conn.execute("SELECT value FROM legacy_private").fetchone()[0] == "保留內容"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 42
    restored = restore(result["backup"], office_env["root"] / "restored")
    with closing(sqlite3.connect(restored["databases"][0])) as conn:
        assert conn.execute("SELECT value FROM legacy_private").fetchone()[0] == "保留內容"
        assert schema_version(conn) == 0
    # Legacy startup continues to work without reading or changing office schema.
    TaskStore(store.path)
    store.ready()
    before = digest(store.path)
    again = store.migrate(office_env["root"] / "unused", offline=True)
    assert not again["changed"] and again["backup"] is None
    assert not (office_env["root"] / "unused").exists()
    assert digest(store.path) == before


def test_incremental_v1_upgrade_preserves_staff(office_env):
    store = office_env["store"]
    with closing(store.connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for name in _MIGRATIONS[1]:
            conn.execute(_OBJECTS[name])
        conn.execute("INSERT INTO office_schema VALUES(1,1)")
        conn.execute("""INSERT INTO office_staff
            (owner,id,name,name_key,provider,tools,workspace_project,enabled,updated)
            VALUES('owner','staff','名字','名字','pythonai','[]','default',0,1)""")
        conn.commit()
    result = store.migrate(office_env["root"] / "v1-backup", offline=True)
    assert result["from_version"] == 1
    with closing(store.connect()) as conn:
        assert conn.execute("SELECT name,revision FROM office_staff").fetchone()[:] == ("名字", 1)
    with closing(sqlite3.connect(Path(result["backup"]) / "0.sqlite")) as conn:
        assert schema_version(conn) == 1


@pytest.mark.parametrize("offline", [False, "true", 1])
def test_migration_requires_explicit_offline_declaration(office_env, offline):
    store = office_env["store"]
    before = digest(store.path)
    with pytest.raises(ValueError):
        store.migrate(office_env["root"] / "backup", offline=offline)
    assert digest(store.path) == before


def test_migration_requires_admin_and_backup_outside_workspace(office_env):
    store = office_env["store"]
    with set_current_role("viewer"), pytest.raises(PermissionError):
        store.migrate(office_env["root"] / "backup", offline=True)
    with pytest.raises(ValueError, match="outside"):
        store.migrate(office_env["workspace"] / "backup", offline=True)
    assert not (office_env["workspace"] / "backup").exists()


def test_backup_cannot_leak_database_to_another_project_workspace(office_env):
    destination = office_env["root"] / "other" / "backup"
    with pytest.raises(ValueError, match="outside"):
        office_env["store"].migrate(destination, offline=True)
    assert not destination.exists()


def test_backup_failure_leaves_database_unchanged(office_env, monkeypatch):
    store = office_env["store"]
    before = digest(store.path)
    def fail(*args):
        raise OSError("injected backup failure")
    monkeypatch.setattr("agent_execution.operations.backup.backup", fail)
    with pytest.raises(OSError):
        store.migrate(office_env["root"] / "backup", offline=True)
    assert digest(store.path) == before


@pytest.mark.parametrize("initial_version", [0, 1])
def test_failed_ddl_rolls_back_all_office_objects_and_preserves_backup(office_env, monkeypatch, initial_version):
    store = office_env["store"]
    if initial_version:
        with closing(store.connect()) as conn:
            for name in _MIGRATIONS[1]:
                conn.execute(_OBJECTS[name])
            conn.execute("INSERT INTO office_schema VALUES(1,1)")
    monkeypatch.setitem(_OBJECTS, "office_members", "CREATE TABLE office_members (invalid syntax !)")
    with pytest.raises(sqlite3.DatabaseError):
        store.migrate(office_env["root"] / "failed-backup", offline=True)
    with closing(store.connect()) as conn:
        assert schema_version(conn) == initial_version
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert verify(office_env["root"] / "failed-backup")["format"] == 1


def test_migration_prevents_writes_between_backup_and_schema_commit(office_env, monkeypatch):
    store = office_env["store"]
    from agent_execution.operations.backup import backup as original
    observed = []
    def guarded_backup(paths, destination):
        with closing(sqlite3.connect(store.path, timeout=.01)) as rival:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                rival.execute("CREATE TABLE rival_write (value TEXT)")
            observed.append("write fenced")
        return original(paths, destination)
    monkeypatch.setattr("agent_execution.operations.backup.backup", guarded_backup)
    store.migrate(office_env["root"] / "backup", offline=True)
    assert observed == ["write fenced"]


@pytest.mark.parametrize("mutation", [
    "UPDATE office_schema SET version=999",
    "DROP INDEX office_one_lead",
    "CREATE TABLE office_surprise (payload TEXT)",
])
def test_unknown_or_partial_schema_is_rejected_without_mutation(office_env, mutation):
    store = office_env["store"]
    store.migrate(office_env["root"] / "backup", offline=True)
    with closing(store.connect()) as conn:
        conn.execute(mutation)
    before = digest(store.path)
    with pytest.raises(OfficeSchemaError):
        store.ready()
    with pytest.raises(OfficeSchemaError):
        store.migrate(office_env["root"] / "must-not-exist", offline=True)
    assert digest(store.path) == before
    assert not (office_env["root"] / "must-not-exist").exists()


def test_unversioned_partial_schema_is_not_silently_adopted(office_env):
    store = office_env["store"]
    with closing(store.connect()) as conn:
        conn.execute("CREATE TABLE office_staff (secret TEXT)")
    before = digest(store.path)
    with pytest.raises(OfficeSchemaError):
        store.migrate(office_env["root"] / "backup", offline=True)
    assert digest(store.path) == before


def test_foreign_keys_prevent_cross_owner_relationships_and_second_lead(directory, office_env):
    store = office_env["store"]
    with closing(store.connect()) as conn:
        conn.execute("""INSERT INTO office_staff(owner,id,name,name_key,provider,tools,workspace_project,enabled,updated)
            VALUES('alice','s','S','s','pythonai','[]','default',0,1)""")
        conn.execute("INSERT INTO office_teams(owner,id,name,name_key,enabled,updated) VALUES('bob','t','T','t',0,1)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO office_members VALUES('bob','t','s','member')")
        conn.execute("INSERT INTO office_teams(owner,id,name,name_key,enabled,updated) VALUES('alice','t','T','t',0,1)")
        conn.execute("INSERT INTO office_members VALUES('alice','t','s','lead')")
        conn.execute("""INSERT INTO office_staff(owner,id,name,name_key,provider,tools,workspace_project,enabled,updated)
            VALUES('alice','s2','S2','s2','pythonai','[]','default',0,1)""")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO office_members VALUES('alice','t','s2','lead')")
