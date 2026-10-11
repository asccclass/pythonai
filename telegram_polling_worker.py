from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import time
from typing import Callable
from urllib.error import HTTPError, URLError

from agent_runtime import AgentRuntime
from communication_adapters.telegram_adapter import TelegramAdapter
from communication_adapters.telegram_polling import PollingLease, TelegramPollingConflict, conflict_from_http
from communication_server import create_agent_runtime, parse_allowed_senders
from communication_store import CommunicationStore
from communication_worker import CommunicationWorker, agent_runtime_command_runner, _enqueue_verified_events
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
    conflict_retries: int = 2
    conflict_retry_seconds: float | None = None
    _lease: PollingLease | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.poll_timeout_seconds) is not int or not 0<=self.poll_timeout_seconds<=300:
            raise ValueError('TELEGRAM_POLL_TIMEOUT must be 0..300 seconds')
        if type(self.conflict_retries) is not int or not 0<=self.conflict_retries<=3:
            raise ValueError('TELEGRAM_POLL_CONFLICT_RETRIES must be 0..3')
        if self.conflict_retry_seconds is not None and (
                type(self.conflict_retry_seconds) not in {int,float} or not 1<=self.conflict_retry_seconds<=300):
            raise ValueError('TELEGRAM_POLL_CONFLICT_SLEEP must be 1..300 seconds')

    def poll_once(self) -> dict[str, int]:
        if self._lease is not None:
            self._wait_for_pending_request()
            self._lease.begin_request(self.poll_timeout_seconds)
        try:
            updates = self.adapter.get_updates(offset=self.offset, timeout=self.poll_timeout_seconds)
        except (HTTPError, TelegramPollingConflict):
            # Telegram returned a response; this particular request has ended.
            if self._lease is not None:self._lease.complete_request()
            raise
        if self._lease is not None:self._lease.complete_request()
        queued = 0
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self.offset = max(self.offset or 0, update_id + 1)
            body = json.dumps(update, ensure_ascii=False).encode("utf-8")
            commands = _enqueue_verified_events(
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
        self.worker.drain(timeout=2.0)
        try:
            from agent_execution.scheduling.notifications import NotificationDispatcher
            processed += NotificationDispatcher({"telegram": self.adapter}, self.allowed_senders).dispatch_once()
        except Exception as error:
            print(f"Schedule notification dispatcher: {type(error).__name__}")
        return processed

    def run_forever(self) -> None:
        with PollingLease(self.adapter.bot_token) as lease:
            self._lease=lease
            try:
                info=self.adapter.get_webhook_info()
                if info.get('url'):raise TelegramPollingConflict('webhook')
                self._wait_for_pending_request()
                print('Telegram polling worker ready. Press Ctrl+C to stop.')
                self._run_loop()
            finally:
                self._lease=None

    def _wait_for_pending_request(self) -> None:
        delay=self._lease.pending_delay()
        if delay:
            print(f'Telegram polling recovery: previous local request did not finish; waiting {delay:g} seconds before getUpdates.')
            self.sleep(delay)
        self._lease.complete_request()

    def _run_loop(self) -> None:
        # A terminated consumer may leave its last long poll alive on Telegram.
        # Keep a lifetime cap: successful intervening polls must not allow a duel.
        conflicts=0;handoff_pending=False
        while True:
            try:
                result = self.poll_once()
                if handoff_pending:
                    print('Telegram polling resumed after request handoff.')
                    handoff_pending=False
            except Exception as error:
                if isinstance(error,HTTPError) and error.code==409:error=conflict_from_http(error)
                if isinstance(error,TelegramPollingConflict):
                    if error.kind!='poller' or conflicts>=self.conflict_retries:raise error from None
                    conflicts+=1
                    handoff_pending=True
                    delay=self.conflict_retry_seconds or max(5,self.poll_timeout_seconds+5,self.error_sleep_seconds)
                    print(f'Telegram polling conflict (409): another getUpdates consumer may still be running; retrying in {delay:g} seconds ({conflicts}/{self.conflict_retries}). If this repeats, stop the other consumer on this or another host.')
                    self.sleep(delay)
                    continue
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
        conflict_retries=int(os.environ.get('TELEGRAM_POLL_CONFLICT_RETRIES','2')),
        conflict_retry_seconds=(float(os.environ['TELEGRAM_POLL_CONFLICT_SLEEP'])
            if os.environ.get('TELEGRAM_POLL_CONFLICT_SLEEP') else None),
    )


scheduler_worker = SchedulerWorker()

def main() -> None:
    polling_worker = create_telegram_polling_worker()
    scheduler_worker.start()
    try:
        polling_worker.run_forever()
    except KeyboardInterrupt:
        print()
    except TelegramPollingConflict as error:
        print(f'Telegram polling stopped: {error}')
        raise SystemExit(1) from None
    finally:
        polling_worker.worker.drain(timeout=2.0)
        scheduler_worker.stop()
        memory_worker = getattr(polling_worker.runtime, "memory_worker", None)
        if memory_worker is not None:
            memory_worker.stop()


if __name__ == "__main__":
    main()
