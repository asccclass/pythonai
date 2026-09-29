import tempfile
import unittest
from pathlib import Path
import sqlite3

from laya_guard import GuardDecision
from memory import DEFAULT_MEMORY_DB, MemoryStore


class MemoryStoreTests(unittest.TestCase):
    def test_default_memory_db_lives_under_memory_directory(self):
        self.assertEqual(DEFAULT_MEMORY_DB.name, "memory.db")
        self.assertEqual(DEFAULT_MEMORY_DB.parent.name, "memory")

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

    def test_store_returns_events_for_episode_in_insert_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            store.add_event(episode_id, "message", role="user", content="one")
            store.add_event(episode_id, "message", role="assistant", content="two")

            events = store.episode_events(episode_id)

        self.assertEqual([event["content"] for event in events], ["one", "two"])

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
        self.assertEqual(memories[0]["memory_type"], "fact")
        self.assertEqual(memories[0]["scope"], "global")
        self.assertIsNone(memories[0]["embedding_updated_at"])

    def test_store_adds_and_filters_typed_semantic_memory(self):
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
                memory_type="user_profile",
                scope="global",
            )
            profile_memories = store.active_semantic_memories(memory_type="user_profile", scope="global")
            project_memories = store.active_semantic_memories(memory_type="project_fact")

        self.assertEqual([memory["id"] for memory in profile_memories], [memory_id])
        self.assertEqual(profile_memories[0]["memory_type"], "user_profile")
        self.assertEqual(profile_memories[0]["scope"], "global")
        self.assertEqual(project_memories, [])

    def test_store_creates_and_finds_entity_aliases(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")

            entity_id = store.get_or_create_entity("PythonAI", entity_type="project", aliases=["python ai"])
            same_entity_id = store.get_or_create_entity("pythonai", entity_type="project")
            entity = store.find_entity("python ai", entity_type="project")
            aliases = store.entity_aliases(entity_id)

        self.assertEqual(same_entity_id, entity_id)
        self.assertEqual(entity["id"], entity_id)
        self.assertIn("python ai", {alias["normalized_alias"] for alias in aliases})

    def test_store_links_semantic_memory_to_entities(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="Project uses SQLite")

            memory_id = store.add_semantic_memory(
                subject="project",
                predicate="uses",
                object_value="SQLite",
                source_event_id=event_id,
                memory_type="project_fact",
                scope="pythonai",
            )
            memory = store.active_semantic_memories()[0]
            project = store.find_entity("project", entity_type="project")
            sqlite = store.find_entity("sqlite")

        self.assertEqual(memory["id"], memory_id)
        self.assertEqual(memory["subject_entity_id"], project["id"])
        self.assertEqual(memory["object_entity_id"], sqlite["id"])

    def test_store_migrates_existing_semantic_memory_type_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "memory.db"
            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE semantic_memories (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        subject TEXT NOT NULL,
                        predicate TEXT NOT NULL,
                        object TEXT NOT NULL,
                        confidence REAL NOT NULL DEFAULT 0.5,
                        source_event_id INTEGER NOT NULL,
                        embedding TEXT,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        expires_at TEXT,
                        superseded_by INTEGER,
                        archived_at TEXT,
                        archive_reason TEXT
                    )
                    """
                )
                connection.commit()
            finally:
                connection.close()

            store = MemoryStore(db_path)
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="hello")
            store.add_semantic_memory("user", "prefers", "Python", source_event_id=event_id)
            memories = store.active_semantic_memories()

        self.assertEqual(memories[0]["memory_type"], "fact")
        self.assertEqual(memories[0]["scope"], "global")

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
                memory_type="user_profile",
                scope="global",
            )
            memories = store.active_semantic_memories(subject="user", predicate="prefers_language")

        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["id"], new_memory_id)
        self.assertEqual(memories[0]["object"], "TypeScript")
        self.assertEqual(memories[0]["memory_type"], "user_profile")

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

    def test_store_returns_expired_semantic_memories_for_lifecycle_policy(self):
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
            memories = store.expired_semantic_memories()

        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["predicate"], "temporary_location")

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

    def test_store_saves_and_updates_semantic_embedding(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="hello")
            memory_id = store.add_semantic_memory(
                "user", "prefers", "Python", source_event_id=event_id, embedding=[0.1, 0.2]
            )

            memories = store.active_semantic_memories()
            self.assertEqual(memories[0]["id"], memory_id)
            self.assertIn("0.1", memories[0]["embedding"])
            self.assertIsNotNone(memories[0]["embedding_updated_at"])

            store.update_semantic_embedding(memory_id, [0.3, 0.4])
            updated_memories = store.active_semantic_memories()
            self.assertIn("0.3", updated_memories[0]["embedding"])
            self.assertIsNotNone(updated_memories[0]["embedding_updated_at"])

    def test_store_leaves_embedding_timestamp_empty_without_embedding(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="hello")
            store.add_semantic_memory("user", "prefers", "Python", source_event_id=event_id)

            memories = store.active_semantic_memories()

        self.assertIsNone(memories[0]["embedding"])
        self.assertIsNone(memories[0]["embedding_updated_at"])


if __name__ == "__main__":
    unittest.main()
