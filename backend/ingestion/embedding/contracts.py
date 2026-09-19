"""Provider-neutral contracts for turning approved chunks into dense vectors.

These models deliberately exclude API keys. ``azure_openai.py`` reads the key
only while constructing a live SDK client, while ``service.py`` uses these
records to validate and return traceable embedding results.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EmbeddingModelSettings(BaseModel):
    """Defines the non-secret configuration for one embedding deployment."""

    provider: str = "azure_openai"
    deployment: str
    api_version: str = "2024-02-01"
    dimensions: int = Field(gt=0)
    batch_size: int = Field(default=16, ge=1, le=256)
    max_retries: int = Field(default=3, ge=1, le=6)
    retry_delay_seconds: float = Field(default=0.75, ge=0.0, le=30.0)


class EmbeddedChunk(BaseModel):
    """Pairs one immutable chunk identity with its validated dense vector."""

    chunk_id: str
    embedding: list[float]
    embedding_model: str
    dimensions: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class EmbeddingBuildResult(BaseModel):
    """Groups vectors created from one chunk artifact and its quality outcome."""

    embedding_model: str
    dimensions: int
    embedded_chunks: list[EmbeddedChunk] = Field(default_factory=list)
    rejected_chunk_ids: list[str] = Field(default_factory=list)
