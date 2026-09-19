"""Unit tests for secure, deterministic Azure embedding integration behavior."""

from __future__ import annotations

import unittest

from backend.ingestion.chunking import ChunkBuildResult, ChunkRejection, ChunkType, EmbeddingChunk
from backend.ingestion.embedding.azure_openai import AzureOpenAIEmbeddingProvider
from backend.ingestion.embedding.contracts import EmbeddingModelSettings
from backend.ingestion.embedding.service import EmbeddingService


class FakeRecord:
    """Represents one SDK embedding response record for adapter tests."""

    def __init__(self, index: int, embedding: list[float]) -> None:
        self.index = index
        self.embedding = embedding


class FakeEmbeddingsApi:
    """Records SDK calls and returns deliberately reversed response order."""

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions
        self.calls: list[list[str]] = []

    def create(self, *, input: list[str], model: str):
        """Creates deterministic vectors while testing response index restoration."""
        self.calls.append(input)
        records = [FakeRecord(index, [float(index)] * self.dimensions) for index in range(len(input))]
        return type("Response", (), {"data": list(reversed(records))})()


class FakeClient:
    """Exposes the same nested embedding API shape used by the Azure adapter."""

    def __init__(self, dimensions: int) -> None:
        self.embeddings = FakeEmbeddingsApi(dimensions)


def chunk(chunk_id: str, text: str = "PDC sensor requirement") -> EmbeddingChunk:
    """Creates a minimal approved chunk with the lineage required by embeddings."""
    return EmbeddingChunk(chunk_id=chunk_id, chunk_type=ChunkType.NARRATIVE, document_id="doc-1",
        agent_id="adas", source_type="pdf", source_version="v1", parent_node_id="section-1",
        source_node_ids=["node-1"], content_text=text, embedding_text=text,
        metadata={"security_scope": {"classification": "internal"}})


def provider(*, dimensions: int = 3, batch_size: int = 2) -> tuple[AzureOpenAIEmbeddingProvider, FakeClient]:
    """Builds an injected Azure adapter without importing the real Azure SDK."""
    client = FakeClient(dimensions)
    settings = EmbeddingModelSettings(deployment="embedding-deployment", dimensions=dimensions,
        batch_size=batch_size, max_retries=1)
    return AzureOpenAIEmbeddingProvider(settings, client), client


class AzureEmbeddingTests(unittest.TestCase):
    """Covers batching, response ordering, quality gates, and dimension validation."""

    def test_embeds_batches_and_preserves_chunk_order(self):
        """The service preserves artifact order even when the provider response is reordered."""
        adapter, client = provider()
        result = EmbeddingService(adapter).embed(ChunkBuildResult(policy_version="v1", chunks=[chunk("one"), chunk("two"), chunk("three")]))
        self.assertEqual([item.chunk_id for item in result.embedded_chunks], ["one", "two", "three"])
        self.assertEqual([item.embedding[0] for item in result.embedded_chunks], [0.0, 1.0, 0.0])
        self.assertEqual([len(call) for call in client.embeddings.calls], [2, 1])
        self.assertEqual(result.embedded_chunks[0].metadata["agent_id"], "adas")

    def test_blocks_artifacts_with_error_rejections(self):
        """An incomplete extraction cannot silently become a vector search result."""
        adapter, client = provider()
        artifact = ChunkBuildResult(policy_version="v1", chunks=[chunk("one")],
            rejected=[ChunkRejection(reason="Extraction conflict", severity="error")])
        with self.assertRaisesRegex(RuntimeError, "requires extraction review"):
            EmbeddingService(adapter).embed(artifact)
        self.assertEqual(client.embeddings.calls, [])

    def test_rejects_unexpected_vector_dimensions(self):
        """Deployment/model configuration mistakes are caught before vector indexing."""
        adapter, _ = provider(dimensions=2)
        adapter.settings.dimensions = 3
        with self.assertRaisesRegex(RuntimeError, "dimensions"):
            EmbeddingService(adapter).embed(ChunkBuildResult(policy_version="v1", chunks=[chunk("one")]))

    def test_skips_empty_embedding_text_without_provider_call(self):
        """Empty chunks remain auditable but are never sent to an embedding endpoint."""
        adapter, client = provider()
        result = EmbeddingService(adapter).embed(ChunkBuildResult(policy_version="v1", chunks=[chunk("empty", "")]))
        self.assertEqual(result.embedded_chunks, [])
        self.assertEqual(result.rejected_chunk_ids, ["empty"])
        self.assertEqual(client.embeddings.calls, [])
