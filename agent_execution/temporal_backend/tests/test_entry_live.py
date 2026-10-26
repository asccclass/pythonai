"""Opt-in real WebSocket/provider/Worker trial; all state stays in tmp_path."""
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import time

import pytest

DIRECTORIES = {'llm-wiki/' + name for name in
               ('sources', 'notes', 'concepts', 'projects', 'people', 'daily', 'templates')}
MARKER = 'TP11.7 WebSocket live acceptance'
GOAL = ('請實際建立 llm-wiki 結構，包含 sources、notes、concepts、projects、people、daily、templates '
        '七個子目錄，以及 llm-wiki/README.md。README 請用繁體中文簡述各目錄用途，包含標記 '
        + MARKER + '。使用 create_directory 的 paths 批次建立目錄及 write_file，'
        '不要執行命令，不要查詢網路。這就是完整任務，請實際完成後回報。')


def reviewed_operation(name, arguments):
    """Only the user's exact isolated wiki scope can receive a trial approval."""
    if name == 'create_directory':
        paths = arguments.get('paths')
        return (isinstance(paths, list) and bool(paths)
                and all(isinstance(p, str) and p in DIRECTORIES | {'llm-wiki'} for p in paths))
    if name == 'write_file':
        text = arguments.get('content')
        return (arguments.get('path') == 'llm-wiki/README.md' and isinstance(text, str)
                and MARKER in text and len(text.encode('utf-8')) <= 16384)
    if name in {'read_file', 'list_files'}:
        return arguments.get('path') in DIRECTORIES | {'llm-wiki', 'llm-wiki/README.md'}
    return False


@pytest.mark.parametrize('name,args,allowed', [
    ('create_directory', {'paths': sorted(DIRECTORIES)}, True),
    ('create_directory', {'paths': ['../outside']}, False),
    ('write_file', {'path': 'llm-wiki/README.md', 'content': MARKER}, True),
    ('write_file', {'path': 'other.md', 'content': MARKER}, False),
    ('write_file', {'path': 'llm-wiki/README.md', 'content': 'missing marker'}, False),
    ('run_command', {'command': 'anything'}, False),
])
def test_trial_approval_stays_within_exact_user_scope(name, args, allowed):
    assert reviewed_operation(name, args) is allowed


@pytest.mark.skipif(os.environ.get('PYTHONAI_TEMPORAL_ENTRY_LIVE') != '1',
                    reason='Explicit opt-in for real provider WebSocket acceptance')
