import unittest

from vector_search import (
    HashingEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    VectorMemorySearcher,
    cosine_similarity,
    format_memory_for_embedding,
    normalize,
)


class StaticEmbeddingProvider:
    def __init__(self, embeddings):
        self.embeddings = embeddings

    def embed(self, text):
        return self.embeddings[text]


class _EmbeddingData:
    def __init__(self, embedding):
        self.embedding = embedding


class _EmbeddingResponse:
    def __init__(self, embedding):
        self.data = [_EmbeddingData(embedding)]


class _Embeddings:
    def __init__(self, embedding=None, error=None):
        self.embedding = embedding
        self.error = error

    def create(self, **kwargs):
        if self.error is not None:
            raise self.error
        return _EmbeddingResponse(self.embedding)


class _Client:
    def __init__(self, embedding=None, error=None):
        self.embeddings = _Embeddings(embedding, error)


class VectorSearchTests(unittest.TestCase):
    def test_cosine_similarity_scores_identical_vectors_highest(self):
        self.assertEqual(cosine_similarity([1.0, 0.0], [1.0, 0.0]), 1.0)
        self.assertEqual(cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0)

    def test_normalize_returns_unit_vector(self):
        self.assertEqual(normalize([3.0, 4.0]), [0.6, 0.8])

    def test_hashing_embedding_provider_is_deterministic(self):
        provider = HashingEmbeddingProvider(dimensions=8)

        self.assertEqual(provider.embed("Python tests"), provider.embed("Python tests"))

    def test_openai_compatible_embedding_provider_uses_embeddings_api(self):
        provider = OpenAICompatibleEmbeddingProvider(lambda: _Client([3.0, 4.0]), "embed-model")

        self.assertEqual(provider.embed("hello"), [0.6, 0.8])

    def test_openai_compatible_embedding_provider_falls_back(self):
        fallback = StaticEmbeddingProvider({"hello": [1.0, 0.0]})
        provider = OpenAICompatibleEmbeddingProvider(lambda: _Client(error=RuntimeError("boom")), "embed-model", fallback)

        self.assertEqual(provider.embed("hello"), [1.0, 0.0])

    def test_vector_memory_searcher_orders_by_similarity(self):
        memories = [
            {
                "id": 1,
                "subject": "user",
                "predicate": "likes",
                "object": "Taipei",
                "confidence": 1.0,
                "updated_at": "2026-01-01",
            },
            {
                "id": 2,
                "subject": "user",
                "predicate": "likes",
                "object": "Python",
                "confidence": 0.5,
                "updated_at": "2026-01-01",
            },
        ]
        provider = StaticEmbeddingProvider(
            {
                "coding help": [1.0, 0.0],
                "user likes Taipei": [0.0, 1.0],
                "user likes Python": [0.9, 0.1],
            }
        )
        searcher = VectorMemorySearcher(provider, min_score=0.0)

        ranked = searcher.search("coding help", memories, limit=2)

        self.assertEqual([memory["id"] for memory in ranked], [2, 1])

    def test_format_memory_for_embedding(self):
        text = format_memory_for_embedding({"subject": "user", "predicate": "likes", "object": "Python"})

        self.assertEqual(text, "user likes Python")


if __name__ == "__main__":
    unittest.main()
