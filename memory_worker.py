from __future__ import annotations

from dataclasses import dataclass
import queue
import threading
import time
from typing import Any, Callable

from forgetting import run_forgetting_policy
from memory import MemoryStore
from memory_classifier import MemoryCandidateDecision
from memory_review import process_memory_review_candidates
from request_budget import background_memory_budget
from vector_search import EmbeddingProvider, format_memory_for_embedding


def safe_memory_call(operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return operation(*args, **kwargs)
    except Exception as error:
        print(f"\nMemory warning: {error}")
        return None


def retry_after_seconds(error: Exception, default: float = 5.0) -> float:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {}) if response is not None else {}
    retry_after = headers.get("retry-after") if hasattr(headers, "get") else None
    if retry_after is None:
        return default
    try:
        return max(0.0, float(retry_after))
    except (TypeError, ValueError):
        return default


def is_rate_limit_error(error: Exception) -> bool:
    return getattr(error, "status_code", None) == 429 or "429" in str(error)


@dataclass
class ProviderCooldown:
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    available_at: float = 0.0
    failures: int = 0

    def wait_if_needed(self) -> float:
        delay = max(0.0, self.available_at - self.now())
        if delay > 0:
            self.sleep(delay)
        return delay

    def record_success(self) -> None:
        self.failures = 0

    def record_rate_limit(self, retry_after: float) -> float:
        self.failures += 1
        delay = max(retry_after, 2.0 ** (self.failures - 1))
        self.available_at = self.now() + delay
        return delay


