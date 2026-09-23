"""Small HTTP bridge for chatting with the ASCS Ollama endpoint."""

from __future__ import annotations

import json
import os

from base import list_files, load_dotenv, read_file, run_command, write_file


load_dotenv()

OLLAMA_BASE_URL = os.environ["OLLAMA_BASE_URL"]
OLLAMA_MODEL = os.environ["OLLAMA_MODEL"]
OLLAMA_API_KEY = os.environ["OLLAMA_API_KEY"]
SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8000"))


def chat_with_ollama(
    messages: list[dict[str, str]],
    *,
    stream: bool = False,
    base_url: str = OLLAMA_BASE_URL,
    model: str = OLLAMA_MODEL,
    api_key: str = OLLAMA_API_KEY,
    timeout: float = 120,
) -> dict[str, Any]:
    """Send chat messages to an Ollama-compatible /api/chat endpoint."""

    url = urljoin(base_url.rstrip("/") + "/", "api/chat")
    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["X-API-Key"] = api_key

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama request failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama request failed: {exc.reason}") from exc


class ChatHandler(BaseHTTPRequestHandler):
    server_version = "ASCSOllamaBridge/1.0"

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json({"ok": True, "model": OLLAMA_MODEL})
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        if self.path != "/chat":
            self._send_json({"error": "not found"}, status=404)
            return

        try:
            payload = self._read_json()
            messages = payload.get("messages")
            if messages is None and "prompt" in payload:
                messages = [{"role": "user", "content": str(payload["prompt"])}]
            if not isinstance(messages, list) or not messages:
                self._send_json({"error": "Provide messages or prompt."}, status=400)
                return

            result = chat_with_ollama(messages)
            self._send_json(result)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON body."}, status=400)
        except RuntimeError as exc:
            self._send_json({"error": str(exc)}, status=502)

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def _read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        return json.loads(raw_body.decode("utf-8"))

    def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_http_server() -> None:
    server = ThreadingHTTPServer((SERVER_HOST, SERVER_PORT), ChatHandler)
    print(f"Serving on http://{SERVER_HOST}:{SERVER_PORT}")
    print(f"Forwarding chat requests to {OLLAMA_BASE_URL} with model {OLLAMA_MODEL}")
    server.serve_forever()


def run_cli(prompt: str) -> str:
    result = chat_with_ollama([{"role": "user", "content": prompt}])
    message = result.get("message", {})
    content = message.get("content")
    if isinstance(content, str):
        return content
    return json.dumps(result, ensure_ascii=False)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chat with the configured Ollama model.")
    parser.add_argument(
        "prompt",
        nargs="*",
        help='Prompt text, or use "serve" to start the HTTP server.',
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.prompt and args.prompt[0] == "serve":
        run_http_server()
        return
    if not args.prompt:
        print('Usage: python server.py "your prompt"')
        print("       python server.py serve")
        return
    print(run_cli(" ".join(args.prompt)))


if __name__ == "__main__":
    main()
