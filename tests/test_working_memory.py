import unittest

from working_memory import PreservationDecision, compact_messages, normalize_content, summarize_messages


class WorkingMemoryTests(unittest.TestCase):
    def test_compact_messages_keeps_system_summary_and_recent_messages(self):
        messages = [{"role": "system", "content": "system"}]
        for index in range(1, 8):
            messages.append({"role": "user", "content": f"message {index}"})

        compacted, summary, preservation = compact_messages(messages, max_messages=5, keep_recent=3)

        self.assertIsNotNone(summary)
        self.assertEqual(preservation, PreservationDecision())
        self.assertEqual(compacted[0], {"role": "system", "content": "system"})
        self.assertEqual(compacted[1]["role"], "system")
        self.assertIn("Earlier conversation summary", compacted[1]["content"])
        self.assertIn("message 1", summary)
        self.assertEqual([message["content"] for message in compacted[-3:]], ["message 5", "message 6", "message 7"])

    def test_compact_messages_returns_original_when_under_limit(self):
        messages = [{"role": "user", "content": "hello"}]

        compacted, summary, preservation = compact_messages(messages, max_messages=5)

        self.assertIs(compacted, messages)
        self.assertIsNone(summary)
        self.assertIsNone(preservation)

    def test_compact_messages_uses_preservation_classifier(self):
        class Classifier:
            def predict(self, state, questions):
                return {
                    "answers": {
                        "should_preserve": {"noul": True},
                        "preservation_kind": {"choice": "semantic"},
                    }
                }

        messages = [{"role": "system", "content": "system"}]
        for index in range(1, 8):
            messages.append({"role": "user", "content": f"message {index}"})

        compacted, summary, preservation = compact_messages(
            messages,
            max_messages=5,
            keep_recent=3,
            preservation_classifier=Classifier(),
        )

        self.assertTrue(preservation.should_preserve)
        self.assertEqual(preservation.preservation_kind, "semantic")

    def test_summarize_messages_includes_roles(self):
        summary = summarize_messages(
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ]
        )

        self.assertIn("1. user: hello", summary)
        self.assertIn("2. assistant: hi", summary)

    def test_normalize_content_truncates_long_text(self):
        text = normalize_content("a" * 300, max_length=20)

        self.assertEqual(len(text), 20)
        self.assertTrue(text.endswith("..."))


if __name__ == "__main__":
    unittest.main()
