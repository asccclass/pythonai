import json
import subprocess
from unittest.mock import patch

import pytest

from agent_execution.media.agent_browser import AgentBrowserError, read_public_page


def completed(command, payload, code=0, stderr=''):
    return subprocess.CompletedProcess(command, code, json.dumps(payload), stderr)


def test_read_public_page_uses_json_and_domain_allowlist():
    with patch('agent_execution.media.agent_browser.shutil.which', return_value='agent-browser'), \
         patch('agent_execution.media.agent_browser.run_process', return_value=completed([], {
             'success': True, 'data': {'content': 'public text'}})) as run:
        assert read_public_page('https://Docs.Example.test/guide') == 'public text'

    command = run.call_args.args[0]
    assert command == ['agent-browser', '--allowed-domains', 'docs.example.test', 'read',
                       'https://Docs.Example.test/guide', '--timeout', '15000', '--json']
    assert run.call_args.kwargs == {'timeout': 15, 'max_output_bytes': 1024 * 1024}


@pytest.mark.parametrize('url', ['file:///private/data', 'https://user:secret@example.test/'])
def test_read_public_page_rejects_unsafe_urls_without_starting_cli(url):
    with patch('agent_execution.media.agent_browser.run_process') as run:
        with pytest.raises(ValueError):
            read_public_page(url)
    run.assert_not_called()


def test_read_public_page_reports_cli_errors_without_echoing_command():
    with patch('agent_execution.media.agent_browser.shutil.which', return_value='agent-browser'), \
         patch('agent_execution.media.agent_browser.run_process', return_value=completed([], {}, 1, 'Chrome is missing')):
        with pytest.raises(AgentBrowserError, match='Chrome is missing'):
            read_public_page('https://example.test/')


def test_read_public_page_rejects_invalid_cli_json():
    result = subprocess.CompletedProcess([], 0, 'not-json', '')
    with patch('agent_execution.media.agent_browser.shutil.which', return_value='agent-browser'), \
         patch('agent_execution.media.agent_browser.run_process', return_value=result):
        with pytest.raises(AgentBrowserError, match='invalid JSON'):
            read_public_page('https://example.test/')


def test_read_public_page_uses_pinned_npx_when_cli_is_not_installed():
    with patch('agent_execution.media.agent_browser.shutil.which', return_value=None), \
         patch('agent_execution.media.agent_browser._npx_prefix', return_value=['npx']), \
         patch('agent_execution.media.agent_browser.run_process', return_value=completed([], {
             'success': True, 'data': {'content': 'public text'}})) as run:
        assert read_public_page('https://example.test/') == 'public text'

    assert run.call_args.args[0][:3] == ['npx', '--yes', 'agent-browser@0.39.1']


def test_windows_npx_uses_node_entrypoint_instead_of_shell_shim(tmp_path):
    from agent_execution.media.agent_browser import _npx_prefix
    install = tmp_path / 'node'
    cli = install / 'node_modules' / 'npm' / 'bin' / 'npx-cli.js'
    cli.parent.mkdir(parents=True)
    cli.write_text('// fixture', encoding='utf-8')
    node = install / 'node.exe'
    npx = install / 'npx.cmd'

    def which(name):
        return str(node) if name == 'node.exe' else str(npx) if name == 'npx.cmd' else None

    with patch('agent_execution.media.agent_browser.os.name', 'nt'), \
         patch('agent_execution.media.agent_browser.shutil.which', side_effect=which):
        assert _npx_prefix() == [str(node), str(cli)]
