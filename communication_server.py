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
from scheduler import SchedulerWorker
from communication_worker import CommunicationWorker, agent_runtime_command_runner, enqueue_adapter_events


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


@dataclass
class MultiWebhookService:
    store: CommunicationStore
    adapters: dict[str, Any]
    worker: CommunicationWorker
    runtime: AgentRuntime | None = None
    allowed_senders: set[str] | None = None
    worker_interval_seconds: float = 0.2

    def __post_init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def handle_webhook(self, adapter_name: str, headers: dict[str, str], body: bytes) -> dict[str, Any]:
        adapter = self.adapters.get(adapter_name)
        if not adapter:
            raise KeyError(f"Adapter not found: {adapter_name}")
            
        for message in adapter.parse_events(headers, body):
            print(
                f"{adapter_name.capitalize()} message received: "
                f"conversation={message.conversation_id} sender={message.sender_id} text={message.text}"
            )
        commands = enqueue_adapter_events(
            self.store,
            adapter,
            headers,
            body,
            allowed_senders=self.allowed_senders,
        )
        return {"ok": True, "queued": len(commands)}


class TelegramWebhookService(MultiWebhookService):
    def __init__(
        self,
        store: CommunicationStore,
        adapter: TelegramAdapter,
        worker: CommunicationWorker,
        runtime: AgentRuntime | None = None,
        allowed_senders: set[str] | None = None,
        worker_interval_seconds: float = 0.2,
    ) -> None:
        super().__init__(
            store=store,
            adapters={"telegram": adapter},
            worker=worker,
            runtime=runtime,
            allowed_senders=allowed_senders,
            worker_interval_seconds=worker_interval_seconds,
        )
        self.adapter = adapter

    def handle_webhook(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if len(args) == 2 and isinstance(args[0], dict):
            return super().handle_webhook("telegram", args[0], args[1])
        return super().handle_webhook(*args, **kwargs)

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
        self.worker.drain(timeout=2.0)

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            processed = self.process_pending_once()
            if processed == 0:
                self._stop_event.wait(self.worker_interval_seconds)


def create_agent_runtime(async_memory_review: bool = True) -> AgentRuntime:
    import server

    messages = [{"role": "system", "content": server.build_system_prompt()}]
    guard = server.LayaGuard()
    memory_classifier_factory = server.LayaMemoryClassifier
    memory = server.safe_memory_call(server.MemoryStore)
    memory_searcher = server.VectorMemorySearcher(
        server.OpenAICompatibleEmbeddingProvider(server.get_client, server.OLLAMA_EMBEDDING_MODEL),
        store=memory,
    )
    skill_registry = server.SkillRegistry()
    print_loaded_skills(skill_registry)
    skill_matcher = server.SkillMatcher(skill_registry)
    semantic_extractor = server.LLMSemanticExtractor(server.get_client, server.OLLAMA_MODEL)
    procedure_matcher = server.LLMProcedureSimilarityMatcher(server.get_client, server.OLLAMA_MODEL)
    worker_lease = server.BackgroundWorkerLease.acquire(memory)
    if worker_lease.acquired:
        memory_worker = server.MemoryReviewWorker(
            async_mode=async_memory_review,
            embedding_provider=memory_searcher.embedding_provider,
            lease=worker_lease,
        )
        memory_worker.enqueue_pending_reviews(memory, memory_classifier_factory, semantic_extractor, procedure_matcher)
        memory_worker.enqueue_pending_embedding_backfills(memory)
    else:
        memory_worker = server.QueueOnlyMemoryWorker()
        print("Memory background worker already active in another process; this process will only enqueue review jobs.")
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
        run_skill=server.run_skill,
        async_memory_review=async_memory_review,
    )


def print_loaded_skills(registry: Any) -> list[str]:
    loaded = [skill.name for skill, error in registry.load_results() if skill is not None and error is None]
    if loaded:
        print(f"Loaded operational skills: {', '.join(loaded)}")
    else:
        print("Loaded operational skills: none")
    return loaded


