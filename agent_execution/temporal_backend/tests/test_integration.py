"""Real CLI service/SDK tests; opt in with PYTHONAI_TEMPORAL_TEST_CLI."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

import pytest

pytest.importorskip('temporalio')
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker, Replayer
from temporalio.exceptions import WorkflowAlreadyStartedError

from agent_execution import execution_scope
from agent_execution.tasks import task_owner
from agent_execution.policy import current_context
from agent_execution.temporal_backend.activities import run_submission
from agent_execution.temporal_backend.client import dispatch
from agent_execution.temporal_backend.config import Settings
from agent_execution.temporal_backend.contracts import ACTIVITY_NAME, READ_TOOLS, validate_envelope
from agent_execution.temporal_backend.store import Store
from agent_execution.temporal_backend.workflows import ReadOnlyBridge


class LocalServer:
    def __init__(self, binary, directory):
        self.directory, self.binary = directory, binary
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.address = f'127.0.0.1:{self.port}'
        self.process = None

    def start(self):
        self.log = (self.directory / 'server.log').open('ab')
        self.process = subprocess.Popen([
            self.binary, 'server', 'start-dev', '--ip', '127.0.0.1', '--port', str(self.port),
            '--headless', '--namespace', 'pythonai-dev', '--db-filename', str(self.directory / 'temporal.db')],
            stdout=self.log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)

    async def client(self):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError('Temporal test server exited: ' + (self.directory / 'server.log').read_text(errors='replace')[-1000:])
            try:
                return await asyncio.wait_for(Client.connect(self.address, namespace='pythonai-dev'), .5)
            except (TimeoutError, RuntimeError):
                await asyncio.sleep(.1)
        raise TimeoutError('Temporal test server did not become ready')

    def stop(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.log.close()
            self.process = None


@pytest.fixture
def configured(tmp_path, monkeypatch):
    # Runtime/service databases are infrastructure, not agent-produced evidence.
    # Keep changing SQLite journals out of the evaluator's workspace snapshots.
    runtime = tmp_path / '.system_generated'
    runtime.mkdir()
    monkeypatch.setenv('AGENT_TASK_DB', str(runtime / 'tasks.db'))
    monkeypatch.setenv('AGENT_TENANT_ROOT', str(runtime / 'tenants'))
    monkeypatch.setenv('AGENT_WORKSPACE_ROOT', str(tmp_path))
    monkeypatch.setenv('AGENT_EXECUTION_BACKEND', 'temporal')
    monkeypatch.setenv('AGENT_TRACE_ENABLED', '0')
    monkeypatch.setenv('AGENT_TASK_WORKTREE', '0')
    monkeypatch.delenv('AGENT_PROJECT_WORKSPACES', raising=False)
    with execution_scope(source='local', identity='local', session='integration', project='default',
                         task_id='integration-task', workspace=tmp_path, repository_workspace=None):
        yield tmp_path


@pytest.fixture
def server(configured, monkeypatch):
    binary = os.environ.get('PYTHONAI_TEMPORAL_TEST_CLI')
    if not binary:
        pytest.skip('Set PYTHONAI_TEMPORAL_TEST_CLI for real service tests')
    service_data = configured / '.system_generated' / 'temporal-service'
    service_data.mkdir()
    instance = LocalServer(str(Path(binary).resolve()), service_data)
    monkeypatch.setenv('AGENT_TEMPORAL_ADDRESS', instance.address)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


def prepare(settings):
    context = current_context()
    owner, store = task_owner(context), Store()
    store.bind(owner, context.task_id, 'temporal')
    return store, store.submit(owner, context.task_id, [{'role': 'user', 'content': 'secret-prompt-stays-local'}],
        {'source': 'local', 'identity': 'local', 'session': 'integration', 'conversation': '',
         'project': 'default', 'workspace': str(context.workspace), 'allowed_tools': sorted(READ_TOOLS)}, 'admin', settings)


def test_service_duplicate_submission_history_replay_and_persistence(server):
    settings = Settings.load()
    store, row = prepare(settings)
    executed = []

    @activity.defn(name=ACTIVITY_NAME)
    def run(envelope: dict) -> dict:
        activity.heartbeat('started')
        def runner(messages, saved):
            executed.append(1)
            return 'finished once'
        return run_submission(validate_envelope(envelope), settings, runner=runner)

    async def exercise():
        client = await server.client()
        with ThreadPoolExecutor(max_workers=1) as executor:
            async with Worker(client, task_queue=store.queue, workflows=[ReadOnlyBridge], activities=[run], activity_executor=executor):
                handle = await dispatch(store, row, client)
                assert await asyncio.wait_for(handle.result(), 30) == {'submission_id': row['id']}
                # A lost StartWorkflow reply/repeated submission only gets the same handle.
                again = await dispatch(store, row, client)
                assert await again.result() == {'submission_id': row['id']}
                history = await handle.fetch_history()
                assert 'secret-prompt-stays-local' not in history.to_json()
                await Replayer(workflows=[ReadOnlyBridge]).replay_workflow(history)
        assert len(executed) == 1
        assert store.tasks.get(row['owner'], row['task_id'])['state'] == 'completed'
        server.stop()
        server.start()
        restarted = await server.client()
        assert await restarted.get_workflow_handle(row['workflow_id']).result() == {'submission_id': row['id']}
    asyncio.run(exercise())


def test_activity_failure_is_not_replayed_automatically(server):
    store, row = prepare(Settings.load())
    attempts = []

    @activity.defn(name=ACTIVITY_NAME)
    def fail(envelope: dict) -> dict:
        attempts.append(1)
        raise RuntimeError('injected failure')

    async def exercise():
        from temporalio.client import WorkflowFailureError
        client = await server.client()
        with ThreadPoolExecutor(max_workers=1) as executor:
            async with Worker(client, task_queue=store.queue, workflows=[ReadOnlyBridge], activities=[fail], activity_executor=executor):
                handle = await dispatch(store, row, client)
                with pytest.raises(WorkflowFailureError):
                    await asyncio.wait_for(handle.result(), 30)
        assert len(attempts) == 1
    asyncio.run(exercise())


def test_unknown_start_reply_stays_pending_and_deduplicates(configured):
    store, row = prepare(Settings.load())
    client = AsyncMock()
    client.get_workflow_handle = Mock(return_value=object())
    client.start_workflow.side_effect = TimeoutError('reply lost')
    async def exercise():
        with pytest.raises(TimeoutError):
            await dispatch(store, row, client)
        assert store.get(row['id'])['state'] == 'pending'
        client.start_workflow.side_effect = WorkflowAlreadyStartedError(row['workflow_id'], 'PythonAIReadOnlyBridgeV1')
        await dispatch(store, row, client)
        assert store.get(row['id'])['state'] == 'dispatched'
        assert client.start_workflow.call_args.kwargs['id'] == row['workflow_id']
    asyncio.run(exercise())


def test_common_entrypoint_real_worker_policy_and_model_tool_roundtrip(server, configured):
    from agent_execution.turn_control import steps
    from openai.types.chat import ChatCompletionMessage
    from agent_execution.temporal_backend.activities import execute_readonly
    from agent_execution.tasks import durable_invoke, TaskStore
    (configured / 'note.txt').write_text('read-through-temporal', encoding='utf-8')
    def reply(content=None, calls=None):
        return SimpleNamespace(choices=[SimpleNamespace(message=ChatCompletionMessage(
            role='assistant', content=content, tool_calls=calls))])
    responses = [
        reply(calls=[{'id': 'read-one', 'type': 'function', 'function': {
            'name': 'read_file', 'arguments': '{"path":"note.txt"}'}}]),
        reply('read-through-temporal'),
        reply('{"success":true,"score":1.0,"feedback":"ok"}'),
    ]
    raw = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(side_effect=responses))))
    messages = [{'role': 'user', 'content': 'Read note.txt and report its content.'}]
    fallback = Mock(side_effect=AssertionError('local fallback must never execute'))
    async def exercise():
        client = await server.client()
        store = Store()
        with (ThreadPoolExecutor(max_workers=1) as executor,
              patch('server.get_client', return_value=raw),
              patch.object(steps, 'model_step', wraps=steps.model_step) as model,
              patch.object(steps, 'tool_step', wraps=steps.tool_step) as tool,
              patch.object(steps, 'evaluation_step', wraps=steps.evaluation_step) as evaluation):
            async with Worker(client, task_queue=store.queue, workflows=[ReadOnlyBridge],
                              activities=[execute_readonly], activity_executor=executor):
                result = await asyncio.wait_for(asyncio.to_thread(durable_invoke, fallback, messages, None), 40)
        assert model.call_count == 2 and tool.call_count == 1 and evaluation.call_count == 1
        assert result == 'read-through-temporal'
        fallback.assert_not_called()
        assert any(m.get('role') == 'tool' and 'read-through-temporal' in m.get('content', '') for m in messages)
        schemas = raw.chat.completions.create.call_args_list[0].kwargs['tools']
        names = {s['function']['name'] for s in schemas}
        assert 'read_file' in names and 'run_command' not in names and 'write_file' not in names
        assert TaskStore().get(task_owner(current_context()), 'integration-task')['state'] == 'completed'
        from agent_execution.temporal_backend.ledger import Ledger
        row = store.for_task(task_owner(current_context()), 'integration-task')
        ledger = Ledger(store, row['id'], row['fence'])
        tool_record = ledger.get('model:1:tool:read-one')
        assert tool_record['state'] == 'completed'
        result = ledger.load(json.loads(tool_record['result_ref']))
        assert result['success'] and 'read-through-temporal' in result['output']
        from contextlib import closing
        with closing(store.tasks.connect()) as conn:
            kinds = [r[0] for r in conn.execute('SELECT kind FROM operation_ledger')]
        assert sorted(kinds) == ['evaluation', 'model', 'model', 'tool']
        history = await client.get_workflow_handle(row['workflow_id']).fetch_history()
        assert 'read-through-temporal' not in history.to_json()
    asyncio.run(exercise())
