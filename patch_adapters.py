import os
import re

content = ""
with open('communication_server.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Replace TelegramWebhookService definition
content = content.replace("class TelegramWebhookService:", "class MultiWebhookService:")
content = content.replace("adapter: TelegramAdapter", "adapters: dict[str, Any]")

# Replace handle_webhook
old_handle_webhook = '''    def handle_webhook(self, headers: dict[str, str], body: bytes) -> dict[str, Any]:
        for message in self.adapter.parse_events(headers, body):
            print(
                "Telegram message received: "
                f"conversation={message.conversation_id} sender={message.sender_id} text={message.text}"
            )
        commands = enqueue_adapter_events(
            self.store,
            self.adapter,
            headers,
            body,
            allowed_senders=self.allowed_senders,
        )
        return {"ok": True, "queued": len(commands)}'''
        
new_handle_webhook = '''    def handle_webhook(self, adapter_name: str, headers: dict[str, str], body: bytes) -> dict[str, Any]:
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
        return {"ok": True, "queued": len(commands)}'''

content = content.replace(old_handle_webhook, new_handle_webhook)

# Replace create_telegram_service with create_multi_service
old_create = '''def create_telegram_service(runtime: AgentRuntime | None = None) -> TelegramWebhookService:
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
    )'''
    
new_create = '''from communication_adapters.line_adapter import LineAdapter
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
        
    worker = CommunicationWorker(store, adapters, agent_runtime_command_runner(runtime))
    return MultiWebhookService(
        store=store,
        adapters=adapters,
        worker=worker,
        runtime=runtime,
        allowed_senders=parse_allowed_senders(os.environ.get("COMM_ALLOWED_SENDERS", "")),
    )'''

content = content.replace(old_create, new_create)

# Replace create_request_handler
content = content.replace("def create_request_handler(service: TelegramWebhookService)", "def create_request_handler(service: MultiWebhookService)")

old_do_POST = '''        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/webhooks/telegram":
                self._send_json(404, {"ok": False, "error": "not_found"})
                return

            try:
                body = self._read_body()
                result = service.handle_webhook(dict(self.headers.items()), body)'''
                
new_do_POST = '''        def do_POST(self) -> None:
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
                    self._send_json(401, {"ok": False, "error": "unauthorized"})
                    return
                
                # Discord Ping check
                if adapter_name == "discord":
                    payload = json.loads(body.decode("utf-8"))
                    if payload.get("type") == 1:
                        self._send_json(200, {"type": 1})
                        return

                result = service.handle_webhook(adapter_name, dict(self.headers.items()), body)'''

content = content.replace(old_do_POST, new_do_POST)

# Replace main
old_main = '''def run_http_server(service: TelegramWebhookService, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    print_telegram_connection_status(service.adapter)'''
    
new_main = '''def run_http_server(service: MultiWebhookService, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    if "telegram" in service.adapters:
        print_telegram_connection_status(service.adapters["telegram"])'''
        
content = content.replace(old_main, new_main)

content = content.replace("run_http_server(create_telegram_service(), host=host, port=port)", "run_http_server(create_multi_service(), host=host, port=port)")

with open('communication_server.py', 'w', encoding='utf-8') as f:
    f.write(content)