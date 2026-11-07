import pytest

from agent_execution.office.contracts import Provider
from agent_execution.office.models import StaffConfig, TeamConfig


@pytest.mark.parametrize("values", [
    {"name": ""}, {"name": "\n"}, {"name": "A\nB"}, {"name": "A\x00B"},
    {"name": "字" * 171}, {"provider": "codex"}, {"provider": None},
    {"model": ""}, {"model": "key with spaces"}, {"model": "x\ncommand"},
    {"model": 1}, {"model": "x" * 129}, {"enabled": "false"},
    {"allowed_tools": {"read_file"}}, {"allowed_tools": frozenset({""})},
    {"allowed_tools": frozenset(str(i) for i in range(65))},
])
def test_staff_config_rejects_invalid_values(values):
    with pytest.raises(ValueError):
        StaffConfig(**{"name": "員工", **values})


@pytest.mark.parametrize("name", ["", "\r\n", "A\tB", "字" * 171])
def test_team_config_rejects_invalid_names(name):
    with pytest.raises(ValueError):
        TeamConfig(name)


def test_configs_do_not_accept_credentials_or_arbitrary_workspace_paths():
    with pytest.raises(TypeError):
        StaffConfig("員工", api_key="must-not-persist")
    with pytest.raises(TypeError):
        StaffConfig("員工", workspace="C:/outside")
    assert StaffConfig("員工", Provider.PYTHONAI, "org/model:latest").model == "org/model:latest"
