"""Versioned embedding-ready contracts produced from canonical Master JSON.

These records are independent of any embedding provider or vector database.
``builder.py`` creates them, ``validation.py`` checks them, and a future index
adapter will persist vectors without needing to reinterpret source documents.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ChunkType(str, Enum):
    """Identifies the retrieval behavior expected for one chunk."""

    NARRATIVE = "narrative"
    TABLE_ROW = "table_row"


class ChunkingPolicy(BaseModel):
    """Controls deterministic, structure-aware chunk assembly."""

    policy_version: str = "canonical-chunks-v2"
    max_chunk_characters: int = 1_800
    max_overlap_characters: int = 320
    max_table_row_characters: int = 3_000
    strict_quality_gate: bool = True


class EmbeddingChunk(BaseModel):
    """Represents one child-retrieval record with a link back to its canonical parent."""

    chunk_id: str
    chunk_type: ChunkType
    document_id: str
    agent_id: str
    source_type: str
    source_version: str
    parent_node_id: str
    source_node_ids: list[str]
    content_text: str
    embedding_text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChunkRejection(BaseModel):
    """Records content intentionally withheld from automatic embedding."""

    reason: str
    node_id: str | None = None
    severity: str = "warning"


class ChunkBuildResult(BaseModel):
    """Groups accepted chunks and explicit quality-gate rejections for one document."""

    policy_version: str
    chunks: list[EmbeddingChunk] = Field(default_factory=list)
    rejected: list[ChunkRejection] = Field(default_factory=list)