def print_telegram_connection_status(adapter_or_adapters: Any) -> dict[str, Any] | None:
    adapter = adapter_or_adapters.get("telegram") if isinstance(adapter_or_adapters, dict) else adapter_or_adapters
    if adapter is None:
        return None
    try:
        bot = adapter.get_me()
    except Exception as error:
        print(f"Telegram connection check failed: {error}")
        return None

    username = bot.get("username") or "<unknown>"
    bot_id = bot.get("id") or "<unknown>"
    print(f"Telegram connected: @{username} (id={bot_id})")
    return bot


from communication_adapters.line_adapter import LineAdapter
from communication_adapters.discord_adapter import DiscordAdapter

def create_multi_service(runtime: AgentRuntime | None = None) -> MultiWebhookService:
    runtime = runtime or create_agent_runtime()
    store = CommunicationStore()
    
    adapters = {}
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        adapters["telegram"] = TelegramAdapter()
    if os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") and os.environ.get("LINE_CHANNEL_SECRET"):
        adapters["line"] = LineAdapter()
    if os.environ.get("DISCORD_BOT_TOKEN"):
        adapters["discord"] = DiscordAdapter()
        
    worker = CommunicationWorker(store, adapters, agent_runtime_command_runner(runtime, store))
    return MultiWebhookService(
        store=store,
        adapters=adapters,
        worker=worker,
        runtime=runtime,
        allowed_senders=parse_allowed_senders(os.environ.get("COMM_ALLOWED_SENDERS", "")),
    )


def create_telegram_service(runtime: AgentRuntime | None = None) -> TelegramWebhookService:
    runtime = runtime or create_agent_runtime()
    store = CommunicationStore()
    adapter = TelegramAdapter()
    worker = CommunicationWorker(store, {"telegram": adapter}, agent_runtime_command_runner(runtime, store))
    return TelegramWebhookService(
        store=store,
        adapter=adapter,
        worker=worker,
        runtime=runtime,
        allowed_senders=parse_allowed_senders(os.environ.get("COMM_ALLOWED_SENDERS", "")),
        worker_interval_seconds=float(os.environ.get("COMM_WORKER_INTERVAL_SECONDS", "0.2")),
    )


def parse_allowed_senders(value: str) -> set[str] | None:
    senders = {item.strip() for item in value.split(",") if item.strip()}
    return senders or None


def create_request_handler(service: MultiWebhookService) -> type[BaseHTTPRequestHandler]:
    class CommunicationRequestHandler(BaseHTTPRequestHandler):
        server_version = "MiniAgentCommunication/0.1"

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            adapter_name = None
            if parsed.path.startswith("/webhooks/"):
                adapter_name = parsed.path.split("/")[-1]
            
            if not adapter_name or adapter_name not in service.adapters:
                self._send_json(404, {"ok": False, "error": "not_found"})
                return

            try:
                body = self._read_body()
                
                # Check signature
                if not service.adapters[adapter_name].verify_request(dict(self.headers.items()), body):
                    self._send_json(403, {"ok": False, "error": "unauthorized"})
                    return
                
                # Discord Ping check
                if adapter_name == "discord":
                    payload = json.loads(body.decode("utf-8"))
                    if payload.get("type") == 1:
                        self._send_json(200, {"type": 1})
                        return

                result = service.handle_webhook(adapter_name, dict(self.headers.items()), body)
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


scheduler_worker = SchedulerWorker()

def run_http_server(service: MultiWebhookService, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    if hasattr(service, "adapter"):
        print_telegram_connection_status(service.adapter)
    elif "telegram" in getattr(service, "adapters", {}):
        print_telegram_connection_status(service.adapters["telegram"])
    service.start_worker_loop()
    scheduler_worker.start()
    httpd = ThreadingHTTPServer((host, port), create_request_handler(service))
    try:
        print(f"Communication server ready on http://{host}:{port}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nCommunication server shutting down.")
    finally:
        scheduler_worker.stop()
        service.stop_worker_loop()
        httpd.server_close()
        memory_worker = getattr(service.runtime, "memory_worker", None)
        if memory_worker is not None:
            memory_worker.stop()


def main() -> None:
    host = os.environ.get("COMMUNICATION_HOST", DEFAULT_HOST)
    port = int(os.environ.get("COMMUNICATION_PORT", str(DEFAULT_PORT)))
    run_http_server(create_multi_service(), host=host, port=port)


if __name__ == "__main__":
    main()
