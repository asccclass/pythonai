"""Real Service/Worker cancellation with a controlled, process-owning provider."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
import sys
import threading
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import pytest
pytest.importorskip('temporalio')
from openai.types.chat import ChatCompletionMessage
from temporalio.client import WorkflowFailureError, WorkflowExecutionStatus
from temporalio.worker import Worker, Replayer

from agent_execution.process import run_process
from agent_execution.temporal_backend.activity_guard import ActivityLeaseLost
from agent_execution.temporal_backend.cancellation import pending, dispatch as cancel_workflow
from agent_execution.temporal_backend.client import dispatch
from agent_execution.temporal_backend.config import Settings
from agent_execution.temporal_backend.step_activities import ACTIVITIES
from agent_execution.temporal_backend.store import Store
from agent_execution.temporal_backend.workflows import AgentTaskWorkflowV1
from agent_execution.temporal_backend.tests.test_integration import configured, server, prepare


@pytest.mark.parametrize('entry', ['offline_task', 'session', 'remote', 'remote_legacy', 'heartbeat'])
def test_real_cancellation_reaps_descendants_and_fences_late_provider(server, configured, monkeypatch, entry):
    monkeypatch.setenv('AGENT_TEMPORAL_ENGINE', 'steps')
    # Runtime artifacts excluded from evaluator workspace evidence.
    marker = configured / '.system_generated' / 'descendant.pid'
    finished = threading.Event()
    effects = []
    code = ('import subprocess,sys,time; from pathlib import Path; '
            'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"]); '
            'Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(30)')
    def response(**kwargs):
        try:
            result = run_process([sys.executable, '-c', code, str(marker)], timeout=20)
            effects.append(result.returncode)
            # Simulate a provider returning success after cancellation was observed.
            return NS(choices=[NS(message=ChatCompletionMessage(role='assistant', content='late-private-result'))])
        except ActivityLeaseLost:
            effects.append('uncertain')
            raise
        finally:
            finished.set()
    if entry == 'heartbeat':
        monkeypatch.setattr('agent_execution.temporal_backend.activity_guard.HEARTBEAT_SECONDS', .05)
        def lost_heartbeat(*args):
            if marker.exists() and marker.stat().st_size > 0:
                raise RuntimeError('injected heartbeat delivery failure')
        monkeypatch.setattr('temporalio.activity.heartbeat', lost_heartbeat)
    create = Mock(side_effect=response)
    raw = NS(chat=NS(completions=NS(create=create)))

    async def exercise():
        from temporalio import workflow
        original_patch = workflow.patched
        if entry == 'remote_legacy':
            # Produce a pre-TP14 cancellation History, then replay without this override.
            monkeypatch.setattr(workflow, 'patched', lambda name: False)
        client = await server.client()
        store, row = prepare(Settings.load())
        with ThreadPoolExecutor(max_workers=2) as executor, patch('server.get_client', return_value=raw):
            async with Worker(client, task_queue=store.queue_for('steps'), workflows=[AgentTaskWorkflowV1],
                              activities=ACTIVITIES, activity_executor=executor):
                handle = await dispatch(store, row, client)
                async with asyncio.timeout(15):
                    while not (marker.exists() and marker.stat().st_size > 0):
                        await asyncio.sleep(.05)
                if entry == 'heartbeat':
                    pass  # The liveness monitor must stop I/O without a client request.
                elif entry in {'remote', 'remote_legacy'}:
                    await handle.cancel()
                else:
                    if entry == 'session':
                        store.tasks.cancel_session(row['owner'])
                    else:
                        store.tasks.cancel(row['owner'], row['task_id'])
                    assert store.get(row['id'])['state'] == 'cancelled'
                    assert not store.active()  # Previously reconciliation lost this task.
                    broken = Mock(get_workflow_handle=Mock(return_value=Mock(cancel=AsyncMock(side_effect=TimeoutError()))))
                    with pytest.raises(TimeoutError):
                        await cancel_workflow(store, row, broken)
                    restarted = Store(store.path)
                    event, = pending(restarted, restarted.settings(row), {row['queue']})
                    await cancel_workflow(restarted, event, client)
                    assert not pending(restarted, restarted.settings(row), {row['queue']})
                with pytest.raises(WorkflowFailureError):
                    await asyncio.wait_for(handle.result(), 20)
                assert await asyncio.to_thread(finished.wait, 15)
                status = (await handle.describe()).status
                if entry in {'heartbeat', 'remote_legacy'}:
                    assert status == WorkflowExecutionStatus.FAILED
                elif entry == 'remote':
                    assert status == WorkflowExecutionStatus.CANCELED
                else:
                    # A local fence may fail the Activity before the cancel RPC wins.
                    assert status in {WorkflowExecutionStatus.CANCELED, WorkflowExecutionStatus.FAILED}
                # Same projection used by the production Worker for service-side cancellation.
                store.workflow_closed(row['id'], row['owner'], status.name)
                history = await handle.fetch_history()
                assert 'late-private-result' not in history.to_json()
                from agent_execution.temporal_backend.contracts import STEP_ACTIVITIES
                names = [event.activity_task_scheduled_event_attributes.activity_type.name
                         for event in history.events if event.HasField('activity_task_scheduled_event_attributes')]
                if entry == 'remote_legacy':
                    assert STEP_ACTIVITIES['recover'] in names
                    monkeypatch.setattr(workflow, 'patched', original_patch)
                elif entry == 'remote':
                    assert STEP_ACTIVITIES['recover'] not in names
                await Replayer(workflows=[AgentTaskWorkflowV1]).replay_workflow(history)
        assert effects == (['uncertain'] if entry == 'heartbeat' else [130])
        assert create.call_count == 1
        expected = 'uncertain' if entry in {'heartbeat', 'remote_legacy'} else 'cancelled'
        assert store.get(row['id'])['state'] == expected
        task = store.tasks.get(row['owner'], row['task_id'])
        assert task['state'] == expected and 'late-private-result' not in task['reply']
        with closing(store.tasks.connect()) as conn:
            assert conn.execute('SELECT requests FROM task_budgets').fetchone()[0] == 1
            assert conn.execute("SELECT state FROM operation_ledger WHERE kind='model'").fetchone()[0] == 'uncertain'
            assert not conn.execute("SELECT 1 FROM operation_ledger WHERE kind='tool'").fetchone()
        pid = int(marker.read_text())
        if os.name == 'nt':
            from memory_worker import windows_pid_exists
            assert not windows_pid_exists(pid)
        else:
            from pathlib import Path
            path = Path('/proc') / str(pid) / 'status'
            assert not path.exists() or 'State:\tZ' in path.read_text()
    asyncio.run(exercise())
