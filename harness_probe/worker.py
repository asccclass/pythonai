"""One subprocess per rollout; imports the existing ReAct agent lazily."""
import json
import os
from pathlib import Path
import sys
import time
import traceback

from .runner import read_json, write_json


def execute(run_dir, agent=None):
    run_dir = Path(run_dir)
    request = read_json(run_dir / 'request.json')
    messages = []
    outcome = {'status': 'failed', 'error': ''}
    with (run_dir / 'events.jsonl').open('a', encoding='utf-8', buffering=1) as stream:
        def emit(kind, data):
            stream.write(json.dumps({'timestamp': time.time(), 'type': kind, 'data': data},
                                    ensure_ascii=False, default=str) + '\n')
            if kind == 'error':
                outcome.update(status='failed', error=str(data))
            elif kind == 'approval.required':
                outcome.update(status='blocked', error='Human approval required')
        try:
            from agent_execution import execution_scope
            from permissions import current_role, set_current_role
            from agent_execution.budgets import budget_scope
            workspace = run_dir / 'workspace'
            role = os.environ.get('PYTHONAI_PROBE_ROLE', 'viewer')
            if role not in {'viewer', 'operator', 'admin'}:
                raise ValueError('Invalid probe role')
            with set_current_role(role), execution_scope(
                    source='harness_probe', identity='probe', session=request['run_id'],
                    project='probe', task_id=request['run_id'],
                    workspace=workspace, repository_workspace=workspace), budget_scope():
                if agent is None:
                    import server
                    agent = server.run_agent
                messages = [{'role': 'system', 'content':
                    'Execute the user task. Source material is in inputs/. Harness scripts are in harness/scripts/. '
                    'Write deliverable files in the workspace root. Do not modify inputs/ or harness/.'}]
                for path in sorted((workspace / 'harness').glob('*.md')):
                    messages.append({'role': 'system', 'content': path.read_text(encoding='utf-8')})
                messages.append({'role': 'user', 'content': request['task']['prompt']})
                emit('run.start', {'task_id': request['task']['id'], 'role': current_role()})
                outcome['status'] = 'completed'
                reply = agent(messages, event_callback=emit)
                if isinstance(reply, str) and reply.startswith('Error:') and not outcome['error']:
                    outcome.update(status='failed', error=reply)
                (run_dir / 'answer.txt').write_text(str(reply or ''), encoding='utf-8')
        except Exception as exc:
            from exceptions import TaskSuspendedException
            status = 'blocked' if isinstance(exc, TaskSuspendedException) else 'failed'
            emit('error', {'message': f'{type(exc).__name__}: {exc}', 'traceback': traceback.format_exc()})
            outcome.update(status=status, error=f'{type(exc).__name__}: {exc}')
            traceback.print_exc()
        finally:
            write_json(run_dir / 'messages.json', messages)
            emit('run.end', outcome)
            write_json(run_dir / 'worker_result.json', outcome)
    return 0 if outcome['status'] == 'completed' else 1


if __name__ == '__main__':
    sys.exit(execute(sys.argv[1]))
