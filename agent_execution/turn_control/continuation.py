"""Bounded continuation control shared by synchronous and durable runners."""
from dataclasses import dataclass

KINDS = frozenset({'model', 'tool', 'evaluation'})


def assistant_frontier(messages):
    for index in range(len(messages)-1, -1, -1):
        if messages[index].get('role') == 'assistant':
            return messages[index], {m.get('tool_call_id') for m in messages[index+1:] if m.get('role') == 'tool'}
    return {}, set()


def next_kind(phase, saved):
    if phase == 'step_ready':
        kind = saved.get('next_kind')
        if not isinstance(kind, str) or kind not in KINDS:
            raise ValueError('Invalid next continuation kind')
        return kind
    if phase == 'evaluation_done':
        return 'evaluation'  # Consume the saved evaluation, without calling it again.
    if phase not in {'model_received', 'tool_done'}:
        raise ValueError('Not a completed step boundary')
    message, finished = assistant_frontier(saved['messages'])
    if any(call['id'] not in finished for call in message.get('tool_calls') or []):
        return 'tool'
    return 'evaluation' if phase == 'model_received' else 'model'


class TaskYield(Exception):
    """A trusted runner reached a persisted boundary, without another I/O step."""
    def __init__(self, next_kind):
        if next_kind not in KINDS:
            raise ValueError('Invalid continuation kind')
        self.next_kind = next_kind
        super().__init__('Task yielded at a durable step boundary')


@dataclass
class StepController:
    expected: str
    performed: bool = False

    def __post_init__(self):
        if self.expected not in KINDS:
            raise ValueError('Invalid continuation kind')

    def enter(self, kind, persist):
        if kind not in KINDS:
            raise ValueError('Invalid continuation kind')
        if self.performed or kind != self.expected:
            persist('step_ready', next_kind=kind)
            raise TaskYield(kind)
        self.performed = True
