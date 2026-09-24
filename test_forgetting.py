import tempfile
import unittest
from pathlib import Path

from forgetting import run_forgetting_policy
from memory import MemoryStore


class ForgettingTests(unittest.TestCase):
    def test_policy_archives_low_confidence_semantic_memories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="maybe")
            store.add_semantic_memory("user", "maybe_prefers", "Rust", event_id, confidence=0.1)

            result = run_forgetting_policy(store, min_semantic_confidence=0.2, confidence_decay=0.0)

            self.assertEqual(result["archived_semantic_memories"], 1)
            self.assertEqual(store.active_semantic_memories(), [])
            self.assertEqual(store.archived_semantic_memories()[0]["archive_reason"], "low_confidence")

    def test_policy_decays_semantic_confidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="I prefer Python")
            store.add_semantic_memory("user", "prefers", "Python", event_id, confidence=0.8)

            result = run_forgetting_policy(store, min_semantic_confidence=0.2, confidence_decay=0.1)

            self.assertEqual(result["decayed_semantic_memories"], 1)
            self.assertAlmostEqual(store.active_semantic_memories()[0]["confidence"], 0.7)

    def test_policy_reinforces_referenced_semantic_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="I prefer Python")
            memory_id = store.add_semantic_memory("user", "prefers", "Python", event_id, confidence=0.5)
            store.add_event(
                episode_id,
                "retrieval_context",
                metadata={"semantic_memory_ids": [memory_id]},
            )

            result = run_forgetting_policy(store, confidence_decay=0.1)

            self.assertEqual(result["reinforced_semantic_memories"], 1)
            self.assertAlmostEqual(store.active_semantic_memories()[0]["confidence"], 0.53)

    def test_policy_archives_expired_semantic_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="old")
            store.add_semantic_memory(
                "project",
                "temporary_fact",
                "old value",
                event_id,
                confidence=0.9,
                expires_at="2000-01-01 00:00:00",
            )

            result = run_forgetting_policy(store, confidence_decay=0.0)

            self.assertEqual(result["archived_expired_semantic_memories"], 1)
            self.assertEqual(store.archived_semantic_memories()[0]["archive_reason"], "expired")

    def test_policy_archives_contradicted_semantic_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="I live in Taipei")
            memory_id = store.add_semantic_memory("user", "lives_in", "Taipei", event_id, confidence=0.9)
            store.add_event(
                episode_id,
                "semantic_memory_contradiction",
                metadata={"semantic_memory_id": memory_id},
            )

            result = run_forgetting_policy(store, confidence_decay=0.0)

            self.assertEqual(result["archived_contradicted_semantic_memories"], 1)
            self.assertEqual(store.archived_semantic_memories()[0]["archive_reason"], "contradicted")

    def test_policy_archives_conflicting_lower_confidence_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            first_event = store.add_event(episode_id, "message", role="user", content="I prefer Python")
            second_event = store.add_event(episode_id, "message", role="user", content="I prefer TypeScript")
            store.add_semantic_memory("user", "prefers", "Python", first_event, confidence=0.4)
            store.add_semantic_memory("user", "prefers", "TypeScript", second_event, confidence=0.9)

            run_forgetting_policy(store, confidence_decay=0.0)

            active = store.active_semantic_memories()
            archived = store.archived_semantic_memories()

        self.assertEqual(active[0]["object"], "TypeScript")
        self.assertIn("conflicts_with", archived[0]["archive_reason"])

    def test_policy_archives_procedures_with_only_failures(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            procedure_id = store.add_procedure(
                "run_tests",
                "python",
                ["bad command"],
                source_episode_id=episode_id,
                success_count=0,
                failure_count=3,
            )

            result = run_forgetting_policy(store, max_procedure_failures=3)

            self.assertEqual(result["archived_procedures"], 1)
            self.assertEqual(store.active_procedures(), [])
            self.assertEqual(store.archived_procedures()[0]["id"], procedure_id)

    def test_policy_archives_procedure_with_high_failure_ratio(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            procedure_id = store.add_procedure(
                "deploy",
                "deploy",
                ["deploy"],
                source_episode_id=episode_id,
                success_count=1,
                failure_count=4,
            )

            result = run_forgetting_policy(store, max_procedure_failures=10)

            self.assertEqual(result["archived_procedures"], 1)
            self.assertEqual(store.archived_procedures()[0]["id"], procedure_id)
            self.assertIn("high_failure_ratio", store.archived_procedures()[0]["archive_reason"])


if __name__ == "__main__":
    unittest.main()
