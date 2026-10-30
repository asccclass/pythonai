"""Offline authentication, refresh and MCP routing contract tests."""
import base64
import io
import json
import os
from pathlib import Path
import time
import tempfile
import unittest
from unittest.mock import patch

import httpx

from . import ContactAuth
from ..remote import RemoteMCPClient
from ...policy import execution_scope


def jwt(expiry=None):
    payload = base64.urlsafe_b64encode(json.dumps({'exp': expiry or time.time() + 900}).encode()).decode().rstrip('=')
    return 'header.' + payload + '.signature'


class ContactAITests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'CONTACTAI_EMAIL': 'member@example.com',
                                     'CONTACTAI_PASSWORD': 'private-password', 'CONTACTAI_MEMAUTH_TOKEN': ''})
        env.start()
        self.addCleanup(env.stop)

    def auth(self, handler, config=None):
        auth = ContactAuth('https://contacts.example/mcp', config or {})
        auth.http.close()
        auth.http = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(auth.close)
        return auth

    def test_gateway_login_cached_refresh_and_logout(self):
        seen = []
        first, second = jwt(), jwt(time.time() + 1800)
        def handler(request):
            body = json.loads(request.content)
            seen.append(request.url.path)
            self.assertEqual(request.headers['Origin'], 'https://contacts.example')
            if request.url.path == '/api/auth/memauth':
                self.assertEqual(body, {'email': 'member@example.com', 'password': 'private-password'})
                data = {'access_token': first, 'refresh_token': 'refresh-one'}
            elif request.url.path == '/api/auth/refresh':
                self.assertEqual(body, {'refresh_token': 'refresh-one'})
                data = {'access_token': second}  # ContactAI does not rotate refresh tokens.
            else:
                self.assertEqual(body, {'refresh_token': 'refresh-one'})
                self.assertEqual(request.headers['Authorization'], 'Bearer ' + second)
                data = None
            return httpx.Response(200, json={'success': True, 'data': data})
        auth = self.auth(handler)
        self.assertEqual(auth.access_token(), first)
        self.assertEqual(auth.access_token(), first)
        auth.session['expires'] = time.time() + 10
        self.assertEqual(auth.access_token(), second)
        auth.close()
        self.assertEqual(seen, ['/api/auth/memauth', '/api/auth/refresh', '/api/auth/logout'])

    def test_memauth_token_preferred_and_identity_isolated(self):
        seen = []
        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={'success': True, 'data': {'access_token': jwt(), 'refresh_token': 'refresh'}})
        auth = self.auth(handler)
        with execution_scope(identity='api:someone'):
            with self.assertRaises(PermissionError):
                auth.access_token()
        self.assertEqual(seen, [])
        with patch.dict(os.environ, {'CONTACTAI_MEMAUTH_TOKEN': 'memauth-access'}):
            auth.access_token()
        self.assertEqual(seen, [{'token': 'memauth-access'}])
        with execution_scope(identity='api:someone'):
            with self.assertRaises(PermissionError):
                auth.access_token()
        self.assertEqual(len(seen), 1)

    def test_missing_credentials_do_not_send_requests(self):
        auth = self.auth(lambda request: self.fail('must not connect'))
        with patch.dict(os.environ, {'CONTACTAI_PASSWORD': ''}):
            with self.assertRaises(PermissionError):
                auth.access_token()

    def test_invalid_responses_and_redirects_are_redacted_and_not_retried(self):
        responses = [httpx.Response(401, text='private-password'),
                     httpx.Response(302, headers={'Location': 'https://evil.example/'}),
                     httpx.Response(200, json={'success': False, 'error': 'private-password'}),
                     httpx.Response(200, json={'success': True, 'data': {'access_token': 'invalid', 'refresh_token': 'r'}}),
                     httpx.Response(200, text='x' * 65537, headers={'content-type': 'application/json'})]
        for response in responses:
            with self.subTest(status=response.status_code, size=len(response.content)):
                seen = []
                auth = self.auth(lambda request: seen.append(request) or response)
                with self.assertRaises(Exception) as error:
                    auth.access_token()
                self.assertNotIn('private-password', str(error.exception))
                with self.assertRaises(PermissionError):
                    auth.access_token()
                self.assertEqual(len(seen), 1)

    def test_refresh_failure_requires_explicit_reconnect(self):
        seen = []
        def handler(request):
            seen.append(request.url.path)
            if len(seen) == 1:
                return httpx.Response(200, json={'success': True, 'data': {'access_token': jwt(), 'refresh_token': 'r'}})
            raise httpx.ReadTimeout('unknown refresh outcome')
        auth = self.auth(handler)
        auth.access_token()
        auth.session['expires'] = 0
        with self.assertRaises(httpx.ReadTimeout):
            auth.access_token()
        with self.assertRaises(PermissionError):
            auth.access_token()
        self.assertEqual(seen, ['/api/auth/memauth', '/api/auth/refresh'])
        self.assertIsNone(auth.session)

    def test_remote_discovery_manager_routes_health_and_no_write_replay(self):
        from mcp_manager import MCPManager
        calls = []
        access = jwt()
        def handler(request):
            if request.url.path != '/mcp':
                return httpx.Response(200, json={'success': True, 'data': {'access_token': access, 'refresh_token': 'r'}})
            self.assertEqual(request.headers['Authorization'], 'Bearer ' + access)
            self.assertEqual(request.headers['MCP-Protocol-Version'], '2025-06-18')
            value = json.loads(request.content)
            calls.append(value['method'])
            if value['method'] == 'notifications/initialized':
                return httpx.Response(202)
            if value['method'] == 'initialize':
                self.assertEqual(value['params']['protocolVersion'], '2025-06-18')
                result = {'protocolVersion': '2025-06-18'}
            elif value['method'] == 'tools/list':
                result = {'tools': [{'name': 'search_contacts', 'inputSchema': {'type': 'object'}},
                                    {'name': 'create_contact', 'inputSchema': {'type': 'object'}}]}
            elif value['params']['name'] == 'create_contact':
                return httpx.Response(401)
            else:
                result = {'content': [{'type': 'text', 'text': 'fixture contact'}]}
            return httpx.Response(200, json={'jsonrpc': '2.0', 'id': value['id'], 'result': result})
        config = {'url': 'https://contacts.example/mcp', 'protocol_version': '2025-06-18',
                  'health_method': 'tools/list', 'memauth': {}}
        client = RemoteMCPClient('contactai', config)
        self.addCleanup(client.stop)
        client.http.close()
        client.http = httpx.Client(transport=httpx.MockTransport(handler))
        client.oauth.http.close()
        client.oauth.http = httpx.Client(transport=httpx.MockTransport(handler))
        client.start()
        manager = MCPManager()
        manager.clients = {'contactai': client}
        self.assertEqual(len(manager.get_all_tools()), 2)
        self.assertTrue(manager.health()['contactai']['ready'])
        result = json.loads(manager.call_tool('mcp_contactai_search_contacts', {'query': 'fixture'}))
        self.assertTrue(result['success'])
        result = json.loads(manager.call_tool('mcp_contactai_create_contact', {'name': 'fixture'}))
        self.assertFalse(result['success'])
        self.assertEqual(calls.count('tools/call'), 2)

    def test_config_validation_and_default_protocol_preserved(self):
        for config in ({'protocol_version': 'old'}, {'health_method': 'tools/call'},
                       {'memauth': {'identity': 'local'}, 'token_env': 'TOKEN'},
                       {'memauth': {'identity': 'local'}, 'oauth': {'issuer': 'https://auth.example'}}):
            with self.assertRaises(ValueError):
                RemoteMCPClient('invalid', {'url': 'https://contacts.example/mcp', **config})
        for resource in ('https://contacts.example/mcp?token=secret', 'http://public.example/mcp',
                         'https://contacts.example/api', 'https://user:password@contacts.example/mcp'):
            with self.assertRaises(ValueError):
                ContactAuth(resource, {})
        client = RemoteMCPClient('default', {'url': 'https://contacts.example/mcp'})
        self.addCleanup(client.stop)
        self.assertEqual(client.protocol_version, '2025-11-25')
        config = json.loads((Path(__file__).parents[3] / 'mcp_config.json').read_text(encoding='utf-8'))
        self.assertIn('aisms', config['mcpServers'])
        self.assertEqual(config['mcpServers']['contactai']['protocol_version'], '2025-06-18')

    def test_cli_redacts_credentials_restores_environment_and_closes(self):
        from .__main__ import main
        from unittest.mock import Mock
        config = {'mcpServers': {'contactai': {'url': 'https://contacts.example/mcp', 'memauth': {}}}}
        client = Mock()
        client.start.side_effect = PermissionError('private-password')
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'config.json'
            source.write_text(json.dumps(config), encoding='utf-8')
            output = io.StringIO()
            with (patch('sys.argv', ['contactai', '--config', str(source), '--prompt']),
                  patch('builtins.input', return_value='prompt@example.com'),
                  patch('getpass.getpass', return_value='prompt-password'),
                  patch('sys.stdout', output),
                  patch('agent_execution.extensions.contactai.__main__.RemoteMCPClient', return_value=client)):
                self.assertEqual(main(), 1)
            client.stop.assert_called_once()
            self.assertEqual(json.loads(output.getvalue()), {'ready': False, 'error_type': 'PermissionError'})
            self.assertEqual(os.environ['CONTACTAI_EMAIL'], 'member@example.com')
            self.assertEqual(os.environ['CONTACTAI_PASSWORD'], 'private-password')

    def test_protocol_mismatch_stops_before_discovering_tools(self):
        seen = []
        def handler(request):
            value = json.loads(request.content)
            seen.append(value['method'])
            return httpx.Response(200, json={'jsonrpc': '2.0', 'id': value['id'],
                                            'result': {'protocolVersion': '2025-06-18'}})
        client = RemoteMCPClient('default', {'url': 'https://contacts.example/mcp'})
        self.addCleanup(client.stop)
        client.http.close()
        client.http = httpx.Client(transport=httpx.MockTransport(handler))
        with self.assertRaises(ValueError):
            client.start()
        self.assertEqual(seen, ['initialize'])


if __name__ == '__main__':
    unittest.main()
