import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from laya_guard import LayaGuard


class LayaGuardTests(unittest.TestCase):
    def test_guard_falls_back_when_laya_is_unavailable(self):
        with patch.dict(sys.modules, {"laya": None}):
            guard = LayaGuard()

        decision = guard.assess("delete all files")

        self.assertFalse(decision.available)
        self.assertIn("Laya unavailable", decision.reason)

    def test_guard_maps_laya_prediction_to_decision(self):
        class Agent:
            def predict(self, state, questions):
                return {
                    "answers": {
                        "intent": {"choice": "run_command"},
                        "risk": {"score": 1.8},
                        "needs_confirmation": {"noul": True},
                    }
                }

        with tempfile.TemporaryDirectory() as model_dir:
            fake_laya = types.SimpleNamespace(load=lambda path: Agent())

            with patch.dict(sys.modules, {"laya": fake_laya}):
                guard = LayaGuard(model_dir=model_dir)

        decision = guard.assess("run rm -rf")

        self.assertTrue(decision.available)
        self.assertEqual(decision.intent, "run_command")
        self.assertEqual(decision.risk, 1.8)
        self.assertTrue(decision.needs_confirmation)

    def test_guard_maps_delete_file_intent(self):
        class Agent:
            def predict(self, state, questions):
                return {
                    "answers": {
                        "intent": {"choice": "delete_file"},
                        "risk": {"score": 1.7},
                        "needs_confirmation": {"noul": True},
                    }
                }

        with tempfile.TemporaryDirectory() as model_dir:
            fake_laya = types.SimpleNamespace(load=lambda path: Agent())

            with patch.dict(sys.modules, {"laya": fake_laya}):
                guard = LayaGuard(model_dir=model_dir)

        decision = guard.assess("delete hello.txt")

        self.assertEqual(decision.intent, "delete_file")
        self.assertEqual(decision.risk, 1.7)
        self.assertTrue(decision.needs_confirmation)

    def test_guard_falls_back_when_prediction_fails(self):
        class Agent:
            def predict(self, state, questions):
                raise RuntimeError("model error")

        with tempfile.TemporaryDirectory() as model_dir:
            fake_laya = types.SimpleNamespace(load=lambda path: Agent())

            with patch.dict(sys.modules, {"laya": fake_laya}):
                guard = LayaGuard(model_dir=model_dir)

        decision = guard.assess("hello")

        self.assertFalse(decision.available)
        self.assertIn("prediction failed", decision.reason)

    def test_guard_falls_back_when_model_dir_is_missing(self):
        missing_dir = Path("missing-laya-model")
        fake_laya = types.SimpleNamespace(load=lambda path: object())

        with patch.dict(sys.modules, {"laya": fake_laya}):
            guard = LayaGuard(model_dir=missing_dir)

        decision = guard.assess("hello")

        self.assertFalse(decision.available)
        self.assertIn("model directory not found", decision.reason)


if __name__ == "__main__":
    unittest.main()
