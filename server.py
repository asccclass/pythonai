"""Small HTTP bridge for chatting with the ASCS Ollama endpoint."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urljoin


def load_dotenv(path: str | Path = ".env") -> None:
    """Load simple KEY=value pairs into the process environment."""

    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


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


def main() -> None:
    server = ThreadingHTTPServer((SERVER_HOST, SERVER_PORT), ChatHandler)
    print(f"Serving on http://{SERVER_HOST}:{SERVER_PORT}")
    print(f"Forwarding chat requests to {OLLAMA_BASE_URL} with model {OLLAMA_MODEL}")
    server.serve_forever()


if __name__ == "__main__":
    main()
