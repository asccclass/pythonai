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


if __name__ == "__main__":
    unittest.main()
