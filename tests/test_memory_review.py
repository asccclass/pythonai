import tempfile
import unittest
from pathlib import Path

from memory import MemoryStore
from memory_review import extract_semantic_triple, process_memory_review_candidates
from procedure_similarity import ProcedureMatch
from semantic_extractor import SemanticTriple


class FakeSemanticExtractor:
    def extract(self, text: str) -> list[SemanticTriple]:
        return [
            SemanticTriple("user", "prefers", "TypeScript", 0.9),
            SemanticTriple("project", "uses", "SQLite", 0.7),
        ]


class DuplicateSemanticExtractor:
    def extract(self, text: str) -> list[SemanticTriple]:
        return [SemanticTriple("user", "prefers", "Python", 0.9)]


class TypeScriptPreferenceExtractor:
    def extract(self, text: str) -> list[SemanticTriple]:
        return [SemanticTriple("user", "prefers", "TypeScript", 0.9, "user_profile", "global")]


class LowConfidenceExtractor:
    def extract(self, text: str) -> list[SemanticTriple]:
        return [SemanticTriple("user", "maybe_prefers", "Rust", 0.1, "user_profile", "global")]


class FakeProcedureMatcher:
    def __init__(self, procedure_id: int):
        self.procedure_id = procedure_id
        self.seen_candidates = []

    def find_match(self, candidate, procedures, threshold=0.72):
        self.seen_candidates.append((candidate, procedures, threshold))
        return ProcedureMatch(self.procedure_id, 0.88, "semantic_test_match")


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

    def test_process_semantic_review_candidate_uses_injected_extractor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            store.add_event(episode_id, "message", role="user", content="Remember multiple facts.")
            store.add_event(
                episode_id,
                "memory_review_candidate",
                metadata={"memory_kind": "semantic", "confidence": 0.8},
            )

            results = process_memory_review_candidates(store, episode_id, semantic_extractor=FakeSemanticExtractor())
            memories = store.active_semantic_memories()
            review_results = [
                event for event in store.episode_events(episode_id) if event["event_type"] == "memory_review_result"
            ]

        self.assertEqual(results[0]["memory_kind"], "semantic")
        self.assertEqual(len(results[0]["semantic_memory_ids"]), 2)
        self.assertEqual(len(memories), 2)
        self.assertEqual({memory["predicate"] for memory in memories}, {"prefers", "uses"})
        self.assertEqual(review_results[0]["metadata"]["semantic_memory_ids"], results[0]["semantic_memory_ids"])

    def test_process_working_memory_preservation_candidate_extracts_from_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            store.add_event(episode_id, "message", role="user", content="Do not extract this.")
            summary_event_id = store.add_event(
                episode_id,
                "working_memory_summary",
                content="The user prefers Python for automation tasks.",
            )
            store.add_event(
                episode_id,
                "working_memory_preservation_candidate",
                metadata={
                    "preservation": {
                        "should_preserve": True,
                        "preservation_kind": "semantic",
                    }
                },
            )

            results = process_memory_review_candidates(store, episode_id)
            memories = store.active_semantic_memories()

        self.assertEqual(results[0]["memory_kind"], "semantic")
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["source_event_id"], summary_event_id)
        self.assertIn("Python", memories[0]["object"])

    def test_process_working_memory_preservation_candidate_with_both_does_not_create_procedure_without_tools(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            store.add_event(episode_id, "working_memory_summary", content="I live in Taipei.")
            store.add_event(
                episode_id,
                "working_memory_preservation_candidate",
                metadata={
                    "preservation": {
                        "should_preserve": True,
                        "preservation_kind": "both",
                    }
                },
            )

            results = process_memory_review_candidates(store, episode_id)
            memories = store.active_semantic_memories()
            procedures = store.active_procedures()

        self.assertEqual([result["memory_kind"] for result in results], ["semantic"])
        self.assertEqual(len(memories), 1)
        self.assertEqual(procedures, [])

    def test_process_semantic_candidates_deduplicates_episode_facts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            store.add_event(episode_id, "message", role="user", content="I prefer Python.")
            store.add_event(
                episode_id,
                "memory_review_candidate",
                metadata={"memory_kind": "semantic", "confidence": 0.8},
            )
            store.add_event(
                episode_id,
                "working_memory_summary",
                content="The user prefers Python.",
            )
            store.add_event(
                episode_id,
                "working_memory_preservation_candidate",
                metadata={
                    "preservation": {
                        "should_preserve": True,
                        "preservation_kind": "semantic",
                    }
                },
            )

            results = process_memory_review_candidates(
                store,
                episode_id,
                semantic_extractor=DuplicateSemanticExtractor(),
            )
            memories = store.active_semantic_memories()

        self.assertEqual(len(results), 2)
        self.assertEqual(len(memories), 1)
        self.assertEqual(results[0]["created_semantic_memory_ids"], [memories[0]["id"]])
        self.assertEqual(results[1]["deduplicated_semantic_memory_ids"], [memories[0]["id"]])

    def test_process_semantic_candidate_reinforces_existing_fact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            first_episode = store.start_episode()
            first_event_id = store.add_event(first_episode, "message", role="user", content="I prefer Python")
            memory_id = store.add_semantic_memory(
                "user",
                "prefers",
                "Python",
                source_event_id=first_event_id,
                confidence=0.4,
                memory_type="user_profile",
            )
            second_episode = store.start_episode()
            store.add_event(second_episode, "message", role="user", content="I prefer Python")
            store.add_event(second_episode, "memory_review_candidate", metadata={"memory_kind": "semantic", "confidence": 0.8})

            results = process_memory_review_candidates(
                store,
                second_episode,
                semantic_extractor=DuplicateSemanticExtractor(),
            )
            memories = store.active_semantic_memories()
            update_events = [
                event for event in store.episode_events(second_episode) if event["event_type"] == "memory_update_decision"
            ]

        self.assertEqual(results[0]["reinforced_semantic_memory_ids"], [memory_id])
        self.assertEqual(len(memories), 1)
        self.assertGreater(memories[0]["confidence"], 0.8)
        self.assertEqual(update_events[0]["metadata"]["action"], "reinforce")

    def test_process_semantic_candidate_supersedes_changed_fact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            first_episode = store.start_episode()
            first_event_id = store.add_event(first_episode, "message", role="user", content="I prefer Python")
            old_memory_id = store.add_semantic_memory(
                "user",
                "prefers",
                "Python",
                source_event_id=first_event_id,
                confidence=0.7,
                memory_type="user_profile",
            )
            second_episode = store.start_episode()
            store.add_event(second_episode, "message", role="user", content="I prefer TypeScript")
            store.add_event(second_episode, "memory_review_candidate", metadata={"memory_kind": "semantic", "confidence": 0.8})

            results = process_memory_review_candidates(
                store,
                second_episode,
                semantic_extractor=TypeScriptPreferenceExtractor(),
            )
            active = store.active_semantic_memories(subject="user", predicate="prefers")
            archived = store.archived_semantic_memories()

        self.assertEqual(results[0]["superseded_semantic_memory_ids"], [old_memory_id])
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["object"], "TypeScript")
        self.assertEqual(archived[0]["id"], old_memory_id)

    def test_process_semantic_candidate_ignores_low_confidence_fact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            store.add_event(episode_id, "message", role="user", content="Maybe Rust")
            store.add_event(episode_id, "memory_review_candidate", metadata={"memory_kind": "semantic", "confidence": 0.8})

            results = process_memory_review_candidates(
                store,
                episode_id,
                semantic_extractor=LowConfidenceExtractor(),
            )
            memories = store.active_semantic_memories()
            update_events = [
                event for event in store.episode_events(episode_id) if event["event_type"] == "memory_update_decision"
            ]

        self.assertEqual(results, [])
        self.assertEqual(memories, [])
        self.assertEqual(update_events[0]["metadata"]["action"], "ignore")

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
            procedure_id = store.add_procedure("run_command", "old context", ["run_command old args"], first_episode, success_count=1)
            second_episode = store.start_episode()
            matcher = FakeProcedureMatcher(procedure_id)
            store.add_event(second_episode, "tool_call", metadata={"name": "run_command", "arguments": '{"command": ["pytest"]}'})
            store.add_event(second_episode, "memory_review_candidate", metadata={"memory_kind": "procedure", "confidence": 0.7})

            result = process_memory_review_candidates(store, second_episode, procedure_matcher=matcher)
            procedures = store.active_procedures("run_command")

        self.assertEqual(len(procedures), 1)
        self.assertEqual(procedures[0]["success_count"], 2)
        self.assertEqual(result[0]["action"], "merged")
        self.assertEqual(matcher.seen_candidates[0][0].task_type, "run_command")

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
