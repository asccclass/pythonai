import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory import MemoryStore
from observability import inspect_episode, list_skills, main, memory_messages, memory_overview, skill_runs


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


if __name__ == "__main__":
    unittest.main()
