import unittest

from semantic_extractor import LLMSemanticExtractor, parse_semantic_triples


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


class SemanticExtractorTests(unittest.TestCase):
    def test_parse_semantic_triples_accepts_json_response(self):
        triples = parse_semantic_triples(
            '{"triples":[{"subject":"user","predicate":"Preferred Language","object":"TypeScript","confidence":0.92}]}'
        )

        self.assertEqual(len(triples), 1)
        self.assertEqual(triples[0].subject, "user")
        self.assertEqual(triples[0].predicate, "preferred_language")
        self.assertEqual(triples[0].object_value, "TypeScript")
        self.assertEqual(triples[0].confidence, 0.92)

    def test_llm_semantic_extractor_returns_multiple_triples(self):
        extractor = LLMSemanticExtractor(
            lambda: _Client(
                '{"triples":['
                '{"subject":"user","predicate":"prefers","object":"Python","confidence":0.9},'
                '{"subject":"project","predicate":"uses","object":"SQLite memory store","confidence":0.8}'
                ']}'
            ),
            "test-model",
        )

        triples = extractor.extract("I prefer Python and this project uses SQLite memory.")

        self.assertEqual([triple.predicate for triple in triples], ["prefers", "uses"])
        self.assertEqual(triples[1].object_value, "SQLite memory store")

    def test_llm_semantic_extractor_falls_back_on_invalid_json(self):
        extractor = LLMSemanticExtractor(lambda: _Client("not json"), "test-model")

        triples = extractor.extract("I prefer Python")

        self.assertEqual(len(triples), 1)
        self.assertEqual(triples[0].subject, "user")
        self.assertEqual(triples[0].predicate, "prefers")
        self.assertEqual(triples[0].object_value, "Python")

    def test_llm_semantic_extractor_falls_back_on_client_error(self):
        extractor = LLMSemanticExtractor(lambda: _Client(error=RuntimeError("boom")), "test-model")

        triples = extractor.extract("I live in Taipei")

        self.assertEqual(triples[0].predicate, "lives_in")
        self.assertEqual(triples[0].object_value, "Taipei")


if __name__ == "__main__":
    unittest.main()
