import json
from pathlib import Path
import subprocess
import sys

import pytest

from harness_probe.runner import ProbeRunner, read_json, trajectory


def test_files_in_rejects_root_and_nested_reparse_points(tmp_path, monkeypatch):
    import stat
    from types import SimpleNamespace
    from harness_probe.runner import files_in

    nested = tmp_path / 'nested'
    nested.mkdir()
    (nested / 'source.txt').write_bytes(b'keep')
    assert dict(files_in(tmp_path)) == {'nested/source.txt': b'keep'}
    original = Path.lstat
    for target in (tmp_path, nested):
        with monkeypatch.context() as patch:
            patch.setattr(Path, 'lstat', lambda path, *args, **kwargs:
                SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
                if path == target else original(path, *args, **kwargs))
            with pytest.raises(ValueError, match='regular directory|Links are unsupported'):
                list(files_in(tmp_path))


@pytest.fixture
def real_worker(monkeypatch):
    original = subprocess.Popen
    calls = []

    def launch(command, **kwargs):
        calls.append((command, kwargs))
        command = [sys.executable, str(Path(__file__).with_name('fixture_worker.py')), command[-1]]
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', launch)
    return calls


def test_execution_trace_artifacts_and_idempotency(tmp_path, real_worker):
    inputs = tmp_path / 'sources'
    inputs.mkdir()
    (inputs / 'source.txt').write_text('來源', encoding='utf-8')
    harness = tmp_path / 'candidate'
    harness.mkdir()
    (harness / 'systemprompt.md').write_text('候選指令', encoding='utf-8')
    (harness / 'scripts').mkdir()
    (harness / 'scripts' / 'helper.py').write_text('print(123)', encoding='utf-8')
    runner = ProbeRunner(tmp_path / 'runs')
    task = {'id': 'task-1', 'prompt': 'write', 'files_dir': str(inputs)}
    run = runner.execute(task, harness, 'run-1')
    result = read_json(run / 'result.json')
    assert result['status'] == 'completed'
    assert result['final_output'] == '完成測試'
    assert {row['path'] for row in result['artifacts']} == {'output.txt', 'binary.bin'}
    assert (run / 'workspace' / 'inputs' / 'source.txt').read_text(encoding='utf-8') == '來源'
    assert 'stdout evidence' in (run / 'stdout.log').read_text(encoding='utf-8')
    assert 'stderr evidence' in (run / 'stderr.log').read_text(encoding='utf-8')
    messages = read_json(run / 'messages.json')
    assert any(row['content'] == '候選指令' for row in messages)
    assert '[ACTION] tool.start' in trajectory(run)
    assert '[OBSERVATION] tool.result' in trajectory(run)
    assert 'render truncated' in trajectory(run, obs_trunc=2)
    assert runner.execute(task, harness, 'run-1') == run
    assert len(real_worker) == 1
    (inputs / 'source.txt').write_text('changed', encoding='utf-8')
    with pytest.raises(ValueError, match='different inputs'):
        runner.execute(task, harness, 'run-1')


@pytest.mark.parametrize(('prompt', 'status'), [
    ('crash', 'failed'), ('provider-error', 'failed'), ('approval', 'blocked'),
    ('suspend', 'blocked'), ('limit', 'failed'),
])
def test_failure_is_not_success(tmp_path, real_worker, prompt, status):
    run = ProbeRunner(tmp_path).execute({'id': 'x', 'prompt': prompt}, None, 'one')
    result = read_json(run / 'result.json')
    assert result['status'] == status
    assert result['error']
    assert '[ERROR]' in trajectory(run)


def test_timeout_preserves_partial_trace(tmp_path, real_worker):
    run = ProbeRunner(tmp_path).execute({'id': 'x', 'prompt': 'timeout'}, None, 'one', timeout=8)
    result = read_json(run / 'result.json')
    assert result['status'] == 'timeout'
    assert result['wall_s'] < 15
    assert '[ACTION] tool.start' in trajectory(run)


def test_existing_agent_core_executes_real_tool(tmp_path, real_worker):
    run = ProbeRunner(tmp_path).execute({'id': 'x', 'prompt': 'real-core'}, None, 'one')
    result = read_json(run / 'result.json')
    assert result['status'] == 'completed', result['error']
    assert (run / 'workspace' / 'core.txt').read_text(encoding='utf-8') == 'core output'
    assert 'write_file' in trajectory(run)


@pytest.mark.parametrize('task', [None, [], {}, {'id': '', 'prompt': 'a'},
    {'id': 'a', 'prompt': 42}, {'id': 'a', 'prompt': 'b', 'rubric': 'secret'},
    {'id': 'a', 'prompt': 'b', 'files_dir': 42}])
def test_invalid_task(tmp_path, task):
    with pytest.raises(ValueError):
        ProbeRunner(tmp_path).execute(task, None, 'one')


@pytest.mark.parametrize('timeout', [0, -1, float('nan'), float('inf'), True, '3'])
def test_invalid_timeout(tmp_path, timeout):
    with pytest.raises(ValueError):
        ProbeRunner(tmp_path).execute({'id': 'x', 'prompt': 'a'}, None, 'one', timeout=timeout)


@pytest.mark.parametrize('run_id', [None, 42, '', ' ', 'a\x00b', 'a' * 1025])
def test_invalid_run_id(tmp_path, run_id):
    with pytest.raises(ValueError):
        ProbeRunner(tmp_path).execute({'id': 'x', 'prompt': 'a'}, None, run_id)


