from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from agent_execution import execution_scope
from agent_execution.policy import current_context
from agent_execution.tasks import task_owner
from agent_execution.office import OwnerScope, Provider, ResumeMode, RuntimeCapabilities
from agent_execution.office.contracts import RuntimeStartRequest, ToolDecision, TurnReceipt, UnsupportedCapability
from agent_execution.office.runtimes import RuntimeEvent, RuntimeEventType


def test_office_owner_matches_taskstore_and_restores_context():
    original = current_context()
    with execution_scope(source="websocket", identity="使用者:a", project="計畫", session='對話:"a"'):
        owner = OwnerScope.from_current()
        assert owner.key == task_owner(current_context())
        with execution_scope(session="other"):
            assert OwnerScope.from_current().key != owner.key
        assert OwnerScope.from_current() == owner
    assert current_context() is original


@pytest.mark.parametrize("field", ["source", "identity", "project", "session"])
def test_every_owner_component_is_an_isolation_boundary(field):
    owner = OwnerScope("websocket", "alice", "project", "conversation")
    assert replace(owner, **{field: "different"}).key != owner.key
    assert json.loads(owner.key) == ["websocket", "alice", "project", "conversation"]


def test_owner_does_not_collapse_delimiters_or_cross_transport_identities():
    assert OwnerScope("a", "b:c", "p", "s").key != OwnerScope("a:b", "c", "p", "s").key
    assert OwnerScope("api", "api:alice", "p", "s").key != OwnerScope("websocket", "alice", "p", "s").key
    owner = OwnerScope("local", "local", "default", "local")
    with pytest.raises(FrozenInstanceError):
        owner.identity = "admin"


@pytest.mark.parametrize("value", ["", "  ", "alice\x00admin", None, 1])
def test_invalid_owner_is_not_accepted(value):
    with pytest.raises(ValueError):
        OwnerScope("websocket", value, "project", "conversation")


def test_default_capabilities_fail_closed_and_do_not_claim_resume():
    capabilities = RuntimeCapabilities()
    capabilities.require()
    with pytest.raises(UnsupportedCapability):
        capabilities.require(writes=True)
    with pytest.raises(UnsupportedCapability):
        capabilities.require(resume=ResumeMode.SESSION)
    with pytest.raises(UnsupportedCapability):
        capabilities.require(resume=ResumeMode.DURABLE)


def test_approval_callback_alone_cannot_admit_managed_writes():
    with pytest.raises(UnsupportedCapability):
        RuntimeCapabilities(tool_approval=True).require(writes=True)
    with pytest.raises(ValueError):
        RuntimeCapabilities(all_tools_gated=True)
    RuntimeCapabilities(tool_approval=True, all_tools_gated=True).require(writes=True)


def test_session_resume_does_not_imply_durable_recovery():
    session = RuntimeCapabilities(resume=ResumeMode.SESSION)
    session.require(resume=ResumeMode.SESSION)
    with pytest.raises(UnsupportedCapability):
        session.require(resume=ResumeMode.DURABLE)
    durable = RuntimeCapabilities(resume=ResumeMode.DURABLE)
    durable.require(resume=ResumeMode.SESSION)
    durable.require(resume=ResumeMode.DURABLE)


@pytest.mark.parametrize("values", [
    {"tool_approval": "false"}, {"usage": 1}, {"mcp": None},
    {"attachments": "yes"}, {"resume": "durable"},
])
def test_capabilities_reject_truthy_or_untyped_claims(values):
    with pytest.raises(ValueError):
        RuntimeCapabilities(**values)


@pytest.mark.parametrize("values", [{"writes": "yes"}, {"resume": "session"}])
def test_admission_requirements_are_typed(values):
    with pytest.raises(ValueError):
        RuntimeCapabilities().require(**values)


def start_request(tmp_path):
    return RuntimeStartRequest(
        OwnerScope("local", "local", "default", "local"), "staff", "task", 1, tmp_path,
        allowed_tools=frozenset({"read_file"}),
    )


def test_start_request_is_immutable_and_does_not_authorize_path(tmp_path):
    request = start_request(tmp_path)
    assert request.provider == Provider.PYTHONAI
    assert request.model is None
    with pytest.raises(FrozenInstanceError):
        request.generation = 2
    # Shape validation deliberately performs no mkdir/resolve/authorization.
    absent = tmp_path / "not-created"
    assert replace(request, workspace=absent).workspace == absent
    assert not absent.exists()


@pytest.mark.parametrize("values", [
    {"staff_id": ""}, {"task_id": "任" * 86}, {"generation": True}, {"generation": 0},
    {"owner": "local"}, {"provider": "pythonai"}, {"model": " "},
    {"workspace": Path("relative")}, {"workspace": "/untyped"},
    {"allowed_tools": {"write_file"}}, {"allowed_tools": frozenset({""})},
])
def test_invalid_start_request_rejected(tmp_path, values):
    with pytest.raises(ValueError):
        replace(start_request(tmp_path), **values)


