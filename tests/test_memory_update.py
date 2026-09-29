import unittest

from memory_update import decide_semantic_update, reinforced_confidence
from semantic_extractor import SemanticTriple


class MemoryUpdateTests(unittest.TestCase):
    def test_decide_semantic_update_adds_new_fact(self):
        decision = decide_semantic_update(SemanticTriple("user", "prefers", "Python", 0.8), [], 0.8)

        self.assertEqual(decision.action, "add")
        self.assertEqual(decision.reason, "new_fact")

    def test_decide_semantic_update_reinforces_same_fact(self):
        decision = decide_semantic_update(
            SemanticTriple("user", "prefers", "Python", 0.8),
            [{"id": 7, "subject": "user", "predicate": "prefers", "object": "Python", "confidence": 0.5}],
            0.8,
        )

        self.assertEqual(decision.action, "reinforce")
        self.assertEqual(decision.existing_memory_id, 7)

    def test_decide_semantic_update_supersedes_changed_fact(self):
        decision = decide_semantic_update(
            SemanticTriple("user", "prefers", "TypeScript", 0.8),
            [{"id": 7, "subject": "user", "predicate": "prefers", "object": "Python", "confidence": 0.5}],
            0.8,
        )

        self.assertEqual(decision.action, "supersede")
        self.assertEqual(decision.existing_memory_id, 7)

    def test_decide_semantic_update_ignores_low_confidence(self):
        decision = decide_semantic_update(SemanticTriple("user", "maybe_prefers", "Rust", 0.1), [], 0.8)

        self.assertEqual(decision.action, "ignore")

    def test_reinforced_confidence_clamps_to_one(self):
        self.assertEqual(reinforced_confidence(0.98, 0.9), 1.0)


if __name__ == "__main__":
    unittest.main()