class MemoryBackgroundWorker:
    def __init__(
        self,
        async_mode: bool = True,
        embedding_provider: EmbeddingProvider | None = None,
        embedding_batch_size: int = 2,
        stale_embedding_before: str | None = None,
        cooldown: ProviderCooldown | None = None,
    ) -> None:
        self.queue: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self.async_mode = async_mode
        self.embedding_provider = embedding_provider
        self.embedding_batch_size = embedding_batch_size
        self.stale_embedding_before = stale_embedding_before
        self.cooldown = cooldown or ProviderCooldown()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        if self.async_mode:
            self._thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._thread.start()

    def enqueue(
        self,
        memory: MemoryStore | None,
        episode_id: int | None,
        memory_classifier: Any,
        semantic_extractor: Any,
        procedure_matcher: Any,
    ) -> None:
        if memory is None or episode_id is None or not hasattr(memory, "episode_events"):
            return
        job_id = self._add_review_job(memory, episode_id)
        item = (job_id, memory, episode_id, memory_classifier, semantic_extractor, procedure_matcher)
        self._enqueue_item(item)

    def enqueue_pending_reviews(
        self,
        memory: MemoryStore | None,
        memory_classifier: Any,
        semantic_extractor: Any,
        procedure_matcher: Any,
        limit: int = 25,
    ) -> int:
        if memory is None or not hasattr(memory, "pending_memory_jobs"):
            return 0
        jobs = memory.pending_memory_jobs(job_type="memory_review", limit=limit)
        for job in jobs:
            episode_id = job.get("episode_id")
            if episode_id is None:
                continue
            item = (job["id"], memory, int(episode_id), memory_classifier, semantic_extractor, procedure_matcher)
            self._enqueue_item(item)
        return len(jobs)

    def _enqueue_item(self, item: tuple[Any, ...]) -> None:
        if self.async_mode:
            self.queue.put(item)
        else:
            self._process_item(item)

    def run_maintenance(self, memory: MemoryStore | None) -> dict[str, Any]:
        if memory is None:
            return {"embedding_backfill": None}
        return {"embedding_backfill": self._backfill_embeddings(memory)}

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self.queue.get(timeout=0.1)
            except queue.Empty:
                if self.embedding_provider is not None:
                    memory = None
                continue
            try:
                self._process_item(item)
            finally:
                self.queue.task_done()

    def _process_item(self, item: tuple[Any, ...]) -> None:
        if len(item) == 6:
            job_id, memory, episode_id, memory_classifier, semantic_extractor, procedure_matcher = item
        else:
            job_id = None
            memory, episode_id, memory_classifier, semantic_extractor, procedure_matcher = item
        if callable(memory_classifier):
            memory_classifier = memory_classifier()
        review_budget = background_memory_budget()
        if hasattr(semantic_extractor, "allow_remote"):
            semantic_extractor.allow_remote = review_budget.try_acquire
        if hasattr(procedure_matcher, "allow_remote"):
            procedure_matcher.allow_remote = review_budget.try_acquire
        self.cooldown.wait_if_needed()
        try:
            self._claim_job(memory, job_id)
            episode_events = safe_memory_call(memory.episode_events, episode_id) or []
            memory_candidate = self._classify_memory(memory_classifier, episode_events)
            self._log_event(
                memory,
                episode_id,
                "memory_candidate_decision",
                metadata={"candidate": memory_candidate},
            )
            self._queue_review_candidate(memory, episode_id, memory_candidate)
            safe_memory_call(
                process_memory_review_candidates,
                memory,
                episode_id,
                semantic_extractor=semantic_extractor,
                procedure_matcher=procedure_matcher,
            )
            safe_memory_call(run_forgetting_policy, memory)
            self._backfill_embeddings(memory)
            self.cooldown.record_success()
            self._complete_job(memory, job_id)
        except Exception as error:
            if is_rate_limit_error(error):
                delay = self.cooldown.record_rate_limit(retry_after_seconds(error))
                self._defer_job(memory, job_id, delay, str(error))
                self._log_event(
                    memory,
                    episode_id,
                    "memory_background_cooldown",
                    content=str(error),
                    metadata={"retry_after_seconds": delay, "error_type": type(error).__name__},
                )
            else:
                self._fail_job(memory, job_id, str(error))
                self._log_event(
                    memory,
                    episode_id,
                    "error",
                    content=str(error),
                    metadata={"error_type": type(error).__name__, "phase": "background_memory"},
                )
        finally:
            self._log_memory_budget(memory, episode_id, "background_review", review_budget)
            safe_memory_call(memory.finish_episode, episode_id)

    def _backfill_embeddings(self, memory: MemoryStore) -> dict[str, Any] | None:
        if self.embedding_provider is None or self.embedding_batch_size <= 0:
            return None
        self.cooldown.wait_if_needed()
        candidates = memory.semantic_memories_missing_embeddings(
            limit=self.embedding_batch_size,
            stale_before=self.stale_embedding_before,
        )
        updated_ids = []
        for semantic_memory in candidates:
            embedding = self.embedding_provider.embed(format_memory_for_embedding(semantic_memory))
            memory.update_semantic_embedding(int(semantic_memory["id"]), embedding)
            updated_ids.append(int(semantic_memory["id"]))
        return {"candidate_count": len(candidates), "updated_ids": updated_ids}

    def join(self) -> None:
        if self.async_mode and self._thread is not None and self._thread.is_alive():
            self.queue.join()

    def stop(self) -> None:
        self._stop_event.set()
        if self.async_mode and self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def _classify_memory(self, memory_classifier: Any, events: list[dict[str, Any]]) -> MemoryCandidateDecision:
        result = safe_memory_call(memory_classifier.assess_episode, events)
        if isinstance(result, MemoryCandidateDecision):
            return result
        return MemoryCandidateDecision(available=False, reason="Memory classifier failed")

    def _queue_review_candidate(
        self,
        memory: MemoryStore,
        episode_id: int,
        candidate: MemoryCandidateDecision,
    ) -> None:
        if not candidate.should_extract:
            return
        self._log_event(
            memory,
            episode_id,
            "memory_review_candidate",
            metadata={
                "memory_kind": candidate.memory_kind,
                "confidence": candidate.confidence,
                "reason": candidate.reason,
            },
        )

    def _log_memory_budget(self, memory: MemoryStore, episode_id: int, phase: str, budget: Any) -> None:
        snapshot = budget.snapshot() if hasattr(budget, "snapshot") else {}
        self._log_event(memory, episode_id, "memory_budget", metadata={"phase": phase, "budget": snapshot})

    def _log_event(
        self,
        memory: MemoryStore,
        episode_id: int,
        event_type: str,
        role: str | None = None,
        content: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        safe_memory_call(memory.add_event, episode_id, event_type, role=role, content=content, metadata=metadata)

    def _add_review_job(self, memory: MemoryStore, episode_id: int) -> int | None:
        if not hasattr(memory, "add_memory_job"):
            return None
        return safe_memory_call(memory.add_memory_job, "memory_review", episode_id=episode_id)

    def _claim_job(self, memory: MemoryStore, job_id: int | None) -> None:
        if job_id is not None and hasattr(memory, "claim_memory_job"):
            safe_memory_call(memory.claim_memory_job, job_id)

    def _complete_job(self, memory: MemoryStore, job_id: int | None) -> None:
        if job_id is not None and hasattr(memory, "complete_memory_job"):
            safe_memory_call(memory.complete_memory_job, job_id)

    def _defer_job(self, memory: MemoryStore, job_id: int | None, delay: float, error: str) -> None:
        if job_id is not None and hasattr(memory, "defer_memory_job"):
            safe_memory_call(memory.defer_memory_job, job_id, delay, error)

    def _fail_job(self, memory: MemoryStore, job_id: int | None, error: str) -> None:
        if job_id is not None and hasattr(memory, "fail_memory_job"):
            safe_memory_call(memory.fail_memory_job, job_id, error)


MemoryReviewWorker = MemoryBackgroundWorker
