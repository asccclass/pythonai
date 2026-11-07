from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from agent_execution.office import OfficeSettings


def test_disabled_by_default_without_creating_files_or_reading_provider_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert OfficeSettings.load({}) == OfficeSettings()
    assert not OfficeSettings.load({}).enabled
    assert list(tmp_path.iterdir()) == []


def test_settings_are_explicit_uncached_and_do_not_change_environment(monkeypatch):
    monkeypatch.setenv("AGENT_OFFICE_ENABLED", "1")
    first = OfficeSettings.load()
    assert first.enabled
    monkeypatch.setenv("AGENT_OFFICE_ENABLED", "0")
    assert not OfficeSettings.load().enabled
    assert first.enabled
    with pytest.raises(FrozenInstanceError):
        first.enabled = False


@pytest.mark.parametrize("enabled", ["true", "yes", "", " 1", "2", 1, [], None])
def test_enabled_flag_requires_exact_zero_or_one(enabled):
    with pytest.raises(ValueError):
        OfficeSettings.load({"AGENT_OFFICE_ENABLED": enabled})


@pytest.mark.parametrize("name", ["OWNER_CONCURRENCY", "GLOBAL_CONCURRENCY", "QUEUE_LIMIT", "APPROVAL_TTL_SECONDS"])
@pytest.mark.parametrize("raw", ["0", "-1", "1.5", "nan", " 2", "２", "", "99999999999999999999999"])
def test_limits_reject_invalid_numbers(name, raw):
    with pytest.raises(ValueError):
        OfficeSettings.load({"AGENT_OFFICE_" + name: raw})


def test_owner_limit_must_fit_global_limit():
    with pytest.raises(ValueError):
        OfficeSettings(owner_concurrency=5, global_concurrency=4)
    with pytest.raises(ValueError):
        OfficeSettings(enabled="false")
    with pytest.raises(ValueError):
        OfficeSettings(owner_concurrency=True)


def test_example_config_loads_and_leaves_feature_disabled():
    path = Path(__file__).resolve().parents[1] / "examples" / "office.env.example"
    values = dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#"))
    settings = OfficeSettings.load(values)
    assert settings == OfficeSettings()
    assert OfficeSettings.load({**values, "AGENT_OFFICE_QUEUE_LIMIT": "250"}).queue_limit == 250
