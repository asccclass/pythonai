"""TP12 root-budget, retry ownership, deadline and cancellation contracts."""
import asyncio
from contextlib import closing
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from agent_execution import execution_scope
from agent_execution.budgets import BudgetExhausted, TaskBudget
from agent_execution.policy import current_context
from agent_execution.tasks import task_owner
from agent_execution.temporal_backend.client import dispatch
from agent_execution.temporal_backend.config import Settings
from agent_execution.temporal_backend.context import execution_context
from agent_execution.temporal_backend.contracts import (READ_TOOLS, STEP_IO_MAX_ATTEMPTS,
    STEP_METADATA_MAX_ATTEMPTS, STEP_RECOVERY_LIMIT, validate_budget_contract)
from agent_execution.temporal_backend.store import Store, canonical
from agent_execution.turn_control.limits import execution_limits


@pytest.fixture
def submission(tmp_path, monkeypatch):
    for key, value in {'AGENT_TASK_DB': str(tmp_path/'tasks.db'),
        'AGENT_TENANT_ROOT': str(tmp_path/'tenants'), 'AGENT_WORKSPACE_ROOT': str(tmp_path),
        'AGENT_TRACE_ENABLED': '0', 'AGENT_TASK_WORKTREE': '0',
        'AGENT_TEMPORAL_ENGINE': 'steps', 'AGENT_EXECUTION_BACKEND': 'temporal',
        'AGENT_TASK_MAX_REQUESTS': '5', 'AGENT_TASK_DEADLINE_SECONDS': '60'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('AGENT_PROJECT_WORKSPACES', raising=False)
    with execution_scope(source='local', identity='local', project='default', session='tp12',
                         task_id='tp12-task', workspace=tmp_path):
        store = Store()
        context = current_context()
        owner = task_owner(context)
        store.bind(owner, context.task_id, 'temporal')
        scope = {name: getattr(context, name) for name in
                 ('source', 'identity', 'session', 'conversation', 'project')}
        scope.update(workspace=str(tmp_path), allowed_tools=sorted(READ_TOOLS))
        row = store.submit(owner, context.task_id, [{'role':'user','content':'private'}],
                           scope, 'admin', Settings.load())
        yield store, row


def test_root_budget_contract_is_pinned_across_environment_changes(submission, monkeypatch):
    store, row = submission
    contract = store.verify_budget_contract(row)
    assert validate_budget_contract(contract)['limits']['requests'] == 5
    monkeypatch.setenv('AGENT_TASK_MAX_REQUESTS', '500')
    assert TaskBudget(row['owner'], row['task_id']).max_requests == 5
    assert store.verify_budget_contract(row) == contract
    assert contract['root_ref'] not in row['workflow_id']


def test_tampered_budget_limits_are_rejected_before_activity_scope(submission):
    store, row = submission
    with closing(store.tasks.connect()) as conn, conn:
        saved = conn.execute('SELECT limits FROM task_budgets WHERE owner=? AND id=?',
                             (row['owner'], row['task_id'])).fetchone()[0]
        limits = json.loads(saved)
        limits['requests'] += 1
        conn.execute('UPDATE task_budgets SET limits=? WHERE owner=? AND id=?',
                     (canonical(limits), row['owner'], row['task_id']))
    with pytest.raises(PermissionError, match='budget contract'):
        with execution_context(store, row):
            raise AssertionError('tampered budget entered Activity scope')


def test_queued_deadline_expires_without_starting_workflow(submission, monkeypatch):
    store, row = submission
    contract = store.verify_budget_contract(row)
    monkeypatch.setattr('agent_execution.temporal_backend.client.time.time',
                        lambda: contract['deadline'] + 1)
    client = NS(start_workflow=AsyncMock())
    with pytest.raises(BudgetExhausted, match='before Workflow dispatch'):
        asyncio.run(dispatch(store, row, client))
    client.start_workflow.assert_not_awaited()
    assert store.get(row['id'])['state'] == 'failed'
    assert store.tasks.get(row['owner'], row['task_id'])['state'] == 'failed'


def test_retry_owners_and_recovered_limits_are_bounded():
    assert (STEP_IO_MAX_ATTEMPTS, STEP_METADATA_MAX_ATTEMPTS, STEP_RECOVERY_LIMIT) == (1, 3, 3)
    saved = {'iteration_limit': 4, 'provider_retry_limit': 2, 'eval_retry_limit': 1}
    assert execution_limits(saved, 200, 256, 256) == (4, 2, 1)
    for field in ('provider_retry_limit', 'eval_retry_limit'):
        with pytest.raises(ValueError):
            execution_limits({field: 257}, None, 3, 2)


@pytest.mark.parametrize('status,expected', [('CANCELED','cancelled'), ('TIMED_OUT','uncertain')])
def test_workflow_terminal_status_projects_cancellation_separately(submission, status, expected):
    store, row = submission
    store.workflow_closed(row['id'], row['owner'], status)
    assert store.get(row['id'])['state'] == expected
    assert store.tasks.get(row['owner'], row['task_id'])['state'] == expected
