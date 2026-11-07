import json

import pytest

from agent_execution.policy import execution_scope
from agent_execution.tasks import TaskStore
from agent_execution.office.store import OfficeStore
from agent_execution.office.directory import OfficeDirectory
from permissions import set_current_role


@pytest.fixture
def office_env(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    for key, value in {
        "AGENT_TASK_DB": tmp_path / "tasks.db", "AGENT_APPROVAL_DB": tmp_path / "approvals.db",
        "AGENT_TENANT_ROOT": tmp_path / "tenants", "SCHEDULER_DB_PATH": tmp_path / "scheduler.db",
        "AGENT_TRACE_DB": tmp_path / "traces.db", "COMPANION_EVENT_DB": tmp_path / "events.db",
        "AGENT_EXTENSION_DB": tmp_path / "extensions.db", "AGENT_MCP_OAUTH_DB": tmp_path / "oauth.db",
        "COMMUNICATION_DB_PATH": tmp_path / "communication.db", "MEMORY_DB_PATH": tmp_path / "memory.db",
        "AGENT_CODE_INDEX_DIR": tmp_path / "index", "AGENT_WORKSPACE_ROOT": workspace,
    }.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setenv("AGENT_PROJECT_WORKSPACES", json.dumps({"default": str(workspace), "other": str(other)}))
    monkeypatch.setenv("AGENT_WORKSPACE_CLEANUP", "1")
    with execution_scope(source="api", identity="api:alice", project="default", session="directory", workspace=workspace), set_current_role("admin"):
        tasks = TaskStore()
        yield {"root": tmp_path, "tasks": tasks, "store": OfficeStore(tasks), "workspace": workspace}


@pytest.fixture
def directory(office_env):
    store = office_env["store"]
    store.migrate(office_env["root"] / "initial-backup", offline=True)
    return OfficeDirectory(store)
