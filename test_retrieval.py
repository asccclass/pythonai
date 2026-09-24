import tempfile
import unittest
from pathlib import Path

from memory import MemoryStore
from retrieval import build_memory_context, inject_memory_context, rank_memories, tokenize


class FakeVectorSearcher:
    def __init__(self, selected_ids):
        self.selected_ids = selected_ids
        self.calls = []

    def search(self, query, memories, limit):
        self.calls.append((query, memories, limit))
        selected = set(self.selected_ids)
        return [memory for memory in memories if memory["id"] in selected][:limit]


class FakeBudgetVectorSearcher:
    def __init__(self):
        self.calls = []

    def search_with_budget(self, query, memories, limit, *, allow_query_embedding=True, max_missing_embeddings=None):
        self.calls.append(
            {
                "query": query,
                "memories": memories,
                "limit": limit,
                "allow_query_embedding": allow_query_embedding,
                "max_missing_embeddings": max_missing_embeddings,
            }
        )
        return memories[:limit]


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

    def test_build_memory_context_uses_vector_search(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            python_event_id = store.add_event(episode_id, "message", role="user", content="I prefer Python")
            location_event_id = store.add_event(episode_id, "message", role="user", content="I live in Taipei")
            python_id = store.add_semantic_memory("user", "prefers_language", "Python", python_event_id, confidence=0.7)
            store.add_semantic_memory("user", "lives_in", "Taipei", location_event_id, confidence=1.0)
            searcher = FakeVectorSearcher([python_id])

            context = build_memory_context(store, query="Please help with Python tests", vector_searcher=searcher)

        self.assertIn("user prefers_language Python", context)
        self.assertNotIn("Taipei", context)
        self.assertEqual(searcher.calls[0][0], "Please help with Python tests")

    def test_build_memory_context_does_not_require_lexical_overlap_before_vector_search(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="I like TypeScript")
            memory_id = store.add_semantic_memory("user", "prefers_language", "TypeScript", event_id, confidence=0.9)
            searcher = FakeVectorSearcher([memory_id])

            context = build_memory_context(store, query="frontend coding", vector_searcher=searcher)

        self.assertIn("TypeScript", context)

    def test_build_memory_context_returns_empty_when_query_has_no_relevant_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="I live in Taipei")
            store.add_semantic_memory("user", "lives_in", "Taipei", event_id, confidence=1.0)

            context = build_memory_context(store, query="Python test command")

        self.assertEqual(context, "")

    def test_build_memory_context_returns_empty_when_no_memory_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")

            context = build_memory_context(store)

        self.assertEqual(context, "")

    def test_build_memory_context_passes_embedding_budget_to_vector_search(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            event_id = store.add_event(episode_id, "message", role="user", content="I prefer Python")
            store.add_semantic_memory("user", "prefers_language", "Python", event_id, confidence=0.9)
            searcher = FakeBudgetVectorSearcher()

            context = build_memory_context(
                store,
                query="Python",
                vector_searcher=searcher,
                allow_query_embedding=False,
                max_missing_embeddings=0,
            )

        self.assertIn("Python", context)
        self.assertFalse(searcher.calls[0]["allow_query_embedding"])
        self.assertEqual(searcher.calls[0]["max_missing_embeddings"], 0)

    def test_inject_memory_context_appends_system_message(self):
        messages = [{"role": "user", "content": "hello"}]

        updated = inject_memory_context(messages, "Relevant long-term memory:\n- user prefers Python")

        self.assertEqual(messages, [{"role": "user", "content": "hello"}])
        self.assertEqual(updated[-1]["role"], "system")
        self.assertIn("prefers Python", updated[-1]["content"])

    def test_inject_memory_context_returns_original_when_empty(self):
        messages = [{"role": "user", "content": "hello"}]

        self.assertIs(inject_memory_context(messages, ""), messages)

    def test_rank_memories_without_query_orders_by_confidence(self):
        memories = [
            {"id": 1, "subject": "user", "predicate": "likes", "object": "Python", "confidence": 0.5, "updated_at": "2026-01-01"},
            {"id": 2, "subject": "user", "predicate": "likes", "object": "TypeScript", "confidence": 0.9, "updated_at": "2026-01-01"},
        ]

        ranked = rank_memories(memories)

        self.assertEqual([memory["id"] for memory in ranked], [2, 1])

    def test_tokenize_returns_lowercase_terms(self):
        self.assertEqual(tokenize("Python tests, please"), {"python", "tests", "please"})


if __name__ == "__main__":
    unittest.main()
