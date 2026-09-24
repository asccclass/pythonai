import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from retrieval_ranker import LayaMemoryRanker, format_memory_for_laya


class RetrievalRankerTests(unittest.TestCase):
    def test_ranker_orders_memories_by_laya_relevance(self):
        class Agent:
            def predict(self, state, questions):
                score = 0.9 if "Python" in state["memory"] else 0.2
                return {"answers": {"relevance": {"score": score}}}

        memories = [
            {"id": 1, "subject": "user", "predicate": "lives_in", "object": "Taipei", "confidence": 1.0},
            {"id": 2, "subject": "user", "predicate": "prefers_language", "object": "Python", "confidence": 0.5},
        ]

        with tempfile.TemporaryDirectory() as model_dir:
            fake_laya = types.SimpleNamespace(load=lambda path: Agent())
            with patch.dict(sys.modules, {"laya": fake_laya}):
                ranker = LayaMemoryRanker(model_dir=model_dir)

        ranked = ranker.rank("Python help", memories)

        self.assertEqual([memory["id"] for memory in ranked], [2, 1])

    def test_ranker_returns_original_order_when_laya_is_unavailable(self):
        memories = [{"id": 1, "subject": "user", "predicate": "likes", "object": "Python", "confidence": 1.0}]

        with patch.dict(sys.modules, {"laya": None}):
            ranker = LayaMemoryRanker()

        self.assertIs(ranker.rank("Python", memories), memories)

    def test_ranker_returns_original_order_when_prediction_fails(self):
        class Agent:
            def predict(self, state, questions):
                raise RuntimeError("rank failed")

        memories = [{"id": 1, "subject": "user", "predicate": "likes", "object": "Python", "confidence": 1.0}]

        with tempfile.TemporaryDirectory() as model_dir:
            fake_laya = types.SimpleNamespace(load=lambda path: Agent())
            with patch.dict(sys.modules, {"laya": fake_laya}):
                ranker = LayaMemoryRanker(model_dir=model_dir)

        self.assertEqual(ranker.rank("Python", memories), memories)

    def test_format_memory_for_laya(self):
        text = format_memory_for_laya({"subject": "user", "predicate": "likes", "object": "Python"})

        self.assertEqual(text, "user likes Python")


if __name__ == "__main__":
    unittest.main()
