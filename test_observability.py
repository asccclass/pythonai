import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory import MemoryStore
from observability import inspect_episode, main, memory_overview


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


if __name__ == "__main__":
    unittest.main()
