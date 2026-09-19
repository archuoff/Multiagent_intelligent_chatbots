"""Deterministic validation for embedding-ready chunk records.

This protects the future embedding and vector-index layer from empty chunks,
duplicate IDs, missing parent links, and chunks missing ACL metadata. It does
not judge semantic relevance; retrieval evaluation handles that later.
"""

from __future__ import annotations

from backend.ingestion.chunking.contracts import ChunkRejection
from backend.ingestion.chunking.contracts import EmbeddingChunk


class ChunkValidator:
    """Checks chunk contract invariants without modifying source content."""

    def validate(self, chunks: list[EmbeddingChunk]) -> list[ChunkRejection]:
        """Returns every issue so batch ingestion can report all rejected records together."""
        issues: list[ChunkRejection] = []
        seen_ids: set[str] = set()
        for chunk in chunks:
            if chunk.chunk_id in seen_ids:
                issues.append(ChunkRejection(reason="Chunk ID is duplicated.", node_id=chunk.parent_node_id, severity="error"))
            seen_ids.add(chunk.chunk_id)
            if not chunk.content_text.strip() or not chunk.embedding_text.strip():
                issues.append(ChunkRejection(reason="Chunk has no embedding-ready text.", node_id=chunk.parent_node_id, severity="error"))
            if not chunk.parent_node_id or not chunk.source_node_ids:
                issues.append(ChunkRejection(reason="Chunk is missing canonical parent or source node links.", node_id=chunk.parent_node_id, severity="error"))
            if "security_scope" not in chunk.metadata:
                issues.append(ChunkRejection(reason="Chunk is missing security scope metadata.", node_id=chunk.parent_node_id, severity="error"))
        return issues
