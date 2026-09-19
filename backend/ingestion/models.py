"""Canonical ingestion models for the JLR Phase 1 master JSON contract.

This file defines the shared document envelope, canonical node types,
provenance, and node-specific attribute models that every parser should emit.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel
from pydantic import Field


class SourceType(str, Enum):
    """Enumerates the source types supported by the Phase 1 ingestion contract."""

    PDF = "pdf"
    PPT = "ppt"
    PPTX = "pptx"
    DOCX = "docx"
    HTML = "html"
    XLSX = "xlsx"


class DocumentFamily(str, Enum):
    """Enumerates the structure families used to describe parsed sources."""

    HIERARCHICAL = "Hierarchical Document"
    PAGE_CENTRIC = "Page-Centric Document"
    SLIDE_BASED = "Slide-Based Document"
    WORKBOOK_BASED = "Workbook-Based Document"
    TABLE_DOMINANT = "Table-Dominant Document"
    FORM_TEMPLATE = "Form / Template Document"
    NARRATIVE_EVIDENCE = "Narrative-Plus-Evidence Document"
    MIXED_LAYOUT = "Mixed-Layout Document"
    DATASET_EMBEDDED = "Dataset-Embedded Document"
    IMAGE_HEAVY = "Image-Heavy Technical Document"
    UNASSIGNED = "Unassigned / Low-Confidence Content"


class NodeType(str, Enum):
    """Enumerates every canonical node type used by the master JSON."""

    DOCUMENT_ROOT = "document_root"
    CHAPTER = "chapter"
    SECTION = "section"
    SUBSECTION = "subsection"
    PAGE = "page"
    SLIDE = "slide"
    WORKBOOK = "workbook"
    SHEET = "sheet"
    REGION = "region"
    TABLE_GROUP = "table_group"
    FORM_SECTION = "form_section"
    PARAGRAPH = "paragraph"
    TEXT_BLOCK = "text_block"
    BULLET_BLOCK = "bullet_block"
    TITLE_BLOCK = "title_block"
    NOTE_BLOCK = "note_block"
    GUIDELINE_BLOCK = "guideline_block"
    REFERENCE_BLOCK = "reference_block"
    MEASUREMENT_BLOCK = "measurement_block"
    LABEL_VALUE_BLOCK = "label_value_block"
    STATUS_BLOCK = "status_block"
    CAPTION = "caption"
    TABLE = "table"
    TABLE_HEADER = "table_header"
    TABLE_ROW = "table_row"
    TABLE_CELL = "table_cell"
    FIELD_GROUP = "field_group"
    EMBEDDED_DATASET = "embedded_dataset"
    IMAGE = "image"
    IMAGE_REGION = "image_region"
    CALLOUT_LABEL = "callout_label"
    OCR_BLOCK = "ocr_block"
    UNKNOWN_BLOCK = "unknown_block"
    UNASSIGNED_BLOCK = "unassigned_block"
    LOW_CONFIDENCE_BLOCK = "low_confidence_block"


class SecurityScope(BaseModel):
    """Stores access policy metadata that travels with every canonical document."""

    classification: str = "internal"
    allowed_groups: list[str] = Field(default_factory=list)
    allowed_users: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class ParserInfo(BaseModel):
    """Stores parser identity and version information for audit and debugging."""

    parser_name: str
    parser_version: str
    extraction_mode: str = "canonical"
    processing_details: dict[str, Any] = Field(default_factory=dict)


class LifecycleMetadata(BaseModel):
    """Stores version lifecycle details for a canonical document instance."""

    status: str = "active"
    is_latest: bool = True
    effective_from: str | None = None
    effective_to: str | None = None


class QualityMetadata(BaseModel):
    """Stores confidence and warning signals emitted during parsing."""

    overall_confidence: float = 1.0
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class Provenance(BaseModel):
    """Tracks the exact source location that produced a canonical node."""

    document_id: str
    source_type: SourceType
    version: str
    parent_node_id: str | None = None
    page_number: int | None = None
    slide_number: int | None = None
    sheet_name: str | None = None
    cell_range: str | None = None
    bbox: list[float] | None = None
    char_start: int | None = None
    char_end: int | None = None


class SurroundingText(BaseModel):
    """Stores local text around a table or image for improved downstream grounding."""

    text_above: str | None = None
    text_below: str | None = None


class SheetAttributes(BaseModel):
    """Stores Excel sheet-specific metadata."""

    sheet_name: str
    sheet_index: int
    classification: str = "UNKNOWN"
    used_range: str | None = None
    is_hidden: bool = False


class RegionAttributes(BaseModel):
    """Stores Excel region-specific metadata."""

    region_kind: str
    classification: str = "UNKNOWN"
    range: str
    header_depth: int = 1


class TableAttributes(BaseModel):
    """Stores normalized metadata about a detected table."""

    table_name: str | None = None
    header_depth: int = 1
    column_count: int = 0
    has_numeric_data: bool = False
    is_nested: bool = False
    units: list[str] = Field(default_factory=list)
    surrounding_text: SurroundingText = Field(default_factory=SurroundingText)


class TableHeaderAttributes(BaseModel):
    """Stores one or more header rows for a table."""

    header_rows: list[list[str]] = Field(default_factory=list)


class TableRowAttributes(BaseModel):
    """Stores metadata about a normalized table row."""

    row_index: int
    row_id: str
    is_total_row: bool = False
    is_header_repeat: bool = False
    semantic_text: str | None = None


class TableCellAttributes(BaseModel):
    """Stores metadata about a normalized table cell."""

    row_index: int
    column_index: int
    column_name: str
    value: Any = None
    normalized_value: Any = None
    unit: str | None = None
    value_type: str = "unknown"
    formula: str | None = None


class ImageAttributes(BaseModel):
    """Stores image and diagram metadata while keeping descriptions non-authoritative."""

    image_id: str
    image_type: str = "unknown"
    saved_path: str | None = None
    width: int | None = None
    height: int | None = None
    description: str | None = None
    technical_details: str | None = None
    is_authoritative: bool = False


class EmbeddedDatasetAttributes(BaseModel):
    """Stores metadata about a dataset discovered inside an HTML or document payload."""

    dataset_name: str
    dataset_format: str
    query_mode: str
    record_count: int = 0


class UnknownBlockAttributes(BaseModel):
    """Stores fallback metadata when a block cannot be typed confidently."""

    reason: str
    raw_text: str | None = None


class CanonicalNode(BaseModel):
    """Represents one reusable node in the master JSON tree."""

    node_id: str
    node_type: NodeType
    title: str | None = None
    text: str | None = None
    children: list["CanonicalNode"] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    provenance: Provenance
    confidence: float = 1.0


class DocumentMetadata(BaseModel):
    """Stores high-level descriptive metadata about the canonical document."""

    title: str | None = None
    document_family: DocumentFamily
    language: str = "en"
    page_count: int | None = None
    sheet_count: int | None = None
    slide_count: int | None = None
    keywords: list[str] = Field(default_factory=list)
    source_system: str | None = None


class CanonicalDocument(BaseModel):
    """Represents the full master JSON document emitted by the ingestion pipeline."""

    document_id: str
    agent_id: str
    source_type: SourceType
    file_name: str
    source_path: str
    source_name: str
    version: str
    ingested_at: datetime
    source_hash: str
    lifecycle: LifecycleMetadata = Field(default_factory=LifecycleMetadata)
    security_scope: SecurityScope = Field(default_factory=SecurityScope)
    parser_info: ParserInfo
    metadata: DocumentMetadata
    root_nodes: list[CanonicalNode] = Field(default_factory=list)
    unassigned_content: list[CanonicalNode] = Field(default_factory=list)
    quality: QualityMetadata = Field(default_factory=QualityMetadata)


# This rebuild ensures recursive node references resolve correctly in Pydantic.
CanonicalNode.model_rebuild()
