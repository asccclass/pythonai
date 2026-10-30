"""ContactAI sessions established through its existing MemAuth gateway."""
import base64
import json
import math
import os
import threading
import time
from urllib.parse import urlsplit

import httpx

from ..oauth import safe_url, origin
from ...policy import current_context


class ContactAuth:
    """Keep ContactAI tokens in memory; never persist MemAuth credentials."""

    def __init__(self, resource, config):
        parsed = urlsplit(safe_url(resource))
        if parsed.path != '/mcp' or parsed.query or parsed.fragment:
            raise ValueError('ContactAI resource must end in /mcp')
        self.base = origin(resource)
        self.origin = config.get('origin', self.base)
        if origin(self.origin) != self.origin:
            raise ValueError('ContactAI Origin must be an origin URL')
        self.identity = config.get('identity', 'local')
        if not isinstance(self.identity, str) or not self.identity:
            raise ValueError('ContactAI identity is required')
        self.email_env = config.get('email_env', 'CONTACTAI_EMAIL')
        self.password_env = config.get('password_env', 'CONTACTAI_PASSWORD')
        self.token_env = config.get('token_env', 'CONTACTAI_MEMAUTH_TOKEN')
        for name in (self.email_env, self.password_env, self.token_env):
            if not isinstance(name, str) or not name or len(name) > 128:
                raise ValueError('Invalid ContactAI credential environment name')
        self.http = httpx.Client(timeout=10, follow_redirects=False, trust_env=False)
        self.guard = threading.Lock()
        self.session = None
        self.failed = False

    def request(self, path, body, token=None):
        headers = {'Accept': 'application/json', 'Origin': self.origin}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        content = bytearray()
        deadline = time.monotonic() + 10
        with self.http.stream('POST', self.base + path, json=body, headers=headers) as response:
            if response.status_code in {401, 403}:
                raise PermissionError('ContactAI authentication rejected; check verified account and Origin')
            if response.status_code != 200:
                raise ConnectionError('ContactAI authentication service unavailable')
            if 'application/json' not in response.headers.get('content-type', ''):
                raise ValueError('ContactAI authentication response must be JSON')
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 65536 or time.monotonic() > deadline:
                    raise ValueError('ContactAI authentication response exceeds limit')
        result = json.loads(content)
        if not isinstance(result, dict) or result.get('success') is not True:
            raise PermissionError('ContactAI authentication failed')
        data = result.get('data')
        if path == '/api/auth/logout':
            return {}
        if not isinstance(data, dict):
            raise ValueError('Invalid ContactAI authentication data')
        return data

    @staticmethod
    def valid_token(value):
        if (not isinstance(value, str) or not 1 <= len(value) <= 8192
                or not value.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in value)):
            raise ValueError('Invalid ContactAI token')
        return value

    def token_session(self, data, previous=None):
        access = self.valid_token(data.get('access_token'))
        refresh = self.valid_token(data.get('refresh_token', (previous or {}).get('refresh_token')))
        # Exp is used only to schedule refresh. The server verifies the JWT signature.
        try:
            payload = access.split('.')[1]
            expiry = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))['exp']
        except (ValueError, IndexError, KeyError, TypeError):
            raise ValueError('ContactAI access token must contain JWT expiration') from None
        if (type(expiry) not in {int, float} or not math.isfinite(expiry)
                or not time.time() < expiry <= time.time() + 86400):
            raise ValueError('Invalid ContactAI token expiration')
        return {'access_token': access, 'refresh_token': refresh, 'expires': expiry}

    def access_token(self):
        if current_context().identity != self.identity:
            raise PermissionError('ContactAI credentials belong to another identity')
        with self.guard:
            if self.failed:
                raise PermissionError('ContactAI session failed; reconnect explicitly')
            try:
                if self.session is None:
                    token = os.environ.get(self.token_env)
                    if token:
                        body = {'token': self.valid_token(token)}
                    else:
                        email = os.environ.get(self.email_env)
                        password = os.environ.get(self.password_env)
                        if not email or not password:
                            raise PermissionError('ContactAI MemAuth credentials required in environment')
                        body = {'email': email, 'password': password}
                    self.session = self.token_session(self.request('/api/auth/memauth', body))
                elif self.session['expires'] <= time.time() + 30:
                    old = self.session
                    self.session = None
                    data = self.request('/api/auth/refresh', {'refresh_token': old['refresh_token']})
                    self.session = self.token_session(data, old)
                return self.session['access_token']
            except Exception:
                self.session = None
                self.failed = True
                raise

    def close(self):
        with self.guard:
            session, self.session = self.session, None
            self.failed = True
            try:
                if session:
                    self.request('/api/auth/logout', {'refresh_token': session['refresh_token']},
                                 session['access_token'])
            except Exception:
                pass  # Best effort: an unavailable service must not prevent shutdown.
            finally:
                self.http.close()
