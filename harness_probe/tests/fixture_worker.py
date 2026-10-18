"""Real subprocess fixture: deterministic agent, never contacts a provider."""
from pathlib import Path
import json
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from harness_probe.worker import execute


def agent(messages, *, event_callback):
    from base import workspace_root
    prompt = messages[-1]['content']
    if prompt == 'real-core':
        import server
        import agent_execution.turn_control.steps as steps
        from openai.types.chat import ChatCompletionMessage
        replies = iter([
            ChatCompletionMessage(role='assistant', tool_calls=[{
                'id': 'write-1', 'type': 'function', 'function': {
                    'name': 'write_file', 'arguments': json.dumps({'path': 'core.txt', 'content': 'core output'})}}]),
            ChatCompletionMessage(role='assistant', content='Core completed'),
        ])
        steps.model_step = lambda *args, **kwargs: next(replies)
        return server.run_agent(messages, event_callback=event_callback)
    event_callback('tool.start', {'name': 'write_file', 'arguments': {'path': 'output.txt'}})
    print('stdout evidence', flush=True)
    print('stderr evidence', file=sys.stderr, flush=True)
    if prompt == 'timeout':
        time.sleep(30)
    if prompt == 'crash':
        raise RuntimeError('fixture failure')
    if prompt == 'approval':
        event_callback('approval.required', {'approval_id': 'fixture'})
        return 'Waiting'
    if prompt == 'suspend':
        from exceptions import TaskSuspendedException
        raise TaskSuspendedException('Need input', 'tool-1', messages)
    if prompt == 'provider-error':
        event_callback('error', {'code': 'provider_error', 'message': 'Unavailable'})
        return 'Error: Model provider request failed'
    if prompt == 'limit':
        return 'Error: Agent execution limit reached'
    (workspace_root() / 'output.txt').write_text('完成測試', encoding='utf-8')
    (workspace_root() / 'binary.bin').write_bytes(b'\xff\x00')
    event_callback('tool.result', {'name': 'write_file', 'result': '完成測試'})
    return '完成測試'


if __name__ == '__main__':
    sys.exit(execute(sys.argv[1], agent=agent))
