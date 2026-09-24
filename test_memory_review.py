import tempfile
import unittest
from pathlib import Path

from memory import MemoryStore
from memory_review import extract_semantic_triple, process_memory_review_candidates


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
        self.assertEqual(memories[0]["subject"], "user")
        self.assertEqual(memories[0]["predicate"], "prefers")
        self.assertEqual(memories[0]["object"], "Python")

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

    def test_process_procedure_review_candidate_merges_similar_procedure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            first_episode = store.start_episode()
            store.add_procedure("run_command", "run_command", ["run_command {}"], first_episode, success_count=1)
            second_episode = store.start_episode()
            store.add_event(second_episode, "tool_call", metadata={"name": "run_command", "arguments": "{}"})
            store.add_event(second_episode, "memory_review_candidate", metadata={"memory_kind": "procedure", "confidence": 0.7})

            process_memory_review_candidates(store, second_episode)
            procedures = store.active_procedures("run_command")

        self.assertEqual(len(procedures), 1)
        self.assertEqual(procedures[0]["success_count"], 2)

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

    def test_extract_semantic_triple_prefers_known_patterns(self):
        self.assertEqual(extract_semantic_triple("I live in Taipei"), ("user", "lives_in", "Taipei"))
        self.assertEqual(extract_semantic_triple("my preferred language is TypeScript"), ("user", "preferred_language", "TypeScript"))


if __name__ == "__main__":
    unittest.main()