def test_real_websocket_wiki_stream_approval_worker_restart_and_retrieval(tmp_path, monkeypatch):
    pytest.importorskip('temporalio')
    from dotenv import load_dotenv
    from websockets.asyncio.client import connect
    from temporalio.worker import Replayer
    from agent_execution.policy import ExecutionContext
    from agent_execution.tasks import task_owner
    from agent_execution.temporal_backend.admission import owner_key
    from agent_execution.temporal_backend.workflows import AgentTaskWorkflowV3
    from agent_execution.temporal_backend.tests.test_integration import LocalServer, frontend_port

    repository = Path(__file__).resolve().parents[3]
    load_dotenv(repository / '.env')
    binary = os.environ.get('PYTHONAI_TEMPORAL_TEST_CLI')
    assert binary, 'Verified Temporal CLI is required'
    workspace = tmp_path / 'workspace'
    runtime = workspace / '.system_generated'
    runtime.mkdir(parents=True)
    roles = runtime / 'roles.json'
    roles.write_text(json.dumps({'websocket:temporal-trial': 'admin'}), encoding='utf-8')
    service_dir = runtime / 'service'
    service_dir.mkdir()
    service = LocalServer(str(Path(binary).resolve()), service_dir)
    token = secrets.token_urlsafe(32)
    owner = task_owner(ExecutionContext(source='websocket', identity='websocket:temporal-trial',
        session='default:entry-trial', project='default'))
    settings = {
        'AGENT_TASK_DB': str(runtime / 'tasks.db'),
        'AGENT_APPROVAL_DB': str(runtime / 'approvals.db'),
        'SCHEDULER_DB_PATH': str(runtime / 'scheduler.db'),
        'COMMUNICATION_DB_PATH': str(runtime / 'communication.db'),
        'COMPANION_EVENT_DB': str(runtime / 'events.db'),
        'MEMORY_DB_PATH': str(runtime / 'memory.db'),
        'AGENT_EXTENSION_DB': str(runtime / 'extensions.db'),
        'AGENT_TENANT_ROOT': str(runtime / 'tenants'),
        'AGENT_WORKSPACE_ROOT': str(workspace),
        'AGENT_PROJECT_WORKSPACES': json.dumps({'default': str(workspace)}),
        'AGENT_EXECUTION_BACKEND': 'temporal', 'AGENT_COORDINATOR_BACKEND': 'sqlite',
        'AGENT_TEMPORAL_ADDRESS': service.address, 'AGENT_TEMPORAL_NAMESPACE': 'pythonai-dev',
        'AGENT_TEMPORAL_ENGINE': 'approval', 'AGENT_TEMPORAL_FILE_WRITES': '1',
        'AGENT_TEMPORAL_TRIAL_OWNERS': json.dumps([owner_key(owner)]),
        'AGENT_TASK_WORKTREE': '0', 'AGENT_TRACE_ENABLED': '0',
        'AGENT_TASK_MAX_REQUESTS': '16', 'AGENT_TASK_MAX_TOKENS': '128000',
        'AGENT_MODEL_MAX_OUTPUT_TOKENS': '2048', 'AGENT_TASK_DEADLINE_SECONDS': '300',
        'COMPANION_WS_TOKENS': token + ':temporal-trial', 'ROLES_CONFIG': str(roles),
        'PYTHONIOENCODING': 'utf-8', 'PYTHONUNBUFFERED': '1',
    }
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    processes, logs = [], []

    def start(module, args, name):
        output = (runtime / (name + '.log')).open('ab')
        logs.append(output)
        # Local diagnostics expose startup stalls without replacing any application behavior.
        bootstrap = ('import faulthandler,runpy,sys; '
                     'faulthandler.dump_traceback_later(45,repeat=True); '
                     'sys.argv=sys.argv[1:]; runpy.run_module(sys.argv[0],run_name="__main__")')
        process = subprocess.Popen([sys.executable, '-u', '-c', bootstrap, module, *args],
            cwd=repository, env=os.environ.copy(), stdout=output, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        processes.append(process)
        return process

    def stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def rows(sql, args=()):
        with closing(sqlite3.connect(runtime / 'tasks.db')) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(sql, args)]

    async def ready(process, name):
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            assert process.poll() is None, name + ' exited; inspect isolated log'
            if 'worker_configured' in (runtime / (name + '.log')).read_text(encoding='utf-8', errors='replace'):
                await asyncio.sleep(1)
                return
            await asyncio.sleep(.1)
        pytest.fail(name + ' startup exceeded 40 seconds')

    async def exercise():
        client = await service.client()
        worker = start('agent_execution.temporal_backend.worker', [], 'worker-1')
        await ready(worker, 'worker-1')
        port = frontend_port()
        frontend = start('uvicorn', ['websocket_server:app', '--host', '127.0.0.1',
            '--port', str(port), '--no-access-log'], 'frontend')
        url = f'ws://127.0.0.1:{port}/ws/v1'
        frames, approved, restart = [], set(), None
        request_id = 'tp11-7-entry-wiki'

        async def opened(last_seq=0):
            deadline = time.monotonic() + 40
            while True:
                assert frontend.poll() is None, 'Frontend exited; inspect isolated log'
                try:
                    ws = await connect(url, open_timeout=2)
                    break
                except OSError:
                    assert time.monotonic() < deadline, 'Frontend startup exceeded deadline'
                    await asyncio.sleep(.1)
            await ws.send(json.dumps({'type': 'hello', 'payload': {'token': token,
                'device_id': 'entry-trial', 'last_seq': last_seq,
                'capabilities': ['approval.auto_resume']}}))
            welcome = json.loads(await asyncio.wait_for(ws.recv(), 10))
            assert welcome['type'] == 'welcome', welcome
            return ws

        ws = await opened()
        try:
            await ws.send(json.dumps({'type': 'chat.send', 'id': request_id, 'payload': {'text': GOAL}}))
            deadline = time.monotonic() + 310
            final = None
            while time.monotonic() < deadline:
                try:
                    frame = json.loads(await asyncio.wait_for(ws.recv(), 20))
                except TimeoutError:
                    # Model startup/requests can take longer than one receive interval.
                    await ws.send(json.dumps({'type': 'ping', 'id': 'trial-heartbeat'}))
                    continue
                frames.append(frame)
                (runtime / 'frames.json').write_text(json.dumps(frames, ensure_ascii=False, indent=2), encoding='utf-8')
                kind, payload = frame['type'], frame.get('payload', {})
                if kind == 'error':
                    pytest.fail('WebSocket error: ' + json.dumps(payload, ensure_ascii=False))
                if kind == 'approval.required' and payload['approval_id'] not in approved:
                    assert reviewed_operation(payload['name'], payload['arguments']), payload
                    if restart is None:
                        before = rows('SELECT * FROM task_budgets WHERE owner=? AND id=?', (owner, request_id))[0]
                        stamp = rows('SELECT id,workflow_id FROM temporal_submissions WHERE owner=? AND task_id=?', (owner, request_id))[0]
                        stop(worker)  # No I/O is running: the Workflow is at a durable approval boundary.
                        worker = start('agent_execution.temporal_backend.worker', [], 'worker-2')
                        await ready(worker, 'worker-2')
                        after = rows('SELECT * FROM task_budgets WHERE owner=? AND id=?', (owner, request_id))[0]
                        assert before == after, 'Worker restart changed the persisted root budget'
                        assert rows('SELECT id,workflow_id FROM temporal_submissions WHERE owner=? AND task_id=?', (owner, request_id))[0] == stamp
                        restart = {'budget_unchanged': True, 'workflow_unchanged': True}
                    approved.add(payload['approval_id'])
                    assert len(approved) <= 12
                    await ws.send(json.dumps({'type': 'approval.decide', 'id': 'decision-' + str(len(approved)),
                        'payload': {'approval_id': payload['approval_id'], 'decision': 'approve'}}))
                if kind == 'chat.end' and not payload.get('suspended'):
                    assert not payload.get('failed'), payload
                    final = frame
                    break
            assert final is not None, 'No terminal result within bounded trial'
            assert restart is not None and len(approved) >= 2
            assert any(f['type'] == 'chat.delta' and f['payload'].get('text') for f in frames), 'No streamed text reached the WebSocket'
            assert any(f['type'] == 'approval.result' and f['payload'].get('approved') for f in frames)
        finally:
            await ws.close()

        assert all((workspace / p).is_dir() for p in DIRECTORIES)
        readme = workspace / 'llm-wiki/README.md'
        content = readme.read_text(encoding='utf-8')
        assert MARKER in content
        assert all(name.split('/')[-1] in content for name in DIRECTORIES)
        files_before = {str(p.relative_to(workspace)): (p.read_bytes(), p.stat().st_mtime_ns)
                        for p in (workspace / 'llm-wiki').rglob('*') if p.is_file()}
        before = rows('SELECT * FROM task_budgets WHERE owner=? AND id=?', (owner, request_id))[0]
        row = rows('SELECT * FROM temporal_submissions WHERE owner=? AND task_id=?', (owner, request_id))[0]
        assert row['state'] == 'completed'
        handle = client.get_workflow_handle(row['workflow_id'])
        await asyncio.wait_for(handle.result(), 20)
        history = await handle.fetch_history()
        assert GOAL not in history.to_json() and MARKER not in history.to_json()
        await Replayer(workflows=[AgentTaskWorkflowV3]).replay_workflow(history)

        # A new Worker and socket must retrieve the same durable completion without new work.
        stop(worker)
        worker = start('agent_execution.temporal_backend.worker', [], 'worker-3')
        await ready(worker, 'worker-3')
        ws = await opened()
        replayed = []
        try:
            while True:
                frame = json.loads(await asyncio.wait_for(ws.recv(), 15))
                replayed.append(frame)
                if frame['type'] == 'chat.end' and not frame['payload'].get('suspended'):
                    assert frame['payload']['text'] == final['payload']['text']
                    break
            await ws.send(json.dumps({'type': 'chat.send', 'id': request_id, 'payload': {'text': GOAL}}))
            while True:
                frame = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if frame['type'] == 'notify' and frame['payload'].get('request_id') == request_id:
                    assert frame['payload']['status'] == 'completed'
                    break
                assert frame['type'] != 'error', frame
            await asyncio.sleep(2)
            assert rows('SELECT * FROM task_budgets WHERE owner=? AND id=?', (owner, request_id))[0] == before
            assert files_before == {str(p.relative_to(workspace)): (p.read_bytes(), p.stat().st_mtime_ns)
                                    for p in (workspace / 'llm-wiki').rglob('*') if p.is_file()}
            assert len(rows('SELECT id FROM temporal_submissions WHERE owner=? AND task_id=?', (owner, request_id))) == 1
        finally:
            await ws.close()
        report = {'entry': '/ws/v1', 'provider': 'real', 'state': row['state'],
            'requests': before['requests'], 'approvals': len(approved), 'restart': restart,
            'stream_frames': sum(f['type'] == 'chat.delta' for f in frames),
            'replayed_frames': len(replayed), 'result_retrieved_without_io': True,
            'directories': sorted(DIRECTORIES), 'readme': str(readme), 'history_replayed': True}
        (runtime / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('ENTRY_LIVE_RESULT ' + json.dumps(report, ensure_ascii=False), flush=True)

    service.start()
    try:
        asyncio.run(exercise())
    except BaseException as error:
        (runtime / 'failure.json').write_text(json.dumps({'error_type': type(error).__name__,
            'accepted': False, 'tp11_7_complete': False,
            'evidence': 'frames.json and isolated frontend/worker logs'}, indent=2), encoding='utf-8')
        raise
    finally:
        for process in reversed(processes):
            stop(process)
        for output in logs:
            output.close()
        service.stop()
