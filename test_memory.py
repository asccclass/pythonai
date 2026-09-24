import tempfile
import unittest
from pathlib import Path

from laya_guard import GuardDecision
from memory import MemoryStore


class MemoryStoreTests(unittest.TestCase):
    def test_store_logs_episode_events(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()

            store.add_event(episode_id, "message", role="user", content="hello")
            store.add_event(
                episode_id,
                "guard_decision",
                metadata={"guard": GuardDecision(intent="chat", risk=0.1)},
            )
            store.finish_episode(episode_id)

            events = store.recent_events(limit=10)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event_type"], "guard_decision")
        self.assertEqual(events[0]["metadata"]["guard"]["intent"], "chat")
        self.assertEqual(events[1]["event_type"], "message")
        self.assertEqual(events[1]["role"], "user")
        self.assertEqual(events[1]["content"], "hello")

    def test_store_adds_and_queries_active_semantic_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="I prefer Python")

            memory_id = store.add_semantic_memory(
                subject="user",
                predicate="prefers_language",
                object_value="Python",
                source_event_id=event_id,
                confidence=0.9,
            )
            memories = store.active_semantic_memories(subject="user", predicate="prefers_language")

        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["id"], memory_id)
        self.assertEqual(memories[0]["object"], "Python")
        self.assertEqual(memories[0]["confidence"], 0.9)
        self.assertEqual(memories[0]["source_event_id"], event_id)

    def test_store_supersedes_semantic_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            old_event_id = store.add_event(episode_id, "message", role="user", content="I prefer Python")
            new_event_id = store.add_event(episode_id, "message", role="user", content="I prefer TypeScript")
            old_memory_id = store.add_semantic_memory(
                "user",
                "prefers_language",
                "Python",
                source_event_id=old_event_id,
                confidence=0.8,
            )

            new_memory_id = store.supersede_semantic_memory(
                old_memory_id,
                "user",
                "prefers_language",
                "TypeScript",
                source_event_id=new_event_id,
                confidence=0.9,
                reason="newer preference",
            )
            memories = store.active_semantic_memories(subject="user", predicate="prefers_language")

        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["id"], new_memory_id)
        self.assertEqual(memories[0]["object"], "TypeScript")

    def test_store_excludes_expired_semantic_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="temporary")

            store.add_semantic_memory(
                "user",
                "temporary_location",
                "Taipei",
                source_event_id=event_id,
                expires_at="2000-01-01 00:00:00",
            )
            memories = store.active_semantic_memories(subject="user")

        self.assertEqual(memories, [])

    def test_store_adds_and_queries_active_procedure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()

            procedure_id = store.add_procedure(
                task_type="run_tests",
                context_pattern="python project",
                steps=["python -m unittest", "python -m py_compile base.py"],
                source_episode_id=episode_id,
                confidence=0.8,
            )
            procedures = store.active_procedures(task_type="run_tests")

        self.assertEqual(len(procedures), 1)
        self.assertEqual(procedures[0]["id"], procedure_id)
        self.assertEqual(procedures[0]["steps"], ["python -m unittest", "python -m py_compile base.py"])
        self.assertEqual(procedures[0]["success_count"], 1)
        self.assertEqual(procedures[0]["failure_count"], 0)

    def test_store_records_procedure_results(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            procedure_id = store.add_procedure(
                "run_tests",
                "python project",
                ["python -m unittest"],
                source_episode_id=episode_id,
            )

            store.record_procedure_result(procedure_id, succeeded=True)
            store.record_procedure_result(procedure_id, succeeded=False)
            procedures = store.active_procedures(task_type="run_tests")

        self.assertEqual(procedures[0]["success_count"], 2)
        self.assertEqual(procedures[0]["failure_count"], 1)

    def test_store_excludes_archived_procedures(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            procedure_id = store.add_procedure(
                "run_tests",
                "python project",
                ["python -m unittest"],
                source_episode_id=episode_id,
            )

            store.archive_procedure(procedure_id, reason="obsolete")
            procedures = store.active_procedures(task_type="run_tests")

        self.assertEqual(procedures, [])


if __name__ == "__main__":
    unittest.main()
