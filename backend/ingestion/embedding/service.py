"""Reliable batching and validation for embedding-ready JLR chunks.

This service accepts only the deterministic output of ``chunking/``. It blocks
quality-gated documents, sends ``embedding_text`` in bounded batches, retries
transient provider failures, and validates every returned vector before any
future vector database can receive it.
"""

from __future__ import annotations

import time
from typing import Protocol

from backend.ingestion.chunking.contracts import ChunkBuildResult, EmbeddingChunk
from backend.ingestion.embedding.contracts import EmbeddedChunk, EmbeddingBuildResult, EmbeddingModelSettings


class EmbeddingProvider(Protocol):
    """Defines the minimal provider behavior needed by the batching service."""

    settings: EmbeddingModelSettings

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Returns one vector per input text in the original input order."""


class EmbeddingService:
    """Embeds approved chunk artifacts without interpreting their source structure."""

    def __init__(self, provider: EmbeddingProvider, *, sleep=time.sleep) -> None:
        """Accepts an adapter and injectable sleep function for deterministic retry tests."""
        self._provider = provider
        self._sleep = sleep

    def embed(self, chunks: ChunkBuildResult) -> EmbeddingBuildResult:
        """Builds validated vectors, refusing artifacts with blocking extraction rejections."""
        blocking = [item for item in chunks.rejected if item.severity == "error"]
        if blocking:
            raise RuntimeError("Embedding is blocked because the chunk artifact requires extraction review.")
        accepted = [chunk for chunk in chunks.chunks if chunk.embedding_text.strip()]
        rejected = [chunk.chunk_id for chunk in chunks.chunks if not chunk.embedding_text.strip()]
        embedded: list[EmbeddedChunk] = []
        batch_size = self._provider.settings.batch_size
        for start in range(0, len(accepted), batch_size):
            batch = accepted[start:start + batch_size]
            vectors = self._embed_with_retry([chunk.embedding_text for chunk in batch])
            if len(vectors) != len(batch):
                raise RuntimeError("Embedding provider returned a vector count that does not match its input batch.")
            for chunk, vector in zip(batch, vectors, strict=True):
                self._validate_vector(vector, chunk)
                embedded.append(EmbeddedChunk(chunk_id=chunk.chunk_id, embedding=vector,
                    embedding_model=self._provider.settings.deployment, dimensions=len(vector),
                    metadata=self._embedding_metadata(chunk)))
        return EmbeddingBuildResult(embedding_model=self._provider.settings.deployment,
            dimensions=self._provider.settings.dimensions, embedded_chunks=embedded, rejected_chunk_ids=rejected)

    def _embed_with_retry(self, texts: list[str]) -> list[list[float]]:
        """Retries provider failures while retaining only an exception type in the final error."""
        attempts = self._provider.settings.max_retries
        for attempt in range(attempts):
            try:
                return self._provider.embed(texts)
            except Exception as error:
                if attempt + 1 == attempts:
                    raise RuntimeError(f"Embedding request failed after {attempts} attempts: {type(error).__name__}.") from error
                self._sleep(self._provider.settings.retry_delay_seconds * (2 ** attempt))
        raise AssertionError("Retry loop must return or raise.")

    def _validate_vector(self, vector: list[float], chunk: EmbeddingChunk) -> None:
        """Rejects malformed or unexpected-dimension vectors before indexing can occur."""
        if len(vector) != self._provider.settings.dimensions:
            raise RuntimeError(f"Embedding dimensions for chunk '{chunk.chunk_id}' do not match the configured deployment.")
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in vector):
            raise RuntimeError(f"Embedding provider returned non-numeric values for chunk '{chunk.chunk_id}'.")

    def _embedding_metadata(self, chunk: EmbeddingChunk) -> dict[str, object]:
        """Carries only retrieval lineage required by the later vector-index writer."""
        return {
            "document_id": chunk.document_id,
            "agent_id": chunk.agent_id,
            "source_version": chunk.source_version,
            "parent_node_id": chunk.parent_node_id,
            "chunk_type": chunk.chunk_type.value,
            "security_scope": chunk.metadata.get("security_scope", {}),
        }