def event(**values):
    return RuntimeEvent("event-1", RuntimeEventType.TEXT_DELTA, "session-1", 1, "turn-1", **values)


def test_event_is_a_json_snapshot_and_requires_no_storage():
    value = event(payload_json=' { "text": "你好", "nested": {"tokens": 3} } ')
    returned = value.payload
    returned["nested"]["tokens"] = 99
    assert value.payload["nested"]["tokens"] == 3
    assert value.payload_json == '{"nested":{"tokens":3},"text":"你好"}'
    with pytest.raises(FrozenInstanceError):
        value.payload_json = "{}"


@pytest.mark.parametrize("payload", ["[]", "null", "invalid", '{"cost":NaN}', '{"cost":Infinity}', '{"cost":1e999}'])
def test_event_rejects_invalid_unbounded_or_nonfinite_payload(payload):
    with pytest.raises(ValueError):
        event(payload_json=payload)


def test_event_rejects_oversized_payload():
    with pytest.raises(ValueError):
        event(payload_json='{"x":"' + "x" * (1024 * 1024) + '"}')


@pytest.mark.parametrize("payload", [
    '{"allow":false,"allow":true}',
    '{"tool":{"path":"first","path":"second"}}',
])
def test_event_rejects_ambiguous_duplicate_keys(payload):
    with pytest.raises(ValueError):
        event(payload_json=payload)


def test_turn_events_require_exact_turn_and_generation():
    for kind in RuntimeEventType:
        if kind in {RuntimeEventType.SESSION_STARTED, RuntimeEventType.SESSION_CLOSED}:
            assert RuntimeEvent("event", kind, "session", 1).turn_id is None
        else:
            with pytest.raises(ValueError, match="turn_id"):
                RuntimeEvent("event", kind, "session", 1)
    with pytest.raises(ValueError):
        replace(event(), generation=True)
    with pytest.raises(ValueError):
        replace(event(), type="turn.completed")
    with pytest.raises(ValueError):
        replace(event(), session_id="")


def test_receipt_is_only_an_acceptance_reference():
    assert TurnReceipt("turn").turn_id == "turn"
    with pytest.raises(ValueError):
        TurnReceipt("")


def test_tool_decision_has_exact_execution_binding_and_no_argument_override():
    decision = ToolDecision("session", 1, "turn", "tool-call", False, "Human denied")
    assert not decision.allow
    assert replace(decision, generation=2) != decision
    assert replace(decision, turn_id="next-turn") != decision
    with pytest.raises(FrozenInstanceError):
        decision.allow = True
    with pytest.raises(TypeError):
        ToolDecision("session", 1, "turn", "tool-call", True, arguments={"path": "other"})


@pytest.mark.parametrize("values", [
    {"allow": "yes"}, {"allow": False}, {"generation": True},
    {"turn_id": ""}, {"tool_call_id": ""}, {"reason": None},
])
def test_tool_decision_rejects_ambiguous_or_unbound_reply(values):
    decision = ToolDecision("session", 1, "turn", "tool-call", True)
    with pytest.raises(ValueError):
        replace(decision, **values)


def test_import_does_not_create_storage_start_workers_or_load_providers(tmp_path):
    root = Path(__file__).resolve().parents[3]
    script = '''
import os, socket, sqlite3, subprocess, sys, threading
from pathlib import Path
from unittest.mock import patch
def forbidden(*args, **kwargs):
    raise AssertionError("office import attempted I/O")
before = dict(os.environ)
with patch.object(sqlite3, "connect", forbidden), patch.object(subprocess, "Popen", forbidden), patch.object(Path, "mkdir", forbidden), patch.object(Path, "write_text", forbidden), patch.object(threading.Thread, "start", forbidden), patch.object(socket, "create_connection", forbidden):
    import agent_execution.office
    import agent_execution.office.runtimes
assert dict(os.environ) == before
assert "server" not in sys.modules
assert "communication_server" not in sys.modules
assert "agent_runtime" not in sys.modules
assert "temporalio" not in sys.modules
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=root, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_example_contracts_are_consumable():
    path = Path(__file__).resolve().parents[1] / "examples" / "runtime.json"
    example = json.loads(path.read_text(encoding="utf-8"))
    capabilities = RuntimeCapabilities(**{**example["capabilities"], "resume": ResumeMode(example["capabilities"]["resume"])})
    assert capabilities.resume == ResumeMode.NONE
    with pytest.raises(UnsupportedCapability):
        capabilities.require(writes=True)
    value = example["event"]
    decoded = RuntimeEvent(
        value["event_id"], RuntimeEventType(value["type"]), value["session_id"],
        value["generation"], value["turn_id"], json.dumps(value["payload"]),
    )
    assert decoded.payload["text"] == "範例串流"