def test_missing_worker_and_incomplete_run(tmp_path):
    runner = ProbeRunner(tmp_path, python=str(tmp_path / 'missing-python'))
    task = {'id': 'x', 'prompt': 'a'}
    run = runner.execute(task, None, 'one')
    assert read_json(run / 'result.json')['status'] == 'failed'
    (run / 'result.json').unlink()
    with pytest.raises(RuntimeError, match='interrupted'):
        runner.execute(task, None, 'one')


def test_worker_role_is_inherited(tmp_path, real_worker):
    from permissions import set_current_role
    with set_current_role('viewer'):
        run = ProbeRunner(tmp_path).execute({'id': 'x', 'prompt': 'a'}, None, 'one')
    assert real_worker[0][1]['env']['PYTHONAI_PROBE_ROLE'] == 'viewer'
    assert json.loads((run / 'events.jsonl').read_text(encoding='utf-8').splitlines()[0])['data']['role'] == 'viewer'
    assert real_worker[0][1]['env']['AGENT_TASK_DB'].startswith(str(run))
    assert real_worker[0][1]['env']['AGENT_EXECUTION_BACKEND'] == 'sqlite'


def test_unsupported_harness_is_rejected(tmp_path):
    harness = tmp_path / 'harness'
    harness.mkdir()
    (harness / 'plugins.ts').write_text('x')
    with pytest.raises(ValueError, match='Unsupported'):
        ProbeRunner(tmp_path / 'runs').execute({'id': 'x', 'prompt': 'a'}, harness, 'one')


def test_cli_invalid_request_is_json(tmp_path):
    result = subprocess.run([sys.executable, '-m', 'harness_probe', '--task', '-',
                             '--runs-root', str(tmp_path), '--run-id', 'one'],
                            input='{}', capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 2
    assert json.loads(result.stdout)['status'] == 'invalid_request'


def test_upstream_adapter_contract(tmp_path, real_worker):
    pytest.importorskip('harness2')
    from harness2.core.spec import BenchmarkAdapter, ChannelSpec
    from harness2.core.tree import validate
    from harness_probe.adapter import PythonAIAdapter
    manifest = tmp_path / 'tasks.json'
    manifest.write_text(json.dumps([{'id': 'x', 'prompt': 'a', 'split': 'test', 'domain': 'code'}]))
    channel = ChannelSpec('opencode', 'local/deterministic')
    adapter = PythonAIAdapter(manifest, tmp_path / 'adapter', channels={'proposer': channel, 'selector': channel})
    assert isinstance(adapter, BenchmarkAdapter)
    assert adapter.tasks('test') == ['x']
    assert adapter.tasks('train') == []
    assert adapter.domain_of('x') == 'code'
    validate(adapter.substrate.surface(), {'systemprompt.md': 'Do the work'}, allow_subagents=False)
    adapter.validate_tree({'systemprompt.md': 'Do the work'})
    with pytest.raises(ValueError, match='Unsupported'):
        adapter.validate_tree({'skills/example/SKILL.md': 'unsupported'})
    run = adapter.execute('x', None, 'run')
    assert adapter.delivered(run)
    assert adapter.deliverable_files(run)['workspace/output.txt'] == '完成測試'
    assert 'workspace/binary.bin' not in adapter.deliverable_files(run)
    assert adapter.deliverable_text(run) == '完成測試'
    assert '[ACTION]' in adapter.trajectory(run, obs_trunc=50000)
    assert adapter.score(run, 'x') is None
    # Exercise the actual upstream Probe call path and materialised candidate tree.
    from harness2.core.recursion import _roll
    from harness2.core.ledger import Ledger
    from harness2.core.schedule import Namespace
    view = _roll(adapter, Ledger(mode='agg', task_id='x', k=1), 'x', 'inv1',
                 {'systemprompt.md': 'Candidate from upstream Probe'},
                 lambda label: Namespace('pythonai', 'pythonai', 'agg', 1).run_id('x', label),
                 tmp_path / 'native')
    assert view.ok and view.delivered
    assert any(row['content'] == 'Candidate from upstream Probe'
               for row in read_json(view.run_dir / 'messages.json'))


def test_cli_success_stdout_is_one_json(tmp_path, real_worker, monkeypatch, capsys):
    from harness_probe.__main__ import main
    task = tmp_path / 'task.json'
    task.write_text(json.dumps({'id': 'cli', 'prompt': 'write'}), encoding='utf-8')
    monkeypatch.setattr(sys, 'argv', ['probe', '--task', str(task), '--runs-root', str(tmp_path / 'runs'), '--run-id', 'cli'])
    assert main() == 0
    assert json.loads(capsys.readouterr().out)['final_output'] == '完成測試'


def test_harness_script_validation(tmp_path):
    harness = tmp_path / 'harness'
    (harness / 'scripts').mkdir(parents=True)
    (harness / 'scripts' / 'broken.py').write_text('def broken(')
    with pytest.raises(ValueError, match='Invalid harness script'):
        ProbeRunner(tmp_path / 'runs').execute({'id': 'x', 'prompt': 'a'}, harness, 'one')


def test_interrupted_utf8_event_is_reported_without_losing_result(tmp_path):
    (tmp_path / 'events.jsonl').write_bytes(b'{"type":"model.delta","data":"\xe4')
    (tmp_path / 'result.json').write_text(json.dumps({'error': 'timeout', 'final_output': ''}))
    rendered = trajectory(tmp_path)
    assert 'Incomplete event record' in rendered
    assert '[ERROR] timeout' in rendered
