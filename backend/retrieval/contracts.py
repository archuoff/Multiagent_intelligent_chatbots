"""Typed results returned by the retrieval layer.

Kept separate from backend.ingestion.chunking.contracts because this is a
query-time view of a chunk (search score, resolved document title) rather
than the ingestion-time record itself.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RetrievedChunk(BaseModel):
    """One access-checked search hit, with citation coordinates attached."""

    chunk_id: str
    score: float
    chunk_type: str
    content_text: str
    document_id: str
    document_title: str | None = None
    page_number: int | None = None
    slide_number: int | None = None
    sheet_name: str | None = None
    cell_range: str | None = None
    breadcrumbs: list[str] = Field(default_factory=list)
    table_title: str | None = None
    header_context: list[str] = Field(default_factory=list)
    field_policies: list[dict] = Field(default_factory=list)
    security_classification: str = "internal"
    source_type: str = ""
    source_version: str = ""
    visual_node_type: str | None = None
    ocr_status: str | None = None
    image_description_status: str | None = None
    vlm_confidence: float | None = None
    image_path: str | None = None
    image_storage_status: str | None = None
    rerank_score: float | None = None


class RetrievalResult(BaseModel):
    """Groups every authorized chunk returned for one query."""

    query: str
    chunks: list[RetrievedChunk] = Field(default_factory=list)
