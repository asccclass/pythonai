"""Bounded public-page reads through the agent-browser CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from urllib.parse import urlsplit

from ..process import run_process


class AgentBrowserError(RuntimeError):
    """A safe, user-facing failure from agent-browser."""


AGENT_BROWSER_NPM_PACKAGE = 'agent-browser@0.39.1'


def _command_prefix() -> list[str]:
    configured = os.environ.get('AGENT_BROWSER_EXECUTABLE')
    if configured:
        return [configured]
    installed = shutil.which('agent-browser')
    if installed:
        return [installed]
    root = Path(__file__).resolve().parents[2]
    local = root / 'node_modules' / '.bin' / ('agent-browser.cmd' if os.name == 'nt' else 'agent-browser')
    if local.is_file():
        return [str(local)]
    npx = shutil.which('npx')
    if npx:
        # Keep automatic bootstrap deterministic when the CLI is not globally installed.
        return [npx, '--yes', AGENT_BROWSER_NPM_PACKAGE]
    raise AgentBrowserError('agent-browser is unavailable; install agent-browser or Node.js/npx')


def _public_hostname(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        raise ValueError('agent-browser requires an HTTP(S) URL')
    if parsed.username or parsed.password:
        raise ValueError('agent-browser rejects URL credentials')
    return parsed.hostname.lower()


def _text_from_response(payload: object) -> str:
    if not isinstance(payload, dict) or payload.get('success') is not True:
        detail = payload.get('error') if isinstance(payload, dict) else None
        raise AgentBrowserError(str(detail or 'agent-browser returned an invalid response'))
    data = payload.get('data')
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ('content', 'text', 'markdown'):
            value = data.get(key)
            if isinstance(value, str):
                return value
    for key in ('content', 'text', 'markdown'):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    raise AgentBrowserError('agent-browser response did not contain page text')


def read_public_page(url: str, *, timeout: float = 15) -> str:
    """Read an unauthenticated public page with a per-request domain allowlist."""
    host = _public_hostname(url)
    # `--json` is a read subcommand option. Before the subcommand, agent-browser
    # exits successfully but prints no JSON, which turns every tool call into a
    # false failed step during task acceptance.
    command = _command_prefix() + ['--allowed-domains', host, 'read', url,
                                   '--timeout', str(int(timeout * 1000)), '--json']
    try:
        result = run_process(command, timeout=timeout, max_output_bytes=1024 * 1024)
    except FileNotFoundError as error:
        raise AgentBrowserError('agent-browser executable is not installed') from error
    if result.returncode:
        detail = ' '.join(str(result.stderr or result.stdout or 'agent-browser failed').split())[:512]
        raise AgentBrowserError(detail)
    try:
        return _text_from_response(json.loads(result.stdout))
    except json.JSONDecodeError as error:
        raise AgentBrowserError('agent-browser returned invalid JSON') from error
