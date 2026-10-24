"""Bounded progress tracking without granting tools additional authority."""
from agent_configuration import load_text
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
    REUSABLE_READS = frozenset({'fetch_url', 'browse_webpage', 'read_file', 'list_files', 'search_web'})
    BROWSER_TOOLS = frozenset({'browser_vision', 'browser_session', 'browse_webpage'})
    BROWSER_ENVIRONMENT_FAMILY = 'browser:environment'

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

    @classmethod
    def environment_family(cls, name, result):
        """Group browser installation failures independently of model prompts.

        URL, actions and vision prompts remain part of the normal operation key.
        Only errors that identify a missing Playwright/browser runtime are shared,
        so ordinary navigation or page-specific failures can still change strategy.
        """
        if name not in cls.BROWSER_TOOLS or result.success:
            return None
        detail = f'{result.error}\n{result.output}'.casefold()
        missing_executable = 'executable doesn\'t exist' in detail and 'browser' in detail
        missing_playwright = ('no module named' in detail or 'modulenotfounderror' in detail) and 'playwright' in detail
        missing_agent_browser = 'agent-browser is unavailable' in detail or 'agent-browser executable is not installed' in detail
        install_required = 'playwright install' in detail or 'agent-browser install' in detail
        if missing_executable or missing_playwright or missing_agent_browser or install_required:
            return cls.BROWSER_ENVIRONMENT_FAMILY
        return None

    def before(self, name, args):
        key = self.key(name, args)
        record = self.state['calls'].get(key, {})
        environment_blocked = (name in self.BROWSER_TOOLS and
                               self.state['failures'].get(self.BROWSER_ENVIRONMENT_FAMILY, 0) >= 2)
        if name in self.REUSABLE_READS and record.get('success'):
            return 'This read already succeeded. Reuse its evidence to answer; do not fetch it again.'
        if name == 'search_web' and self.state.get('search_result_key'):
            return ('A web search already returned usable results. Reuse those results; do not search again '
                    'with reworded keywords. Open one returned URL if more detail is required.')
        if name == 'search_web' and self.state.get('search_rate_limited'):
            self.state['blocked'] += 1
            return ('Search provider returned HTTP 429. Do not search it again in this turn, '
                    'including with different keywords. Use existing evidence, another public source, '
                    'or explain that current information could not be verified.')
        # Successful writes may be repeated after another step changes the file. Only
        # consecutive identical outcomes and failures are blocked; no cached writes replay.
        if key in self.state['blocked_keys'] or environment_blocked or (self.state.get('last_key') == key and record.get('repeats', 0) >= 2) or self.state['failures'].get(self.family(name, args), 0) >= 2:
            self.state['blocked'] += 1
            if key not in self.state['blocked_keys']:
                self.state['blocked_keys'].append(key)
            return (load_text('instructions.json', 'repeated_operation'))
        return None

    def observe(self, name, args, result):
        if name == 'search_web' and not result.success and result.evidence.get('http_status') == 429:
            self.state['search_rate_limited'] = True
        if result.success and name in {'write_file', 'edit_file', 'create_directory', 'run_command', 'apply_project_patch'}:
            for old_key, record in list(self.state['calls'].items()):
                if record.get('name') in {'read_file', 'list_files'}:
                    self.state['failures'].pop(old_key, None)
                    if old_key in self.state['blocked_keys']:
                        self.state['blocked_keys'].remove(old_key)
                    self.state['calls'].pop(old_key, None)
        key, family = self.key(name, args), self.family(name, args)
        environment_family = self.environment_family(name, result)
        outcome = hashlib.sha256(json.dumps([result.success, result.output, result.error], ensure_ascii=False).encode()).hexdigest()
        previous = self.state['calls'].get(key, {})
        repeats = previous.get('repeats', 0) + 1 if self.state.get('last_key') == key and previous.get('outcome') == outcome else 1
        self.state['last_key'] = key
        self.state['calls'][key] = {'outcome': outcome, 'repeats': repeats, 'success': result.success, 'name': name}
        if name == 'search_web' and result.success and result.evidence.get('kind') == 'duckduckgo_search':
            try:
                search_results = json.loads(result.output)
            except (TypeError, ValueError):
                search_results = []
            if (isinstance(search_results, list) and any(
                    isinstance(item, dict) and str(item.get('href') or '').startswith(('http://', 'https://'))
                    for item in search_results)):
                self.state['search_result_key'] = key
        self.state['failures'][family] = 0 if result.success else self.state['failures'].get(family, 0) + 1
        if result.success and name in self.BROWSER_TOOLS:
            self.state['failures'].pop(self.BROWSER_ENVIRONMENT_FAMILY, None)
        elif environment_family:
            self.state['failures'][environment_family] = self.state['failures'].get(environment_family, 0) + 1
        if repeats >= 2 or self.state['failures'][family] >= 2 or (environment_family and self.state['failures'][environment_family] >= 2):
            return ('Progress warning: this operation returned no new result or the same tool family failed twice. '
                    'Reflect on the error and change strategy; do not repeat the same operation. '
                    'Use create_directory for mkdir; use fetched content to answer instead of fetching it again.')
        return ''

    def reusable_record(self, name, args):
        """Return the successful read that should satisfy a redundant model call."""
        record = self.state['calls'].get(self.key(name, args), {})
        if name == 'search_web' and not record.get('success') and self.state.get('search_result_key'):
            record = self.state['calls'].get(self.state['search_result_key'], {})
        return record if record.get('success') else {}
