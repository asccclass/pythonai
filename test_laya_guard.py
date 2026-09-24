import sys
import types
import unittest
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
        class Router:
            def __init__(self, preload=False):
                self.preload = preload

            def predict(self, state, questions):
                return {
                    "answers": {
                        "intent": {"choice": "run_command"},
                        "risk": {"score": 1.8},
                        "needs_confirmation": {"noul": True},
                    }
                }

        fake_laya = types.SimpleNamespace(Router=Router)

        with patch.dict(sys.modules, {"laya": fake_laya}):
            guard = LayaGuard(preload=True)

        decision = guard.assess("run rm -rf")

        self.assertTrue(decision.available)
        self.assertEqual(decision.intent, "run_command")
        self.assertEqual(decision.risk, 1.8)
        self.assertTrue(decision.needs_confirmation)

    def test_guard_falls_back_when_prediction_fails(self):
        class Router:
            def __init__(self, preload=False):
                pass

            def predict(self, state, questions):
                raise RuntimeError("model error")

        fake_laya = types.SimpleNamespace(Router=Router)

        with patch.dict(sys.modules, {"laya": fake_laya}):
            guard = LayaGuard()

        decision = guard.assess("hello")

        self.assertFalse(decision.available)
        self.assertIn("prediction failed", decision.reason)


if __name__ == "__main__":
    unittest.main()
