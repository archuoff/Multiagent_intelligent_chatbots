"""Safe orchestration from immutable chunk artifact to persistent vector index.

This is the only intended application path for automatic indexing. It verifies
the chunk checksum manifest first, then embeds the approved chunks and finally
upserts the matching vectors into Qdrant.
"""

from __future__ import annotations

from backend.ingestion.chunking.chunk_store import ChunkArtifactStore, StoredChunkArtifact
from backend.ingestion.embedding.contracts import EmbeddingBuildResult
from backend.ingestion.embedding.service import EmbeddingService
from backend.ingestion.indexing.qdrant_store import QdrantVectorStore
from backend.ingestion.indexing.contracts import VectorIndexWriteResult


class EmbeddingIndexingService:
    """Verifies, embeds, and indexes one immutable chunk artifact in that order."""

    def __init__(self, artifact_store: ChunkArtifactStore, embedding_service: EmbeddingService,
                 vector_store: QdrantVectorStore) -> None:
        """Receives explicit collaborators so storage and provider choices remain replaceable."""
        self._artifact_store = artifact_store
        self._embedding_service = embedding_service
        self._vector_store = vector_store

    def embed_and_index(self, artifact: StoredChunkArtifact) -> tuple[EmbeddingBuildResult, VectorIndexWriteResult]:
        """Consumes a checksum-verified artifact and never indexes content requiring review."""
        chunks = self._artifact_store.load(artifact)
        embeddings = self._embedding_service.embed(chunks)
        try:
            indexed = self._vector_store.upsert(chunks, embeddings)
        finally:
            self._vector_store.close()
        return embeddings, indexed
