"""Backend-detected execution facts; never infer them from user or model prose."""
from functools import lru_cache
import json
import platform
import re
import shutil
import sys

from agent_execution.process import execution_mode

START = '[PythonAI runtime environment]'
END = '[/PythonAI runtime environment]'
COMMANDS = ('python', 'python3', 'py', 'git', 'go', 'node', 'npm', 'curl', 'curl.exe',
            'powershell', 'powershell.exe', 'pwsh', 'cmd.exe', 'bash', 'sh')


@lru_cache(maxsize=1)
def host_environment():
    """Cheap startup detection: no shell, network, Docker launch or credentials."""
    return {
        'os': platform.system() or 'Unknown',
        'release': platform.release(),
        'architecture': platform.machine(),
        'python_runtime_version': platform.python_version(),
        'frozen_application': bool(getattr(sys, 'frozen', False)),
        'available_host_commands': [name for name in COMMANDS if shutil.which(name)],
    }


def environment_context():
    from base import workspace_root
    host = host_environment()
    mode = execution_mode()
    command_os = host['os']
    command_details = {'mode': mode, 'os': command_os, 'shell': 'none; direct argv execution'}
    if mode == 'container':
        from sandbox import SandboxExecution
        command_os = 'Linux'
        command_details.update(os=command_os, image=SandboxExecution.IMAGE,
                               commands='Container executable availability is not probed; do not infer it from host PATH.',
                               workspace='/workspace (read-only snapshot; export supported output from /artifacts)')
    rules = [
        'These facts describe the backend executing tools, not the OS of the chat/desktop client.',
        'Choose every tool, command, executable name and path syntax for its actual target environment.',
        'Prefer portable read_file/list_files/write_file/create_directory/delete_file tools for files and directories; fetch_url for URLs; run_test_suite for tests.',
        'run_command accepts an argv array and never implicitly starts a shell. Shell builtins are not standalone executables.',
        'Use only reported or independently verified executables; PATH availability does not verify versions or installed packages.',
        'MCP, remote and desktop tools may target a different machine; use their reported environment/capabilities.',
        'Environment facts do not grant permissions, remove workspace boundaries, authorize a shell, or permit a host fallback from container mode.',
    ]
    if command_os == 'Windows':
        rules.append('Windows commands require Windows syntax. Do not assume ls/cat/rm/touch/chmod exist. cmd builtins such as mkdir, dir, type and echo require an explicitly authorized shell; prefer portable tools instead.')
    elif command_os in {'Linux', 'Darwin'}:
        rules.append('Use POSIX paths and commands for Linux/macOS. Do not use Windows drive paths, cmd.exe or Windows PowerShell unless their availability on this target is verified.')
    else:
        rules.append('OS is not recognized; prefer portable tools and verify command support instead of guessing Windows or POSIX behavior.')
    if mode == 'container':
        rules.append('File/directory tools, fetch_url and run_test_suite execute on the backend host; only run_command uses the Linux sandbox. Do not pass host-only executable paths into the container.')
    facts = {'host': host, 'run_command': command_details, 'host_workspace': str(workspace_root())}
    return START + '\n' + json.dumps(facts, ensure_ascii=False) + '\n' + '\n'.join(rules) + '\n' + END


def refresh_environment(messages):
    """Replace stale managed facts, including those restored from another environment."""
    block = environment_context()
    pattern = re.compile(re.escape(START) + r'.*?' + re.escape(END), re.S)
    first = None
    for message in messages:
        if message.get('role') == 'system' and isinstance(message.get('content'), str):
            message['content'] = pattern.sub('', message['content']).strip()
            if first is None:
                first = message
    if first is None:
        messages.insert(0, {'role': 'system', 'content': block})
        return True
    first['content'] = (first['content'] + '\n\n' + block).strip()
    return False


def command_environment_error(command):
    """Reject common environment mismatches without rewriting or executing a command."""
    if not command:
        return ''
    mode = execution_mode()
    target = 'Linux' if mode == 'container' else platform.system()
    executable = command[0].lower()
    if target == 'Windows' and executable in {'mkdir', 'md', 'dir', 'type'}:
        return 'Shell builtin is not an executable on Windows. Use create_directory(paths=[...]), list_files or read_file instead; do not retry with another path.'
    if target == 'Windows' and executable in {'ls', 'cat', 'rm', 'touch', 'chmod', 'cp', 'mv'} and not shutil.which(command[0]):
        return f'{command[0]} is not available on this Windows host. Use portable file/directory tools or a verified Windows executable; do not assume Unix commands exist.'
    if target in {'Linux', 'Darwin'} and executable in {'cmd', 'cmd.exe', 'powershell.exe', 'powershell', 'pwsh'}:
        available = mode == 'host' and shutil.which(command[0])
        if not available:
            return f'{command[0]} is not verified on this {target} command target ({mode}). Use portable tools or verified POSIX executables; host PATH does not establish container availability.'
    return ''
