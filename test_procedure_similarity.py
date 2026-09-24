import unittest

from procedure_similarity import (
    LLMProcedureSimilarityMatcher,
    LexicalProcedureSimilarityMatcher,
    ProcedureCandidate,
    parse_procedure_match,
)


class _Message:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Message(content)


class _Response:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class _Completions:
    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error

    def create(self, **kwargs):
        if self.error is not None:
            raise self.error
        return _Response(self.content)


class _Chat:
    def __init__(self, completions):
        self.completions = completions


class _Client:
    def __init__(self, content=None, error=None):
        self.chat = _Chat(_Completions(content, error))


class ProcedureSimilarityTests(unittest.TestCase):
    def test_parse_procedure_match_accepts_json(self):
        match = parse_procedure_match('{"best_id": 7, "score": 0.84, "reason": "same workflow"}')

        self.assertEqual(match.procedure_id, 7)
        self.assertEqual(match.score, 0.84)
        self.assertEqual(match.reason, "same workflow")

    def test_llm_matcher_returns_semantic_match(self):
        matcher = LLMProcedureSimilarityMatcher(
            lambda: _Client('{"best_id": 3, "score": 0.91, "reason": "same test workflow"}'),
            "test-model",
        )

        match = matcher.find_match(
            ProcedureCandidate("run_command", "run_command", ["run_command pytest"]),
            [
                {
                    "id": 3,
                    "task_type": "run_command",
                    "context_pattern": "run_command",
                    "steps": ["run_command python -m unittest"],
                    "confidence": 0.8,
                    "success_count": 2,
                    "failure_count": 0,
                }
            ],
        )

        self.assertEqual(match.procedure_id, 3)
        self.assertEqual(match.score, 0.91)

    def test_llm_matcher_falls_back_when_llm_fails(self):
        matcher = LLMProcedureSimilarityMatcher(lambda: _Client(error=RuntimeError("boom")), "test-model")

        match = matcher.find_match(
            ProcedureCandidate("run_command", "run_command", ["run_command python -m unittest"]),
            [
                {
                    "id": 4,
                    "task_type": "run_command",
                    "context_pattern": "run_command",
                    "steps": ["run_command python -m unittest"],
                    "confidence": 0.8,
                    "success_count": 1,
                    "failure_count": 0,
                }
            ],
        )

        self.assertEqual(match.procedure_id, 4)

    def test_lexical_matcher_rejects_low_similarity(self):
        matcher = LexicalProcedureSimilarityMatcher()

        match = matcher.find_match(
            ProcedureCandidate("write_file", "write_file", ["write_file hello.txt"]),
            [
                {
                    "id": 8,
                    "task_type": "run_command",
                    "context_pattern": "run_command",
                    "steps": ["run_command python -m unittest"],
                    "confidence": 0.8,
                    "success_count": 1,
                    "failure_count": 0,
                }
            ],
        )

        self.assertIsNone(match)


if __name__ == "__main__":
    unittest.main()
