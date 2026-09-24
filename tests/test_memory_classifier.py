import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from memory_classifier import LayaMemoryClassifier, format_episode_for_laya


class MemoryClassifierTests(unittest.TestCase):
    def test_classifier_maps_laya_prediction_to_candidate_decision(self):
        class Agent:
            def predict(self, state, questions):
                return {
                    "answers": {
                        "memory_kind": {"choice": "semantic"},
                        "should_extract": {"noul": True},
                        "confidence": {"score": 0.9},
                    }
                }

        with tempfile.TemporaryDirectory() as model_dir:
            fake_laya = types.SimpleNamespace(load=lambda path: Agent())
            with patch.dict(sys.modules, {"laya": fake_laya}):
                classifier = LayaMemoryClassifier(model_dir=model_dir)

        decision = classifier.assess_episode([{"event_type": "message", "role": "user", "content": "I prefer Python"}])

        self.assertTrue(decision.available)
        self.assertEqual(decision.memory_kind, "semantic")
        self.assertTrue(decision.should_extract)
        self.assertEqual(decision.confidence, 0.9)

    def test_classifier_falls_back_when_laya_is_unavailable(self):
        with patch.dict(sys.modules, {"laya": None}):
            classifier = LayaMemoryClassifier()

        decision = classifier.assess_episode([])

        self.assertFalse(decision.available)
        self.assertIn("unavailable", decision.reason)

    def test_format_episode_for_laya_includes_event_metadata(self):
        text = format_episode_for_laya(
            [
                {
                    "event_type": "tool_call",
                    "role": None,
                    "content": "",
                    "metadata": {"name": "run_command"},
                }
            ]
        )

        self.assertIn("tool_call", text)
        self.assertIn("run_command", text)


if __name__ == "__main__":
    unittest.main()
