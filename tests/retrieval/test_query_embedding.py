"""Unit tests for query-side embedding (Phase 1, round 1).

Follows the same offline Fake-Azure-client pattern as
tests/ingestion/test_azure_embeddings.py -- no real Azure SDK or network
call, just verifying the adapter is reused correctly for a single query
string instead of a batch of chunks.
"""

from __future__ import annotations

import unittest

from backend.ingestion.embedding.azure_openai import AzureOpenAIEmbeddingProvider
from backend.ingestion.embedding.contracts import EmbeddingModelSettings
from backend.ingestion.embedding.sparse_provider import FastEmbedSparseProvider
from backend.retrieval.query_embedding import (embed_query, embed_query_sparse,
    get_embedding_provider, get_sparse_embedding_provider)


class FakeRecord:
    def __init__(self, index: int, embedding: list[float]) -> None:
        self.index = index
        self.embedding = embedding


class FakeEmbeddingsApi:
    """Records SDK calls and returns one deterministic vector per input string."""

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions
        self.calls: list[list[str]] = []

    def create(self, *, input: list[str], model: str):
        self.calls.append(input)
        records = [FakeRecord(index, [float(index + 1)] * self.dimensions) for index in range(len(input))]
        return type("Response", (), {"data": records})()


class FakeClient:
    def __init__(self, dimensions: int) -> None:
        self.embeddings = FakeEmbeddingsApi(dimensions)


def provider(dimensions: int = 3) -> tuple[AzureOpenAIEmbeddingProvider, FakeClient]:
    client = FakeClient(dimensions)
    settings = EmbeddingModelSettings(deployment="embedding-deployment", dimensions=dimensions)
    return AzureOpenAIEmbeddingProvider(settings, client), client


class EmbedQueryTests(unittest.TestCase):
    def test_embeds_the_raw_query_string_with_no_synthetic_prefix(self):
        """The query text reaches the SDK exactly as given -- no "Document:/Context:" wrapping."""
        adapter, client = provider()
        vector = embed_query("what is the torque spec for the PDC sensor bracket?", provider=adapter)
        self.assertEqual(client.embeddings.calls, [["what is the torque spec for the PDC sensor bracket?"]])
        self.assertEqual(vector, [1.0, 1.0, 1.0])

    def test_returns_a_single_vector_not_a_batch(self):
        adapter, _ = provider(dimensions=5)
        vector = embed_query("supplier contact for ASA material", provider=adapter)
        self.assertEqual(len(vector), 5)

    def test_raises_clearly_if_the_provider_returns_no_embedding(self):
        """Defends against a provider returning [] even though a single query text was sent."""
        class EmptyResponseProvider:
            def embed(self, texts: list[str]) -> list[list[float]]:
                return []

        with self.assertRaises(RuntimeError):
            embed_query("anything", provider=EmptyResponseProvider())


class GetEmbeddingProviderTests(unittest.TestCase):
    def test_is_cached_across_calls(self):
        """Confirms the lru_cache avoids rebuilding the Azure client on every query."""
        get_embedding_provider.cache_clear()
        original_from_env = AzureOpenAIEmbeddingProvider.from_runtime_environment
        built = []
        try:
            AzureOpenAIEmbeddingProvider.from_runtime_environment = classmethod(
                lambda cls: built.append(1) or provider()[0])
            first = get_embedding_provider()
            second = get_embedding_provider()
            self.assertIs(first, second)
            self.assertEqual(len(built), 1)
        finally:
            AzureOpenAIEmbeddingProvider.from_runtime_environment = original_from_env
            get_embedding_provider.cache_clear()


class FakeSparseVector:
    """Mirrors fastembed's real SparseEmbedding return shape (numpy .indices/.values)."""

    def __init__(self, indices: list[int], values: list[float]) -> None:
        self.indices = indices
        self.values = values


class FakeSparseModel:
    def __init__(self) -> None:
        self.embed_calls: list[list[str]] = []

    def embed(self, texts):
        self.embed_calls.append(list(texts))
        return [FakeSparseVector([1, 5], [0.9, 0.4]) for _ in texts]


class EmbedQuerySparseTests(unittest.TestCase):
    def test_embeds_a_single_sparse_vector_via_the_real_provider_shape(self):
        """Exercises FastEmbedSparseProvider itself, not just a hand-faked embed_query_sparse provider."""
        provider = FastEmbedSparseProvider(model=FakeSparseModel())
        result = embed_query_sparse("torque spec for X", provider=provider)
        self.assertEqual(result, {"indices": [1, 5], "values": [0.9, 0.4]})
        self.assertIsInstance(result["indices"][0], int)
        self.assertIsInstance(result["values"][0], float)

    def test_raises_clearly_if_the_provider_returns_no_vector(self):
        class EmptyResponseProvider:
            def embed(self, texts: list[str]) -> list[dict]:
                return []

        with self.assertRaises(RuntimeError):
            embed_query_sparse("anything", provider=EmptyResponseProvider())


class GetSparseEmbeddingProviderTests(unittest.TestCase):
    def test_is_cached_across_calls(self):
        get_sparse_embedding_provider.cache_clear()
        original_from_default = FastEmbedSparseProvider.from_default_model
        built = []
        try:
            FastEmbedSparseProvider.from_default_model = classmethod(
                lambda cls: built.append(1) or FastEmbedSparseProvider(model=FakeSparseModel()))
            first = get_sparse_embedding_provider()
            second = get_sparse_embedding_provider()
            self.assertIs(first, second)
            self.assertEqual(len(built), 1)
        finally:
            FastEmbedSparseProvider.from_default_model = original_from_default
            get_sparse_embedding_provider.cache_clear()


if __name__ == "__main__":
    unittest.main()
