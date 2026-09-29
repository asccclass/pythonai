import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory import MemoryStore
from observability import (
    active_facts,
    archive_fact,
    confirm_fact,
    contradict_fact,
    export_memory,
    import_memory,
    import_memory_file,
    inspect_episode,
    list_skills,
    low_confidence_facts,
    main,
    memory_messages,
    memory_overview,
    semantic_conflicts,
    skill_runs,
    supersede_fact,
)


class ObservabilityTests(unittest.TestCase):
    def test_memory_overview_returns_all_sections(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            store.add_event(episode_id, "message", role="user", content="hello")

            overview = memory_overview(store)

        self.assertIn("recent_events", overview)
        self.assertIn("active_semantic_memories", overview)
        self.assertIn("archived_semantic_memories", overview)
        self.assertIn("active_procedures", overview)
        self.assertIn("archived_procedures", overview)
        self.assertIn("review_candidates", overview)

    def test_inspect_episode_returns_events(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            store.add_event(episode_id, "message", role="user", content="hello")

            result = inspect_episode(store, episode_id)

        self.assertEqual(result["episode_id"], episode_id)
        self.assertEqual(result["events"][0]["content"], "hello")

    def test_memory_messages_returns_only_message_events(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            store.add_event(episode_id, "message", role="user", content="hello")
            store.add_event(episode_id, "guard_decision", metadata={"intent": "chat"})
            store.add_event(episode_id, "message", role="assistant", content="hi")

            result = memory_messages(store, episode_id=episode_id)

        self.assertEqual(result["episode_id"], episode_id)
        self.assertEqual([message["content"] for message in result["messages"]], ["hi", "hello"])
        self.assertEqual({message["event_type"] for message in result["messages"]}, {"message"})

    def test_cli_overview_prints_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "memory.db"
            MemoryStore(db_path)
            with (
                patch("observability.MemoryStore", return_value=MemoryStore(db_path)),
                patch("sys.argv", ["observability.py", "overview"]),
                patch("builtins.print") as print_mock,
            ):
                main()

        self.assertIn("recent_events", print_mock.call_args.args[0])

    def test_cli_messages_prints_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "memory.db"
            store = MemoryStore(db_path)
            episode_id = store.start_episode()
            store.add_event(episode_id, "message", role="user", content="hello")
            with (
                patch("observability.MemoryStore", return_value=store),
                patch("sys.argv", ["observability.py", "messages", "--episode-id", str(episode_id), "--limit", "5"]),
                patch("builtins.print") as print_mock,
            ):
                main()

        printed = print_mock.call_args.args[0]
        self.assertIn('"messages"', printed)
        self.assertIn("hello", printed)

    def test_list_skills_returns_registry_skills(self):
        class Skill:
            def to_dict(self):
                return {"name": "example_skill"}

        class Registry:
            def list(self):
                return [Skill()]

        self.assertEqual(list_skills(Registry()), {"skills": [{"name": "example_skill"}]})

    def test_skill_runs_returns_skill_events(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            store.add_event(episode_id, "skill_result", metadata={"skill": "example_skill", "success": True})

            result = skill_runs(store)

        self.assertEqual(result["events"][0]["event_type"], "skill_result")

    def test_active_facts_filters_by_type_and_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="I prefer Python")
            store.add_semantic_memory(
                "user",
                "prefers",
                "Python",
                source_event_id=event_id,
                memory_type="user_profile",
                scope="global",
            )

            result = active_facts(store, memory_type="user_profile", scope="global")

        self.assertEqual(len(result["memories"]), 1)
        self.assertEqual(result["memories"][0]["object"], "Python")

    def test_low_confidence_facts_returns_threshold_matches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="maybe")
            store.add_semantic_memory("user", "maybe_prefers", "Rust", source_event_id=event_id, confidence=0.2)

            result = low_confidence_facts(store, threshold=0.3)

        self.assertEqual(result["memories"][0]["predicate"], "maybe_prefers")

    def test_semantic_conflicts_groups_different_objects(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            first_event_id = store.add_event(episode_id, "message", role="user", content="I prefer Python")
            second_event_id = store.add_event(episode_id, "message", role="user", content="I prefer TypeScript")
            store.add_semantic_memory("user", "prefers", "Python", source_event_id=first_event_id)
            store.add_semantic_memory("user", "prefers", "TypeScript", source_event_id=second_event_id)

            result = semantic_conflicts(store)

        self.assertEqual(result["conflicts"][0]["subject"], "user")
        self.assertEqual(len(result["conflicts"][0]["memories"]), 2)

    def test_archive_confirm_contradict_and_supersede_fact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="I prefer Python")
            memory_id = store.add_semantic_memory("user", "prefers", "Python", source_event_id=event_id)

            confirm_result = confirm_fact(store, memory_id)
            contradict_result = contradict_fact(store, memory_id)
            supersede_result = supersede_fact(
                store,
                memory_id,
                "user",
                "prefers",
                "TypeScript",
                memory_type="user_profile",
            )
            archive_result = archive_fact(store, supersede_result["new_memory_id"], reason="manual_test")
            events = store.recent_events(limit=10)
            archived = store.archived_semantic_memories()

        self.assertEqual(confirm_result["semantic_memory_id"], memory_id)
        self.assertEqual(contradict_result["semantic_memory_id"], memory_id)
        self.assertEqual(supersede_result["old_memory_id"], memory_id)
        self.assertEqual(archive_result["reason"], "manual_test")
        self.assertIn("semantic_memory_confirmation", [event["event_type"] for event in events])
        self.assertIn("semantic_memory_contradiction", [event["event_type"] for event in events])
        self.assertTrue(any(memory["archive_reason"] == "manual_test" for memory in archived))

    def test_export_memory_returns_memory_sections(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")

            result = export_memory(store)

        self.assertIn("active_semantic_memories", result)
        self.assertIn("active_procedures", result)

    def test_import_memory_imports_semantic_and_procedure_rows(self):
        payload = {
            "active_semantic_memories": [
                {
                    "subject": "user",
                    "predicate": "prefers",
                    "object": "Python",
                    "confidence": 0.9,
                    "memory_type": "user_profile",
                    "scope": "global",
                }
            ],
            "active_procedures": [
                {
                    "task_type": "run_command",
                    "context_pattern": "python tests",
                    "steps": ["run_command pytest"],
                    "confidence": 0.8,
                    "success_count": 2,
                    "failure_count": 1,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")

            result = import_memory(store, payload)
            memories = store.active_semantic_memories()
            procedures = store.active_procedures()
            events = store.episode_events(result["episode_id"])

        self.assertEqual(memories[0]["object"], "Python")
        self.assertEqual(memories[0]["source_event_id"], result["source_event_id"])
        self.assertEqual(procedures[0]["success_count"], 2)
        self.assertEqual(events[0]["event_type"], "memory_import")

    def test_import_memory_file_reads_json_payload(self):
        payload = {
            "active_semantic_memories": [
                {"subject": "project", "predicate": "uses", "object": "SQLite"}
            ],
            "active_procedures": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "memory-export.json"
            path.write_text(__import__("json").dumps(payload), encoding="utf-8")
            store = MemoryStore(Path(temp_dir) / "memory.db")

            result = import_memory_file(store, path)
            memories = store.active_semantic_memories()

        self.assertEqual(result["imported_semantic_memory_ids"], [memories[0]["id"]])
        self.assertEqual(memories[0]["object"], "SQLite")

    def test_cli_skill_runs_prints_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "memory.db"
            store = MemoryStore(db_path)
            episode_id = store.start_episode()
            store.add_event(episode_id, "skill_result", metadata={"skill": "example_skill", "success": True})
            with (
                patch("observability.MemoryStore", return_value=store),
                patch("sys.argv", ["observability.py", "skill-runs", "--limit", "5"]),
                patch("builtins.print") as print_mock,
            ):
                main()

        self.assertIn("skill_result", print_mock.call_args.args[0])

    def test_cli_facts_prints_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "memory.db"
            store = MemoryStore(db_path)
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="I prefer Python")
            store.add_semantic_memory(
                "user",
                "prefers",
                "Python",
                source_event_id=event_id,
                memory_type="user_profile",
            )
            with (
                patch("observability.MemoryStore", return_value=store),
                patch("sys.argv", ["observability.py", "facts", "--memory-type", "user_profile"]),
                patch("builtins.print") as print_mock,
            ):
                main()

        self.assertIn("Python", print_mock.call_args.args[0])

    def test_cli_import_prints_json(self):
        payload = {
            "active_semantic_memories": [
                {"subject": "user", "predicate": "prefers", "object": "Python"}
            ],
            "active_procedures": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "memory-export.json"
            path.write_text(__import__("json").dumps(payload), encoding="utf-8")
            store = MemoryStore(Path(temp_dir) / "memory.db")
            with (
                patch("observability.MemoryStore", return_value=store),
                patch("sys.argv", ["observability.py", "import", "--path", str(path)]),
                patch("builtins.print") as print_mock,
            ):
                main()

        self.assertIn("imported_semantic_memory_ids", print_mock.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
