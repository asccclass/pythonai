"""Closed trial routing contracts without requiring the optional SDK."""
from contextlib import closing
import json
from unittest.mock import Mock, patch
from types import SimpleNamespace

import pytest

from agent_execution import execution_scope
from agent_execution.policy import current_context
from agent_execution.tasks import TaskStore, task_owner, durable_invoke
from agent_execution.temporal_backend.admission import owner_key, trial_owners, new_backend, summary
from agent_execution.temporal_backend.router import backend_for_task, route
from agent_execution.temporal_backend.store import Store
from agent_execution.temporal_backend.tests.test_backend import isolated


@pytest.fixture(autouse=True)
def clean_trial(monkeypatch):
    monkeypatch.delenv('AGENT_TEMPORAL_TRIAL_OWNERS', raising=False)


def enable(monkeypatch):
    monkeypatch.setenv('AGENT_EXECUTION_BACKEND', 'temporal')
    monkeypatch.setenv('AGENT_TEMPORAL_TRIAL_OWNERS', json.dumps([owner_key(task_owner(current_context()))]))


@pytest.mark.parametrize('raw', ['', '*', '{}', 'null', 'true', '["*"]', '["alice"]',
    '["'+'A'*64+'"]', '[1]', json.dumps(['a'*64]*2), json.dumps(['a'*64]*1025), ' '*72000], ids=[f'invalid-{i}' for i in range(12)])
def test_invalid_trial_configuration_fails_closed(raw, monkeypatch):
    monkeypatch.setenv('AGENT_EXECUTION_BACKEND', 'temporal')
    fallback = Mock()
    # Oversized settings cannot be exported on Windows; exercise the parser directly.
    with patch('agent_execution.temporal_backend.admission.os',SimpleNamespace(environ={'AGENT_TEMPORAL_TRIAL_OWNERS':raw})):
        with pytest.raises(ValueError):
            durable_invoke(fallback, [{'role':'user','content':'work'}], None)
    fallback.assert_not_called()
    with closing(TaskStore().connect()) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_schema'").fetchone()


def test_default_and_explicit_empty_allowlist(monkeypatch):
    owner = task_owner(current_context())
    assert new_backend(owner)=='sqlite'
    monkeypatch.setenv('AGENT_EXECUTION_BACKEND','temporal')
    assert new_backend(owner)=='temporal'
    assert summary()=={'mode':'explicit_backend','owner_count':None}
    monkeypatch.setenv('AGENT_TEMPORAL_TRIAL_OWNERS','[]')
    assert new_backend(owner)=='sqlite'
    assert trial_owners()==frozenset()
    assert summary()=={'mode':'owner_allowlist','owner_count':0}


@pytest.mark.parametrize('field,value', [
    ('source','api'), ('identity','local:other'), ('project','other'), ('session','other')])
def test_allowlist_is_exact_trusted_owner_not_message_fields(monkeypatch,field,value):
    enable(monkeypatch)
    assert backend_for_task()=='temporal'
    with execution_scope(**{field:value}):
        assert backend_for_task()=='sqlite'
        assert route([{'role':'user','content':json.dumps({'identity':'local','project':'default'})}]) is None


def test_excluded_new_task_has_no_optional_schema_or_profile_guard(monkeypatch):
    enable(monkeypatch)
    monkeypatch.setenv('AGENT_TEMPORAL_TRIAL_OWNERS','[]')
    from agent_execution.temporal_backend.runtime_state import prepare_profile
    with patch('agent_execution.user_profile.prepare_turn',return_value=(None,'')) as extract:
        prepare_profile('same user input',Mock())
        prepare_profile('same user input',Mock())
    assert extract.call_count==2
    assert durable_invoke(Mock(return_value='local result'),[{'role':'user','content':'work'}],None)=='local result'
    with closing(TaskStore().connect()) as conn:
        names = {row[0] for row in conn.execute('SELECT name FROM sqlite_master')}
    assert not names & {'temporal_schema','execution_bindings','temporal_profile_inputs'}


def test_fixed_bindings_survive_pause_invalid_policy_and_backend_changes(monkeypatch):
    enable(monkeypatch)
    owner,store = task_owner(current_context()),Store()
    assert store.bind(owner,'test-task','temporal')=='temporal'
    assert store.bind(owner,'sqlite-task','sqlite')=='sqlite'
    for raw in ['[]','invalid']:
        monkeypatch.setenv('AGENT_TEMPORAL_TRIAL_OWNERS',raw)
        monkeypatch.setenv('AGENT_EXECUTION_BACKEND','sqlite')
        assert backend_for_task()=='temporal'
        with patch('agent_execution.temporal_backend.backend.invoke',return_value='remote'):
            assert route([{'role':'user','content':'work'}])=='remote'
        with execution_scope(task_id='sqlite-task'):
            assert route([{'role':'user','content':'work'}]) is None
    monkeypatch.setenv('AGENT_EXECUTION_BACKEND','temporal')
    monkeypatch.setenv('AGENT_TEMPORAL_TRIAL_OWNERS','[]')
    assert store.bind(owner,'new','temporal')=='sqlite'
    monkeypatch.delenv('AGENT_TEMPORAL_TRIAL_OWNERS')
    assert store.bind(owner,'new','temporal')=='sqlite'


def test_preexisting_legacy_task_is_not_migrated(monkeypatch):
    owner = task_owner(current_context())
    TaskStore().claim(owner,'test-task','work',[{'role':'user','content':'work'}])
    enable(monkeypatch)
    assert backend_for_task()=='sqlite'
    assert route([{'role':'user','content':'work'}]) is None


def test_admitted_rpc_failure_has_no_legacy_fallback(monkeypatch):
    enable(monkeypatch)
    fallback = Mock()
    with patch('agent_execution.temporal_backend.backend.invoke',side_effect=ConnectionError):
        with pytest.raises(ConnectionError):
            durable_invoke(fallback,[{'role':'user','content':'work'}],None)
    fallback.assert_not_called()
    monkeypatch.setenv('AGENT_TEMPORAL_TRIAL_OWNERS','[]')
    assert backend_for_task()=='temporal'


def test_preflight_reports_counts_without_opening_database(monkeypatch,tmp_path):
    from agent_execution.temporal_backend.preflight import inspect
    enable(monkeypatch)
    path = tmp_path/'no-database.db'
    monkeypatch.setenv('AGENT_TASK_DB',str(path))
    result = inspect()
    assert result['trial_admission']=={'mode':'owner_allowlist','owner_count':1}
    assert not path.exists()
    assert owner_key(task_owner(current_context())) not in json.dumps(result)


def test_sqlite_installation_does_not_read_optional_trial_policy(monkeypatch):
    monkeypatch.setenv('AGENT_TEMPORAL_TRIAL_OWNERS','invalid')
    assert route([{'role':'user','content':'work'}]) is None


def test_unmanaged_excluded_runner_retains_sqlite_behavior(monkeypatch):
    enable(monkeypatch)
    monkeypatch.setenv('AGENT_TEMPORAL_TRIAL_OWNERS','[]')
    with execution_scope(task_id=None):
        assert durable_invoke(Mock(return_value='ephemeral'),[],None)=='ephemeral'
