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

            result = run_forgetting_policy(store, min_semantic_confidence=0.2)

            self.assertEqual(result["archived_semantic_memories"], 1)
            self.assertEqual(store.active_semantic_memories(), [])
            self.assertEqual(store.archived_semantic_memories()[0]["archive_reason"], "low_confidence")

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


if __name__ == "__main__":
    unittest.main()
