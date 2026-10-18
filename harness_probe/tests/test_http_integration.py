"""Complete CLI -> worker -> existing Agent -> streaming HTTP -> real tool path."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import threading


def test_cli_with_controlled_streaming_provider(tmp_path, monkeypatch):
    requests = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            has_result = any(message['role'] == 'tool' for message in body['messages'])
            delta = {'content': 'HTTP integration completed'} if has_result else {
                'tool_calls': [{'index': 0, 'id': 'write-http', 'type': 'function', 'function': {
                    'name': 'write_file', 'arguments': json.dumps({'path': 'http.txt', 'content': 'HTTP tool output'})}}]}
            def chunk(value, finish):
                return {'id': 'controlled', 'object': 'chat.completion.chunk', 'created': 0,
                        'model': 'probe-test', 'choices': [{'index': 0, 'delta': value, 'finish_reason': finish}]}
            data = ''.join('data: ' + json.dumps(row) + '\n\n' for row in [
                chunk(delta, None), chunk({}, 'stop' if has_result else 'tool_calls')]) + 'data: [DONE]\n\n'
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(data.encode())))
            self.end_headers()
            self.wfile.write(data.encode())

    http = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('OLLAMA_BASE_URL', f'http://127.0.0.1:{http.server_port}/v1')
    monkeypatch.setenv('OLLAMA_MODEL', 'probe-test')
    monkeypatch.setenv('OLLAMA_API_KEY', 'sk-probe-test-placeholder')
    monkeypatch.setenv('AGENT_MODEL_RATES', '{}')
    monkeypatch.setenv('AGENT_TASK_MAX_COST_USD', '0')
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    task = tmp_path / 'task.json'
    task.write_text(json.dumps({'id': 'http', 'prompt': 'Write the requested file'}), encoding='utf-8')
    try:
        response = subprocess.run([sys.executable, '-m', 'harness_probe', '--task', str(task),
                                   '--runs-root', str(tmp_path / 'runs'), '--run-id', 'http/e2e/one'],
                                  capture_output=True, text=True, encoding='utf-8', timeout=45)
    finally:
        http.shutdown()
        http.server_close()
        thread.join(2)
    assert response.returncode == 0, response.stdout + response.stderr
    result = json.loads(response.stdout)
    assert result['status'] == 'completed'
    assert result['final_output'] == 'HTTP integration completed'
    run_dir = Path(result['run_dir'])
    assert (run_dir / 'workspace' / 'http.txt').read_text(encoding='utf-8') == 'HTTP tool output'
    assert len(requests) == 2
    assert all(request['stream'] for request in requests)
    events = [json.loads(row) for row in (run_dir / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
    assert {'model.start', 'model.delta', 'tool.start', 'tool.result'} <= {row['type'] for row in events}
