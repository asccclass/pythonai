"""Bounded NDJSON replacement proposals, never direct editor/file mutations."""
from agent_configuration import load_text
import json
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import time
import uuid

from ..api import authenticate
from ..policy import execution_scope, resolve_path, current_context
from ..budgets import budget_scope, budget_client
from ..tasks import TaskStore, task_owner
from permissions import get_user_role, set_current_role


class IDERequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Do not log tokens or prompts.

    def send_json(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.connection.settimeout(15)
        identity = authenticate(self.headers)
        if not identity:
            return self.send_json(401, {'error': 'unauthorized'})
        if get_user_role(identity) != 'admin':
            return self.send_json(403, {'error': 'forbidden'})
        project = self.headers.get('X-Agent-Project', 'default')
        session = self.headers.get('X-Agent-Session', 'dashboard')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', project) or not session or len(session) > 128:
            return self.send_json(400, {'error': 'invalid_scope'})
        if self.path not in {'/api/inline_chat', '/api/autocomplete'}:
            return self.send_json(404, {'error': 'not_found'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 128 * 1024 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise ValueError('Invalid request size')
            req = json.loads(self.rfile.read(size).decode('utf-8'))
            if not isinstance(req, dict):
                raise ValueError('Expected object')
            with set_current_role('admin'), execution_scope(source='api', identity=identity,
                    project=project, session=f'{project}:{session}'):
                path = resolve_path(req.get('file_path', ''))
                if not path.is_file() or path.stat().st_size > 1024 * 1024:
                    raise ValueError('Expected bounded workspace file')
                if self.path == '/api/autocomplete':
                    prefix = req.get('prefix', '')
                    if not isinstance(prefix, str) or len(prefix.encode()) > 65536:
                        raise ValueError('Invalid completion prefix')
                    from server import get_client, OLLAMA_MODEL
                    with execution_scope(task_id='ide-completion-' + uuid.uuid4().hex), budget_scope():
                        result = budget_client(get_client().with_options(timeout=15, max_retries=0)).chat.completions.create(
                            model=OLLAMA_MODEL, messages=[{'role':'user','content':
                                'Complete this code prefix; output only the continuation:\n' + prefix[-2000:]}],
                            max_tokens=60, temperature=0.2, timeout=15)
                    completion = result.choices[0].message.content or ''
                    if not isinstance(completion, str) or len(completion.encode()) > 65536:
                        raise ValueError('Invalid completion output')
                    return self.send_json(200, {'completion': completion})
                self.propose(req, path)
        except (ValueError, UnicodeError, TypeError):
            self.send_json(400, {'error': 'bad_request'})
        except PermissionError:
            self.send_json(403, {'error': 'workspace_denied'})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.send_json(502, {'error': 'generation_failed'})

    def propose(self, req, path):
        selected = req.get('selected_text'); prompt = req.get('prompt')
        if not isinstance(selected, str) or not 0 < len(selected.encode()) <= 65536:
            raise ValueError('Invalid selection')
        if not isinstance(prompt, str) or not 0 < len(prompt.encode()) <= 8192:
            raise ValueError('Invalid prompt')
        # No disk context is read: dirty/unsaved editor selection is the authoritative input.
        from server import get_client, OLLAMA_MODEL
        messages = [{'role': 'system', 'content': load_text('instructions.json', 'ide_edit')},
                    {'role': 'user', 'content': json.dumps({'selected_code': selected, 'request': prompt}, ensure_ascii=False)}]
        key = 'ide-' + uuid.uuid4().hex
        store = TaskStore(); owner = task_owner(current_context())
        lease = store.claim(owner, key, prompt, [], criteria={'goal': prompt, 'checks': []})
        store.checkpoint(owner, key, lease, 'model_pending', {'messages': [],
                         'plan': 'Generate replacement → review diff → explicit editor apply/undo',
                         'file': path.name})
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
        self.send_header('Connection', 'close')
        self.end_headers(); self.close_connection = True
        stream = None
        scopes = ExitStack()
        def emit(value):
            self.wfile.write((json.dumps(value, ensure_ascii=False) + '\n').encode('utf-8')); self.wfile.flush()
        try:
            scopes.enter_context(execution_scope(task_id=key, check_cancelled=lambda: store.get(owner, key)['state'] == 'cancelled'))
            scopes.enter_context(budget_scope())
            emit({'type': 'start', 'task_id': key})
            stream = budget_client(get_client().with_options(timeout=30, max_retries=0)).chat.completions.create(
                model=OLLAMA_MODEL, messages=messages, stream=True, max_tokens=2048)
            started = time.monotonic(); total = 0; text = ''
            for chunk in stream:
                if time.monotonic() - started > 90 or store.get(owner, key)['state'] == 'cancelled':
                    raise InterruptedError('Generation cancelled')
                if not chunk.choices:
                    continue
                value = chunk.choices[0].delta.content or ''
                total += len(value.encode())
                if total > 1024 * 1024:
                    raise ValueError('Proposal exceeds 1 MiB')
                text += value
                emit({'type': 'delta', 'text': value})
            if not text:
                raise ValueError('Empty proposal')
            store.finish(owner, key, lease, 'completed', text)
            emit({'type': 'done', 'task_id': key})
        except (BrokenPipeError, ConnectionResetError, InterruptedError):
            store.cancel(owner, key)
        except Exception:
            if store.get(owner, key)['state'] != 'cancelled':
                store.finish(owner, key, lease, 'failed', 'Generation failed; original editor content retained')
            try:
                emit({'type': 'error', 'error': 'generation_failed'})
            except OSError:
                pass
        finally:
            try:
                if stream is not None and hasattr(stream, 'close'):
                    stream.close()
            finally:
                scopes.close()


def run_ide_server(host='127.0.0.1', port=11435):
    server = ThreadingHTTPServer((host, port), IDERequestHandler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
