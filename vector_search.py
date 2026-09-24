from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any, Protocol


class EmbeddingProvider(Protocol):
    def embed(self, text: str) -> list[float]:
        ...


@dataclass(frozen=True)
class VectorSearchResult:
    memory: dict[str, Any]
    score: float


class HashingEmbeddingProvider:
    def __init__(self, dimensions: int = 64) -> None:
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in tokenize(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        return normalize(vector)


class OpenAICompatibleEmbeddingProvider:
    def __init__(
        self,
        client_factory: Any,
        model: str,
        fallback_provider: EmbeddingProvider | None = None,
    ) -> None:
        self.client_factory = client_factory
        self.model = model
        self.fallback_provider = fallback_provider or HashingEmbeddingProvider()

    def embed(self, text: str, allow_remote: bool = True) -> list[float]:
        if not allow_remote:
            return self.fallback_provider.embed(text)
        try:
            response = self.client_factory().embeddings.create(model=self.model, input=text)
            return normalize([float(value) for value in response.data[0].embedding])
        except Exception:
            return self.fallback_provider.embed(text)

    def fallback_embed(self, text: str) -> list[float]:
        return self.fallback_provider.embed(text)


class VectorMemorySearcher:
    def __init__(
        self,
        embedding_provider: EmbeddingProvider | None = None,
        min_score: float = 0.12,
        store: Any | None = None,
    ) -> None:
        self.embedding_provider = embedding_provider or HashingEmbeddingProvider()
        self.min_score = min_score
        self.store = store

    def search(self, query: str, memories: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        return self.search_with_budget(query, memories, limit)

    def search_with_budget(
        self,
        query: str,
        memories: list[dict[str, Any]],
        limit: int,
        *,
        allow_query_embedding: bool = True,
        max_missing_embeddings: int | None = None,
    ) -> list[dict[str, Any]]:
        if not query:
            return sorted(memories, key=lambda memory: (memory["confidence"], memory["updated_at"], memory["id"]), reverse=True)[:limit]

        query_vector = _embed(self.embedding_provider, query, allow_remote=allow_query_embedding)
        scored = []
        missing_embeddings_used = 0
        for memory in memories:
            memory_vector = None
            raw_embedding = memory.get("embedding")
            if raw_embedding:
                if isinstance(raw_embedding, list):
                    memory_vector = raw_embedding
                elif isinstance(raw_embedding, str):
                    try:
                        parsed = json.loads(raw_embedding)
                        if isinstance(parsed, list):
                            memory_vector = [float(v) for v in parsed]
                    except Exception:
                        memory_vector = None

            if memory_vector is None:
                allow_remote = max_missing_embeddings is None or missing_embeddings_used < max_missing_embeddings
                memory_vector = _embed(self.embedding_provider, format_memory_for_embedding(memory), allow_remote=allow_remote)
                if allow_remote:
                    missing_embeddings_used += 1
                memory["embedding"] = memory_vector
                if allow_remote and self.store is not None and "id" in memory and memory["id"]:
                    try:
                        self.store.update_semantic_embedding(int(memory["id"]), memory_vector)
                    except Exception:
                        pass

            score = cosine_similarity(query_vector, memory_vector)
            if score < self.min_score:
                continue
            scored.append((score, float(memory["confidence"]), memory["updated_at"], int(memory["id"]), memory))
        scored.sort(reverse=True)
        return [memory for _, _, _, _, memory in scored[:limit]]


def format_memory_for_embedding(memory: dict[str, Any]) -> str:
    return f"{memory['subject']} {memory['predicate']} {memory['object']}"


def _embed(provider: EmbeddingProvider, text: str, allow_remote: bool = True) -> list[float]:
    try:
        return provider.embed(text, allow_remote=allow_remote)  # type: ignore[call-arg]
    except TypeError:
        if allow_remote:
            return provider.embed(text)
        fallback = getattr(provider, "fallback_embed", None)
        if callable(fallback):
            return fallback(text)
        return HashingEmbeddingProvider().embed(text)


def cosine_similarity(first: list[float], second: list[float]) -> float:
    if not first or not second or len(first) != len(second):
        return 0.0
    numerator = sum(left * right for left, right in zip(first, second))
    first_norm = math.sqrt(sum(value * value for value in first))
    second_norm = math.sqrt(sum(value * value for value in second))
    if first_norm == 0.0 or second_norm == 0.0:
        return 0.0
    return numerator / (first_norm * second_norm)


def normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in re.findall(r"[\w]+", text) if len(token) >= 2]
