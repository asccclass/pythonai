"""Committed, sanitized histories must remain replayable by their pinned Workflow versions."""
import pytest

pytest.importorskip('temporalio')

from agent_execution.temporal_backend.tests.replay_fixtures import FIXTURE_DIR


@pytest.mark.parametrize('name', ['v1', 'v1_pre_patch', 'v2', 'v3'])
def test_committed_workflow_history_fixture_replays(name):
    import asyncio
    import json
    from temporalio.client import WorkflowHistory
    from temporalio.worker import Replayer
    from agent_execution.temporal_backend.workflows import AgentTaskWorkflowV1,AgentTaskWorkflowV2,AgentTaskWorkflowV3

    supported={'v1':[AgentTaskWorkflowV1],'v1_pre_patch':[AgentTaskWorkflowV1],
               'v2':[AgentTaskWorkflowV2],'v3':[AgentTaskWorkflowV3]}
    payload=json.loads((FIXTURE_DIR/(name+'.json')).read_text(encoding='utf-8'))
    history=WorkflowHistory.from_json(payload['workflow_id'],payload['history'])
    asyncio.run(Replayer(workflows=supported[name]).replay_workflow(history))
