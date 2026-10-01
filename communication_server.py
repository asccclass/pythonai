from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
from typing import Any
from urllib.parse import urlparse

from agent_runtime import AgentRuntime
from communication_adapters.telegram_adapter import TelegramAdapter
from communication_store import CommunicationStore
from communication_worker import CommunicationWorker, agent_runtime_command_runner, enqueue_adapter_events


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


@dataclass
class TelegramWebhookService:
    store: CommunicationStore
    adapter: TelegramAdapter
    worker: CommunicationWorker
    runtime: AgentRuntime | None = None
    allowed_senders: set[str] | None = None
    worker_interval_seconds: float = 0.2

    def __post_init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def handle_webhook(self, headers: dict[str, str], body: bytes) -> dict[str, Any]:
        commands = enqueue_adapter_events(
            self.store,
            self.adapter,
            headers,
            body,
            allowed_senders=self.allowed_senders,
        )
        return {"ok": True, "queued": len(commands)}

    def process_pending_once(self) -> int:
        processed = 0
        while self.worker.process_next() is not None:
            processed += 1
        return processed

    def start_worker_loop(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._thread.start()

    def stop_worker_loop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            processed = self.process_pending_once()
            if processed == 0:
                self._stop_event.wait(self.worker_interval_seconds)


def create_agent_runtime(async_memory_review: bool = True) -> AgentRuntime:
    import server

    messages = [{"role": "system", "content": server.SYSTEM_PROMPT}]
    guard = server.LayaGuard()
    memory_classifier_factory = server.LayaMemoryClassifier
    memory = server.safe_memory_call(server.MemoryStore)
    memory_searcher = server.VectorMemorySearcher(
        server.OpenAICompatibleEmbeddingProvider(server.get_client, server.OLLAMA_EMBEDDING_MODEL),
        store=memory,
    )
    skill_matcher = server.SkillMatcher(server.SkillRegistry())
    semantic_extractor = server.LLMSemanticExtractor(server.get_client, server.OLLAMA_MODEL)
    procedure_matcher = server.LLMProcedureSimilarityMatcher(server.get_client, server.OLLAMA_MODEL)
    memory_worker = server.MemoryReviewWorker(
        async_mode=async_memory_review,
        embedding_provider=memory_searcher.embedding_provider,
    )
    memory_worker.enqueue_pending_reviews(memory, memory_classifier_factory, semantic_extractor, procedure_matcher)
    memory_worker.enqueue_pending_embedding_backfills(memory)
    return AgentRuntime(
        messages=messages,
        guard=guard,
        memory=memory,
        memory_searcher=memory_searcher,
        skill_matcher=skill_matcher,
        memory_worker=memory_worker,
        memory_classifier_factory=memory_classifier_factory,
        semantic_extractor=semantic_extractor,
        procedure_matcher=procedure_matcher,
        run_agent=server.run_agent,
        async_memory_review=async_memory_review,
    )


def create_telegram_service(runtime: AgentRuntime | None = None) -> TelegramWebhookService:
    runtime = runtime or create_agent_runtime()
    store = CommunicationStore()
    adapter = TelegramAdapter()
    worker = CommunicationWorker(store, {"telegram": adapter}, agent_runtime_command_runner(runtime))
    return TelegramWebhookService(
        store=store,
        adapter=adapter,
        worker=worker,
        runtime=runtime,
        allowed_senders=parse_allowed_senders(os.environ.get("COMM_ALLOWED_SENDERS", "")),
    )


def parse_allowed_senders(value: str) -> set[str] | None:
    senders = {item.strip() for item in value.split(",") if item.strip()}
    return senders or None


def create_request_handler(service: TelegramWebhookService) -> type[BaseHTTPRequestHandler]:
    class CommunicationRequestHandler(BaseHTTPRequestHandler):
        server_version = "MiniAgentCommunication/0.1"

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/webhooks/telegram":
                self._send_json(404, {"ok": False, "error": "not_found"})
                return

            try:
                body = self._read_body()
                result = service.handle_webhook(dict(self.headers.items()), body)
            except PermissionError as error:
                self._send_json(403, {"ok": False, "error": str(error)})
                return
            except json.JSONDecodeError:
                self._send_json(400, {"ok": False, "error": "invalid_json"})
                return
            except Exception as error:
                self._send_json(500, {"ok": False, "error": str(error)})
                return
            self._send_json(202, result)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._send_json(200, {"ok": True})
                return
            self._send_json(404, {"ok": False, "error": "not_found"})

        def log_message(self, format: str, *args: Any) -> None:
            if os.environ.get("COMMUNICATION_ACCESS_LOG"):
                super().log_message(format, *args)

        def _read_body(self) -> bytes:
            content_length = int(self.headers.get("Content-Length", "0") or "0")
            return self.rfile.read(content_length)

        def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return CommunicationRequestHandler


def run_http_server(service: TelegramWebhookService, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    service.start_worker_loop()
    httpd = ThreadingHTTPServer((host, port), create_request_handler(service))
    try:
        print(f"Communication server ready on http://{host}:{port}")
        httpd.serve_forever()
    finally:
        service.stop_worker_loop()
        httpd.server_close()
        memory_worker = getattr(service.runtime, "memory_worker", None)
        if memory_worker is not None:
            memory_worker.stop()


def main() -> None:
    host = os.environ.get("COMMUNICATION_HOST", DEFAULT_HOST)
    port = int(os.environ.get("COMMUNICATION_PORT", str(DEFAULT_PORT)))
    run_http_server(create_telegram_service(), host=host, port=port)


if __name__ == "__main__":
    main()
