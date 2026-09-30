import tempfile
import unittest
from pathlib import Path

import httpx
from openai import APIStatusError

from memory import MemoryStore
from memory_classifier import MemoryCandidateDecision
from memory_worker import MemoryBackgroundWorker, ProviderCooldown


class NoopClassifier:
    def assess_episode(self, events):
        return MemoryCandidateDecision(should_extract=False)


class NoopExtractor:
    allow_remote = None

    def extract(self, text):
        return []


class NoopMatcher:
    allow_remote = None

    def find_match(self, candidate, procedures, threshold=0.72):
        return None


class StaticEmbeddingProvider:
    def embed(self, text):
        return [1.0, 0.0]


class RateLimitedEmbeddingProvider:
    def embed(self, text):
        request = httpx.Request("POST", "https://example.test/v1/embeddings")
        response = httpx.Response(429, headers={"Retry-After": "3"}, request=request)
        raise APIStatusError("rate limited", response=response, body={})


class MemoryWorkerTests(unittest.TestCase):
    def test_worker_backfills_missing_embeddings_after_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="hello")
            memory_id = store.add_semantic_memory("user", "prefers", "Python", source_event_id=event_id)
            worker = MemoryBackgroundWorker(
                async_mode=False,
                embedding_provider=StaticEmbeddingProvider(),
                embedding_batch_size=5,
            )

            worker.enqueue(store, episode_id, NoopClassifier(), NoopExtractor(), NoopMatcher())
            memories = store.active_semantic_memories()
            jobs = store.memory_jobs(limit=5)

        self.assertEqual(memories[0]["id"], memory_id)
        self.assertIn("1.0", memories[0]["embedding"])
        self.assertIsNotNone(memories[0]["embedding_updated_at"])
        self.assertEqual(jobs[0]["status"], "completed")
        self.assertEqual(jobs[0]["attempts"], 1)

    def test_worker_records_provider_cooldown_after_rate_limit(self):
        slept = []
        now = [100.0]

        def clock():
            return now[0]

        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="hello")
            store.add_semantic_memory("user", "prefers", "Python", source_event_id=event_id)
            cooldown = ProviderCooldown(now=clock, sleep=slept.append)
            worker = MemoryBackgroundWorker(
                async_mode=False,
                embedding_provider=RateLimitedEmbeddingProvider(),
                embedding_batch_size=5,
                cooldown=cooldown,
            )

            worker.enqueue(store, episode_id, NoopClassifier(), NoopExtractor(), NoopMatcher())
            events = store.recent_events(limit=10)
            jobs = store.memory_jobs(limit=5)
            provider_state = store.provider_cooldown_state("default")

        cooldown_events = [event for event in events if event["event_type"] == "memory_background_cooldown"]
        self.assertEqual(len(cooldown_events), 1)
        self.assertEqual(cooldown_events[0]["metadata"]["retry_after_seconds"], 3.0)
        self.assertEqual(cooldown.available_at, 103.0)
        self.assertEqual(slept, [])
        self.assertEqual(jobs[0]["status"], "pending")
        self.assertEqual(jobs[0]["attempts"], 1)
        self.assertIn("rate limited", jobs[0]["last_error"])
        self.assertIsNotNone(jobs[0]["next_attempt_at"])
        self.assertEqual(provider_state["cooldown_available_at"], 103.0)
        self.assertEqual(provider_state["failures"], 1)

    def test_worker_enqueues_pending_review_jobs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            store.add_event(episode_id, "message", role="user", content="hello")
            job_id = store.add_memory_job("memory_review", episode_id=episode_id)
            worker = MemoryBackgroundWorker(async_mode=False)

            count = worker.enqueue_pending_reviews(store, NoopClassifier(), NoopExtractor(), NoopMatcher())
            jobs = store.memory_jobs(limit=5)

        self.assertEqual(count, 1)
        self.assertEqual(jobs[0]["id"], job_id)
        self.assertEqual(jobs[0]["status"], "completed")

    def test_worker_processes_embedding_backfill_jobs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="hello")
            memory_id = store.add_semantic_memory("user", "prefers", "Python", source_event_id=event_id)
            worker = MemoryBackgroundWorker(
                async_mode=False,
                embedding_provider=StaticEmbeddingProvider(),
                embedding_batch_size=5,
            )

            queued = worker.enqueue_embedding_backfills(store)
            memory = store.semantic_memory(memory_id)
            jobs = store.memory_jobs(limit=5)

        self.assertEqual(queued, 1)
        self.assertIn("1.0", memory["embedding"])
        self.assertEqual(jobs[0]["job_type"], "embedding_backfill")
        self.assertEqual(jobs[0]["status"], "completed")

    def test_worker_defers_embedding_backfill_job_after_rate_limit(self):
        now = [100.0]

        def clock():
            return now[0]

        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="hello")
            store.add_semantic_memory("user", "prefers", "Python", source_event_id=event_id)
            worker = MemoryBackgroundWorker(
                async_mode=False,
                embedding_provider=RateLimitedEmbeddingProvider(),
                embedding_batch_size=5,
                cooldown=ProviderCooldown(now=clock, sleep=lambda _: None),
            )

            queued = worker.enqueue_embedding_backfills(store)
            jobs = store.memory_jobs(limit=5)

        self.assertEqual(queued, 1)
        self.assertEqual(jobs[0]["job_type"], "embedding_backfill")
        self.assertEqual(jobs[0]["status"], "pending")
        self.assertEqual(jobs[0]["attempts"], 1)
        self.assertIn("rate limited", jobs[0]["last_error"])

    def test_worker_run_maintenance_enqueues_embedding_jobs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="hello")
            store.add_semantic_memory("user", "prefers", "Python", source_event_id=event_id)
            worker = MemoryBackgroundWorker(
                async_mode=False,
                embedding_provider=StaticEmbeddingProvider(),
                embedding_batch_size=5,
            )

            result = worker.run_maintenance(store)
            jobs = store.memory_jobs(limit=5)

        self.assertEqual(result["embedding_backfill"]["new_enqueued"], 1)
        self.assertEqual(jobs[0]["status"], "completed")

    def test_worker_respects_persisted_provider_cooldown(self):
        slept = []
        now = [100.0]

        def clock():
            return now[0]

        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            store.save_provider_cooldown_state("default", cooldown_available_at=105.0, failures=2)
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="hello")
            store.add_semantic_memory("user", "prefers", "Python", source_event_id=event_id)
            worker = MemoryBackgroundWorker(
                async_mode=False,
                embedding_provider=StaticEmbeddingProvider(),
                embedding_batch_size=5,
                cooldown=ProviderCooldown(now=clock, sleep=slept.append),
            )

            worker.enqueue_embedding_backfills(store)
            provider_state = store.provider_cooldown_state("default")

        self.assertEqual(slept, [5.0])
        self.assertEqual(provider_state["failures"], 0)


if __name__ == "__main__":
    unittest.main()
