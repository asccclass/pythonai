import tempfile
import unittest
from pathlib import Path

from memory import MemoryStore
from retrieval import build_memory_context, inject_memory_context


class RetrievalTests(unittest.TestCase):
    def test_build_memory_context_formats_active_semantic_memories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="I prefer Python")
            store.add_semantic_memory("user", "prefers_language", "Python", event_id, confidence=0.9)

            context = build_memory_context(store)

        self.assertIn("Relevant long-term memory:", context)
        self.assertIn("user prefers_language Python", context)
        self.assertIn("confidence=0.90", context)

    def test_build_memory_context_returns_empty_when_no_memory_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")

            context = build_memory_context(store)

        self.assertEqual(context, "")

    def test_inject_memory_context_appends_system_message(self):
        messages = [{"role": "user", "content": "hello"}]

        updated = inject_memory_context(messages, "Relevant long-term memory:\n- user prefers Python")

        self.assertEqual(messages, [{"role": "user", "content": "hello"}])
        self.assertEqual(updated[-1]["role"], "system")
        self.assertIn("prefers Python", updated[-1]["content"])

    def test_inject_memory_context_returns_original_when_empty(self):
        messages = [{"role": "user", "content": "hello"}]

        self.assertIs(inject_memory_context(messages, ""), messages)


if __name__ == "__main__":
    unittest.main()
