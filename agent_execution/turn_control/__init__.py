"""Bounded progress tracking without granting tools additional authority."""
import hashlib
import json
import os


def iteration_limit(requested=None, saved=None):
    if requested is not None:
        value = requested
    elif saved is not None:
        value = saved
    else:
        value = int(os.environ.get('AGENT_MAX_ITERATIONS', '32'))
        from agent_execution.budgets import active_budget
        budget = active_budget()
        if budget:
            # Profile extraction already consumed its request; reserve judge/error headroom.
            value = min(value, max(1, budget.max_requests - budget.snapshot()['requests'] - 2))
    if type(value) is not int or not 1 <= value <= 256:
        raise ValueError('AGENT_MAX_ITERATIONS/max_iterations must be 1..256')
    return value


class TurnProgress:
    def __init__(self, state=None):
        self.state = state or {'calls': {}, 'failures': {}, 'blocked': 0}
        self.state.setdefault('blocked_keys', [])

    @staticmethod
    def key(name, args):
        return hashlib.sha256(json.dumps([name, args], sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def family(name, args):
        command = args.get('command') or []
        if name == 'run_command' and isinstance(command, list) and command and str(command[0]).lower() in {'mkdir', 'md', 'dir', 'ls', 'type'}:
            return name + ':' + str(command[0]).lower()
        return TurnProgress.key(name, args)

    def before(self, name, args):
        key = self.key(name, args)
        record = self.state['calls'].get(key, {})
        # Successful writes may be repeated after another step changes the file. Only
        # consecutive identical outcomes and failures are blocked; no cached writes replay.
        if key in self.state['blocked_keys'] or (self.state.get('last_key') == key and record.get('repeats', 0) >= 2) or self.state['failures'].get(self.family(name, args), 0) >= 2:
            self.state['blocked'] += 1
            if key not in self.state['blocked_keys']:
                self.state['blocked_keys'].append(key)
            return ('Error: Repeated tool operation made no progress; do not repeat it. '
                    'Use the existing result, choose another tool/strategy, or explain the blocker. '
                    'For directories use create_directory(paths=[...]); for GitHub read README/source URLs. '
                    'For edit_file read the current file and use an exact existing old_str.')
        return None

    def observe(self, name, args, result):
        if result.success and name in {'write_file', 'edit_file', 'create_directory', 'run_command', 'apply_project_patch'}:
            for old_key, record in self.state['calls'].items():
                if record.get('name') in {'read_file', 'list_files'}:
                    self.state['failures'].pop(old_key, None)
                    if old_key in self.state['blocked_keys']:
                        self.state['blocked_keys'].remove(old_key)
        key, family = self.key(name, args), self.family(name, args)
        outcome = hashlib.sha256(json.dumps([result.success, result.output, result.error], ensure_ascii=False).encode()).hexdigest()
        previous = self.state['calls'].get(key, {})
        repeats = previous.get('repeats', 0) + 1 if self.state.get('last_key') == key and previous.get('outcome') == outcome else 1
        self.state['last_key'] = key
        self.state['calls'][key] = {'outcome': outcome, 'repeats': repeats, 'success': result.success, 'name': name}
        self.state['failures'][family] = 0 if result.success else self.state['failures'].get(family, 0) + 1
        if repeats >= 2 or self.state['failures'][family] >= 2:
            return ('Progress warning: this operation returned no new result or the same tool family failed twice. '
                    'Reflect on the error and change strategy; do not repeat the same operation. '
                    'Use create_directory for mkdir; use fetched content to answer instead of fetching it again.')
        return ''
