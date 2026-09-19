"""Contracts for the local Qdrant vector-index persistence boundary."""

from __future__ import annotations

from pydantic import BaseModel, Field


class QdrantIndexSettings(BaseModel):
    """Controls stable agent-isolated Qdrant collection naming and write batches."""

    collection_prefix: str = "jlr"
    batch_size: int = Field(default=100, ge=1, le=5_000)


class VectorIndexWriteResult(BaseModel):
    """Reports an auditable Qdrant upsert without exposing vectors or source text."""

    collection_name: str
    indexed_chunk_ids: list[str]
    document_id: str
    source_version: str
