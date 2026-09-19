"""Application-facing chunking service between canonical storage and embedding.

This service invokes the structure-aware builder and validator. It deliberately
does not load an embedding model, write a vector database, or bypass canonical
quality gates; those are later indexing-layer responsibilities.
"""

from __future__ import annotations

from backend.ingestion.chunking.builder import CanonicalChunkBuilder
from backend.ingestion.chunking.contracts import ChunkBuildResult
from backend.ingestion.chunking.contracts import ChunkingPolicy
from backend.ingestion.chunking.chunk_store import ChunkArtifactStore, StoredChunkArtifact
from backend.ingestion.chunking.validation import ChunkValidator
from backend.ingestion.models import CanonicalDocument


class ChunkingService:
    """Builds validated, model-agnostic chunks from one canonical document."""

    def __init__(self, policy: ChunkingPolicy | None = None) -> None:
        """Shares one chunking policy across deterministic build and validation steps."""
        self._builder = CanonicalChunkBuilder(policy)
        self._validator = ChunkValidator()

    def build(self, document: CanonicalDocument) -> ChunkBuildResult:
        """Creates chunks and appends invariant failures without embedding any content."""
        result = self._builder.build(document)
        result.rejected.extend(self._validator.validate(result.chunks))
        return result

    def build_and_persist(self, document: CanonicalDocument, store: ChunkArtifactStore) -> tuple[ChunkBuildResult, StoredChunkArtifact]:
        """Builds chunks then stores the immutable artifact that later embedding must consume."""
        result = self.build(document)
        artifact = store.persist(document_id=document.document_id, agent_id=document.agent_id,
            source_version=document.version, source_hash=document.source_hash, result=result)
        return result, artifact
