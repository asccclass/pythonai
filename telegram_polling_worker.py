from __future__ import annotations

from dataclasses import dataclass
import json
import os
import time
from typing import Callable
from urllib.error import HTTPError, URLError

from agent_runtime import AgentRuntime
from communication_adapters.telegram_adapter import TelegramAdapter
from communication_server import create_agent_runtime, parse_allowed_senders
from communication_store import CommunicationStore
from communication_worker import CommunicationWorker, agent_runtime_command_runner, enqueue_adapter_events
from scheduler import SchedulerWorker


@dataclass
class TelegramPollingWorker:
    store: CommunicationStore
    adapter: TelegramAdapter
    worker: CommunicationWorker
    runtime: AgentRuntime | None = None
    allowed_senders: set[str] | None = None
    poll_timeout_seconds: int = 30
    idle_sleep_seconds: float = 0.2
    error_sleep_seconds: float = 5.0
    offset: int | None = None
    sleep: Callable[[float], None] = time.sleep

    def poll_once(self) -> dict[str, int]:
        updates = self.adapter.get_updates(offset=self.offset, timeout=self.poll_timeout_seconds)
        queued = 0
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self.offset = max(self.offset or 0, update_id + 1)
            body = json.dumps(update, ensure_ascii=False).encode("utf-8")
            commands = enqueue_adapter_events(
                self.store,
                self.adapter,
                {},
                body,
                allowed_senders=self.allowed_senders,
            )
            queued += len(commands)
        processed = self.process_pending_once()
        return {"updates": len(updates), "queued": queued, "processed": processed}

    def process_pending_once(self) -> int:
        processed = 0
        while self.worker.process_next() is not None:
            processed += 1
        return processed

    def run_forever(self) -> None:
        while True:
            try:
                result = self.poll_once()
            except Exception as error:
                print(f"Telegram polling warning: {error}; retrying in {self.error_sleep_seconds:g} seconds.")
                self.sleep(self.error_sleep_seconds)
                continue
            if result["updates"] == 0 and result["processed"] == 0:
                self.sleep(self.idle_sleep_seconds)


def create_telegram_polling_worker(runtime: AgentRuntime | None = None) -> TelegramPollingWorker:
    runtime = runtime or create_agent_runtime()
    store = CommunicationStore()
    adapter = TelegramAdapter(webhook_secret="")
    worker = CommunicationWorker(store, {"telegram": adapter}, agent_runtime_command_runner(runtime, store))
    return TelegramPollingWorker(
        store=store,
        adapter=adapter,
        worker=worker,
        runtime=runtime,
        allowed_senders=parse_allowed_senders(os.environ.get("COMM_ALLOWED_SENDERS", "")),
        poll_timeout_seconds=int(os.environ.get("TELEGRAM_POLL_TIMEOUT", "30")),
        idle_sleep_seconds=float(os.environ.get("TELEGRAM_POLL_IDLE_SLEEP", "0.2")),
        error_sleep_seconds=float(os.environ.get("TELEGRAM_POLL_ERROR_SLEEP", "5")),
    )


scheduler_worker = SchedulerWorker()

def main() -> None:
    polling_worker = create_telegram_polling_worker()
    print("Telegram polling worker ready. Press Ctrl+C to stop.")
    try:
        polling_worker.run_forever()
    except KeyboardInterrupt:
        print()
    finally:
        scheduler_worker.stop()
        memory_worker = getattr(polling_worker.runtime, "memory_worker", None)
        if memory_worker is not None:
            memory_worker.stop()


if __name__ == "__main__":
    main()
