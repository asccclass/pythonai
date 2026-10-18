"""Isolated, durable execution of one task; no benchmark scores enter the agent."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def files_in(root):
    """Reject links instead of accidentally copying files outside task material."""
    root = Path(root)
    if root.is_symlink() or root.is_junction() or not root.is_dir():
        raise ValueError(f'Expected a regular directory: {root}')
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or path.is_junction():
            raise ValueError(f'Links are unsupported: {path}')
        if path.is_file():
            yield path.relative_to(root).as_posix(), path.read_bytes()


def validate_task(task):
    if not isinstance(task, dict):
        raise ValueError('Task must be a JSON object')
    if set(task) - {'id', 'prompt', 'files_dir', 'domain', 'split'}:
        raise ValueError('Unknown task fields (do not pass rubrics or reference answers)')
    for key in ('id', 'prompt'):
        if not isinstance(task.get(key), str) or not task[key].strip():
            raise ValueError(f'Task {key} must be a nonempty string')
    for key in ('domain', 'split', 'files_dir'):
        if key in task and (not isinstance(task[key], str) or not task[key].strip()):
            raise ValueError(f'Task {key} must be a nonempty string')


def validate_harness(harness):
    allowed = {'systemprompt.md', 'guardrails.md', 'memory.md', 'tool_guidance.md'}
    for name, content in harness.items():
        if name not in allowed and not re.fullmatch(r'scripts/[a-z0-9_]+\.py', name):
            raise ValueError(f'Unsupported PythonAI harness component: {name}')
        if name.endswith('.py'):
            try:
                compile(content.decode('utf-8'), name, 'exec')
            except SyntaxError as exc:
                raise ValueError(f'Invalid harness script {name}: {exc}') from exc
        else:
            content.decode('utf-8')


class ProbeRunner:
    def __init__(self, runs_root, *, python=sys.executable):
        self.root = Path(runs_root).resolve()
        self.python = str(python)

    def execute(self, task, harness_dir, run_id, *, timeout=300):
        validate_task(task)
        if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 1024 or any(ord(char) < 32 for char in run_id):
            raise ValueError('run_id must contain 1..1024 characters without control characters')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('timeout must be finite and positive')
        source = dict(files_in(task['files_dir'])) if task.get('files_dir') else {}
        harness = dict(files_in(harness_dir)) if harness_dir is not None else {}
        validate_harness(harness)
        from permissions import current_role
        role = current_role()
        identity = hashlib.sha256(json.dumps({'task': task, 'timeout': timeout, 'role': role}, sort_keys=True).encode())
        for group in (source, harness):
            identity.update(b'\x00GROUP\x00')
            for name, content in sorted(group.items()):
                identity.update(json.dumps([name, hashlib.sha256(content).hexdigest()]).encode())
        fingerprint = identity.hexdigest()
        # Hash names so Windows reserved names, case folding and trailing dots cannot collide.
        run_dir = self.root / hashlib.sha256(run_id.encode()).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            run_dir.mkdir()
        except FileExistsError:
            if (run_dir / 'result.json').is_file():
                result = read_json(run_dir / 'result.json')
                if result['fingerprint'] != fingerprint:
                    raise ValueError('run_id already belongs to different inputs')
                return run_dir
            raise RuntimeError('Run is active or interrupted; inspect traces and use a new run_id')
        workspace = run_dir / 'workspace'
        workspace.mkdir()
        for name, content in source.items():
            target = workspace / 'inputs' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        for name, content in harness.items():
            target = workspace / 'harness' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        request = {'task': {k: v for k, v in task.items() if k != 'files_dir'}, 'run_id': run_id}
        write_json(run_dir / 'request.json', request)
        env = os.environ.copy()
        env['PYTHONAI_PROBE_ROLE'] = role
        state = run_dir / 'state'
        state.mkdir()
        env.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1', AGENT_EXECUTION_BACKEND='sqlite',
                   AGENT_WORKSPACE_ROOT=str(workspace), AGENT_PROJECT_WORKSPACES='{}',
                   AGENT_TENANT_ROOT=str(state / 'tenants'), AGENT_CODE_INDEX_DIR=str(state / 'index'))
        for name in ('TASK', 'APPROVAL', 'TRACE', 'EXTENSION', 'IMPROVEMENT', 'MCP_OAUTH'):
            env[f'AGENT_{name}_DB'] = str(state / f'{name.lower()}.db')
        command = [self.python, '-m', 'harness_probe.worker', str(run_dir)]
        started = time.monotonic()
        status, error, code = 'failed', '', None
        process, job = None, None
        with (run_dir / 'stdout.log').open('wb') as stdout, (run_dir / 'stderr.log').open('wb') as stderr:
            try:
                from agent_execution.process import WindowsJob, terminate_tree
                options = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | 0x4} if os.name == 'nt' else {'start_new_session': True}
                process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1], env=env,
                                           stdout=stdout, stderr=stderr, **options)
                if os.name == 'nt':
                    job = WindowsJob(process)
                    job.resume(process)
                try:
                    code = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    terminate_tree(process)
                    process.wait()
                    status, error = 'timeout', f'Execution exceeded {timeout} seconds; effects may be incomplete'
                else:
                    if (run_dir / 'worker_result.json').exists():
                        outcome = read_json(run_dir / 'worker_result.json')
                        status, error = outcome['status'], outcome.get('error', '')
                    else:
                        error = f'Worker exited without a result (exit code {code})'
                    if code != 0 and status == 'completed':
                        status, error = 'failed', f'Worker exited with code {code}'
            except (OSError, RuntimeError) as exc:
                error = f'{type(exc).__name__}: {exc}'
            finally:
                if process is not None:
                    if process.poll() is None:
                        terminate_tree(process)
                    if job is not None:
                        job.close()
                    elif os.name != 'nt':
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    process.wait(timeout=5)
        artifacts = []
        try:
            for name, content in files_in(workspace):
                if not name.startswith(('inputs/', 'harness/')):
                    artifacts.append({'path': name, 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
        except (OSError, ValueError) as exc:
            status, error = 'failed', f'Artifact collection failed: {exc}'
        result = {'schema_version': 1, 'task_id': task['id'], 'run_id': run_id,
                  'fingerprint': fingerprint, 'status': status, 'error': error, 'exit_code': code,
                  'wall_s': time.monotonic() - started, 'artifacts': artifacts,
                  'final_output': (run_dir / 'answer.txt').read_text(encoding='utf-8', errors='replace') if (run_dir / 'answer.txt').exists() else '',
                  'run_dir': str(run_dir)}
        write_json(run_dir / 'result.json', result)
        return run_dir


def trajectory(run_dir, *, obs_trunc=50000):
    lines = []
    path = Path(run_dir) / 'events.jsonl'
    if path.exists():
        for row in path.read_text(encoding='utf-8', errors='replace').splitlines():
            try:
                event = json.loads(row)
            except ValueError:
                lines.append('[ERROR] Incomplete event record')
                continue
            kind = event['type']
            tag = 'ACTION' if kind in {'tool.start', 'model.start'} else 'ERROR' if kind == 'error' else 'OBSERVATION'
            text = json.dumps(event['data'], ensure_ascii=False)
            if obs_trunc > 0 and len(text) > obs_trunc:
                text = text[:obs_trunc] + ' [render truncated; full data in events.jsonl]'
            lines.append(f'[{tag}] {kind}: {text}')
    result = read_json(Path(run_dir) / 'result.json')
    if result['error']:
        lines.append('[ERROR] ' + result['error'])
    lines.append('[ASSISTANT] ' + result['final_output'])
    return '\n'.join(lines)
