import tempfile
import unittest
from pathlib import Path

from memory import MemoryStore
from memory_review import process_memory_review_candidates


class MemoryReviewTests(unittest.TestCase):
    def test_process_semantic_review_candidate_creates_semantic_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            store.add_event(episode_id, "message", role="user", content="I prefer Python")
            store.add_event(
                episode_id,
                "memory_review_candidate",
                metadata={"memory_kind": "semantic", "confidence": 0.8},
            )

            results = process_memory_review_candidates(store, episode_id)
            memories = store.active_semantic_memories()

        self.assertEqual(results[0]["memory_kind"], "semantic")
        self.assertEqual(memories[0]["object"], "I prefer Python")

    def test_process_procedure_review_candidate_creates_procedure_from_tool_calls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            store.add_event(
                episode_id,
                "tool_call",
                metadata={"name": "run_command", "arguments": '{"command": ["python", "-m", "unittest"]}'},
            )
            store.add_event(
                episode_id,
                "memory_review_candidate",
                metadata={"memory_kind": "procedure", "confidence": 0.7},
            )

            results = process_memory_review_candidates(store, episode_id)
            procedures = store.active_procedures()

        self.assertEqual(results[0]["memory_kind"], "procedure")
        self.assertEqual(procedures[0]["task_type"], "run_command")
        self.assertIn("run_command", procedures[0]["steps"][0])

    def test_process_forgetting_review_candidate_records_policy_queue_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            store.add_event(
                episode_id,
                "memory_review_candidate",
                metadata={"memory_kind": "forgetting", "confidence": 0.6},
            )

            results = process_memory_review_candidates(store, episode_id)

        self.assertEqual(results[0], {"memory_kind": "forgetting", "status": "queued_for_policy"})


if __name__ == "__main__":
    unittest.main()
