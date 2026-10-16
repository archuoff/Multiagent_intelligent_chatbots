"""Canonical automotive document schema version 0.2.0.

The models in this module define a validated interchange envelope only. They
do not normalize extractor output, infer relationships, persist documents, or
perform semantic validation.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_NAME = "automotive_canonical_document"
SCHEMA_VERSION = "0.2.0"
_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


class SchemaModel(BaseModel):
    """Shared strict validation and JSON serialization configuration."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ElementType(StrEnum):
    PAGE = "page"
    SLIDE = "slide"
    WORKSHEET = "worksheet"
    SECTION = "section"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TEXT_BLOCK = "text_block"
    LIST = "list"
    LIST_ITEM = "list_item"
    TABLE = "table"
    TABLE_ROW = "table_row"
    TABLE_CELL = "table_cell"
    IMAGE = "image"
    FIGURE = "figure"
    CAPTION = "caption"
    CHART = "chart"
    DIAGRAM = "diagram"
    EQUATION = "equation"
    CODE_BLOCK = "code_block"
    HEADER = "header"
    FOOTER = "footer"
    FOOTNOTE = "footnote"


class SourceType(StrEnum):
    PDF = "pdf"
    PPTX = "pptx"
    XLSX = "xlsx"
    DOCX = "docx"


class ElementStatus(StrEnum):
    EXTRACTED = "extracted"
    PARTIAL = "partial"
    FAILED = "failed"


class ExtractionStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class NormalizationStatus(StrEnum):
    NOT_STARTED = "not_started"
    NOT_APPLICABLE = "not_applicable"
    COMPLETE = "complete"
    PARTIAL = "partial"


class EnrichmentStatus(StrEnum):
    NOT_PROCESSED = "not_processed"
    AVAILABLE = "available"
    FAILED = "failed"
    VALIDATED = "validated"


class AssetType(StrEnum):
    IMAGE = "image"
    FILE = "file"
    AUDIO = "audio"
    VIDEO = "video"
    OTHER = "other"


class RelationshipType(StrEnum):
    CONTAINS = "contains"
    CHILD_OF = "child_of"
    FOLLOWS = "follows"
    CAPTION_FOR = "caption_for"
    HEADER_FOR = "header_for"
    BELONGS_TO_TABLE = "belongs_to_table"
    PLACED_ON = "placed_on"
    CONTINUED_ON = "continued_on"
    SPECIFIES = "specifies"
    MEASURES = "measures"
    MOUNTED_ON = "mounted_on"
    CONNECTED_TO = "connected_to"
    USES_COORDINATE_SYSTEM = "uses_coordinate_system"
    APPLIES_TO = "applies_to"
    REFERENCES = "references"


class BoundingBox(SchemaModel):
    x: float
    y: float
    width: float = Field(ge=0)
    height: float = Field(ge=0)


class LayoutInfo(SchemaModel):
    bounding_box: BoundingBox | None = None
    page_width: float | None = Field(default=None, ge=0)
    page_height: float | None = Field(default=None, ge=0)
    coordinate_unit: str | None = None
    order: int | None = Field(default=None, ge=0)

# schemas for "Where exactly did this canonical element come from?"
class PdfSourceLocator(SchemaModel):
    source_type: Literal[SourceType.PDF] = SourceType.PDF
    page: int = Field(ge=1)
    bounding_box: BoundingBox | None = None
    native_reference: str | None = None


class PptxSourceLocator(SchemaModel):
    source_type: Literal[SourceType.PPTX] = SourceType.PPTX
    slide: int = Field(ge=1)
    shape_id: int | None = None
    bounding_box: BoundingBox | None = None
    native_reference: str | None = None


class XlsxSourceLocator(SchemaModel):
    source_type: Literal[SourceType.XLSX] = SourceType.XLSX
    sheet: str = Field(min_length=1)
    cell: str | None = None
    range: str | None = None
    table_name: str | None = None
    native_reference: str | None = None


class DocxSourceLocator(SchemaModel):
    source_type: Literal[SourceType.DOCX] = SourceType.DOCX
    section: int | str | None = None
    paragraph: int | str | None = None
    table: int | str | None = None
    run: int | str | None = None
    native_reference: str | None = None


SourceLocator = Annotated[
    Union[PdfSourceLocator, PptxSourceLocator, XlsxSourceLocator, DocxSourceLocator],
    Field(discriminator="source_type"),
]


class ProvenanceRecord(SchemaModel):
    source: str = Field(min_length=1)
    extractor: str | None = None
    extractor_version: str | None = None
    source_locator: SourceLocator | None = None
    evidence: str | None = None
    extracted_at: datetime | None = None


class Tolerance(SchemaModel):
    lower: float | None = None
    upper: float | None = None
    nominal: float | None = None
    unit: str | None = None
    text: str | None = None


class AutomotiveSemantic(SchemaModel):
    domain: str | None = None
    role: str | None = None
    component_references: list[str] = Field(default_factory=list)
    sensor_references: list[str] = Field(default_factory=list)
    connector_references: list[str] = Field(default_factory=list)
    coordinate_system: str | None = None
    quantity_type: str | None = None
    axis: str | None = None
    unit: str | None = None
    tolerance: Tolerance | None = None
    conditions: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    semantic_source: str | None = None


class OcrEnrichment(SchemaModel):
    text: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    engine: str | None = None
    engine_version: str | None = None
    status: EnrichmentStatus = EnrichmentStatus.NOT_PROCESSED
    provenance: list[ProvenanceRecord] = Field(default_factory=list)

    @field_validator("text", "engine", "engine_version", mode="before")
    @classmethod
    def normalize_non_empty_text(cls, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("OCR text and engine fields must be strings")
        value = value.strip()
        if not value:
            raise ValueError("OCR text and engine fields must not be empty")
        return value


class VlmEnrichment(SchemaModel):
    description: str | None = None
    labels: list[str] = Field(default_factory=list)
    diagram_kind: Literal[
        "mounting", "coordinate_system", "architecture", "flow", "wiring",
        "sensor_placement", "component", "exploded_view", "annotated_automotive",
    ] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    model: str | None = None
    model_version: str | None = None
    status: EnrichmentStatus = EnrichmentStatus.NOT_PROCESSED
    provenance: list[ProvenanceRecord] = Field(default_factory=list)

    @field_validator("description", "model", "model_version", mode="before")
    @classmethod
    def normalize_non_empty_text(cls, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("VLM description and model fields must be strings")
        value = value.strip()
        if not value:
            raise ValueError("VLM description and model fields must not be empty")
        return value

    @field_validator("labels", mode="before")
    @classmethod
    def normalize_labels(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, list):
            raise TypeError("VLM labels must be a list of strings")
        normalized: list[str] = []
        seen: set[str] = set()
        for label in value:
            if not isinstance(label, str):
                raise TypeError("VLM labels must contain only strings")
            label = label.strip()
            if not label:
                raise ValueError("VLM labels must not contain empty strings")
            if label not in seen:
                normalized.append(label)
                seen.add(label)
        return normalized


class VisualEnrichment(SchemaModel):
    ocr: OcrEnrichment | None = None
    vlm: VlmEnrichment | None = None


class BaseDocumentElement(SchemaModel):
    element_id: str = Field(min_length=1, max_length=128, pattern=_ID_PATTERN)
    type: ElementType
    parent_id: str | None = Field(default=None, max_length=128, pattern=_ID_PATTERN)
    child_ids: list[str] = Field(default_factory=list)
    content: Any | None = None
    source_locator: SourceLocator | None = None
    layout: LayoutInfo | None = None
    semantic: AutomotiveSemantic | None = None
    provenance: list[ProvenanceRecord] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: ElementStatus = ElementStatus.EXTRACTED
    content_hash: str | None = Field(default=None,pattern=r"^[A-Fa-f0-9]{64}$",)

class PageElement(BaseDocumentElement):
    type: Literal[ElementType.PAGE] = ElementType.PAGE


class SlideElement(BaseDocumentElement):
    type: Literal[ElementType.SLIDE] = ElementType.SLIDE


class WorksheetElement(BaseDocumentElement):
    type: Literal[ElementType.WORKSHEET] = ElementType.WORKSHEET


class SectionElement(BaseDocumentElement):
    type: Literal[ElementType.SECTION] = ElementType.SECTION


class HeadingElement(BaseDocumentElement):
    type: Literal[ElementType.HEADING] = ElementType.HEADING
    level: int | None = Field(default=None, ge=1)


class ParagraphElement(BaseDocumentElement):
    type: Literal[ElementType.PARAGRAPH] = ElementType.PARAGRAPH


class TextBlockElement(BaseDocumentElement):
    type: Literal[ElementType.TEXT_BLOCK] = ElementType.TEXT_BLOCK


class ListElement(BaseDocumentElement):
    type: Literal[ElementType.LIST] = ElementType.LIST


class ListItemElement(BaseDocumentElement):
    type: Literal[ElementType.LIST_ITEM] = ElementType.LIST_ITEM


class TableCellElement(BaseDocumentElement):
    type: Literal[ElementType.TABLE_CELL] = ElementType.TABLE_CELL
    row_index: int = Field(ge=0)
    column_index: int = Field(ge=0)
    row_span: int = Field(default=1, ge=1)
    column_span: int = Field(default=1, ge=1)
    cell_role: str | None = None
    typed_value: Any | None = None
    display_value: str | None = None
    unit: str | None = None
    formula: str | None = None
    tolerance: Tolerance | None = None


class TableRowElement(BaseDocumentElement):
    type: Literal[ElementType.TABLE_ROW] = ElementType.TABLE_ROW
    row_index: int = Field(ge=0)
    cell_ids: list[str] = Field(default_factory=list)


class TableElement(BaseDocumentElement):
    type: Literal[ElementType.TABLE] = ElementType.TABLE
    title: str | None = None
    caption_reference: str | None = Field(default=None, pattern=_ID_PATTERN)
    row_count: int = Field(default=0, ge=0)
    column_count: int = Field(default=0, ge=0)
    table_kind: str | None = None
    header_row_ids: list[str] = Field(default_factory=list)
    row_ids: list[str] = Field(default_factory=list)


class ImageElement(BaseDocumentElement):
    type: Literal[ElementType.IMAGE] = ElementType.IMAGE
    asset_id: str | None = Field(default=None, pattern=_ID_PATTERN)
    caption_reference: str | None = Field(default=None, pattern=_ID_PATTERN)
    visual_enrichment: VisualEnrichment | None = None


class FigureElement(BaseDocumentElement):
    type: Literal[ElementType.FIGURE] = ElementType.FIGURE
    asset_id: str | None = Field(default=None, pattern=_ID_PATTERN)
    visual_enrichment: VisualEnrichment | None = None


class CaptionElement(BaseDocumentElement):
    type: Literal[ElementType.CAPTION] = ElementType.CAPTION


class ChartElement(BaseDocumentElement):
    type: Literal[ElementType.CHART] = ElementType.CHART
    asset_id: str | None = Field(default=None, pattern=_ID_PATTERN)
    visual_enrichment: VisualEnrichment | None = None


class DiagramElement(BaseDocumentElement):
    type: Literal[ElementType.DIAGRAM] = ElementType.DIAGRAM
    asset_id: str | None = Field(default=None, pattern=_ID_PATTERN)
    visual_enrichment: VisualEnrichment | None = None


class EquationElement(BaseDocumentElement):
    type: Literal[ElementType.EQUATION] = ElementType.EQUATION


class CodeBlockElement(BaseDocumentElement):
    type: Literal[ElementType.CODE_BLOCK] = ElementType.CODE_BLOCK


class HeaderElement(BaseDocumentElement):
    type: Literal[ElementType.HEADER] = ElementType.HEADER


class FooterElement(BaseDocumentElement):
    type: Literal[ElementType.FOOTER] = ElementType.FOOTER


class FootnoteElement(BaseDocumentElement):
    type: Literal[ElementType.FOOTNOTE] = ElementType.FOOTNOTE


CanonicalElement = Annotated[
    Union[
        PageElement, SlideElement, WorksheetElement, SectionElement,
        HeadingElement, ParagraphElement, TextBlockElement, ListElement,
        ListItemElement, TableElement, TableRowElement, TableCellElement,
        ImageElement, FigureElement, CaptionElement, ChartElement,
        DiagramElement, EquationElement, CodeBlockElement, HeaderElement,
        FooterElement, FootnoteElement,
    ],
    Field(discriminator="type"),
]


class DocumentStructure(SchemaModel):
    root_element_ids: list[str] = Field(default_factory=list)
    element_order: list[str] = Field(default_factory=list)


class Asset(SchemaModel):
    asset_id: str = Field(min_length=1, max_length=128, pattern=_ID_PATTERN)
    asset_type: AssetType
    mime_type: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[A-Fa-f0-9]{64}$")
    relative_uri: str = Field(min_length=1)
    file_size: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Relationship(SchemaModel):
    relationship_id: str = Field(min_length=1, max_length=128, pattern=_ID_PATTERN)
    type: RelationshipType
    source_id: str = Field(min_length=1, max_length=128, pattern=_ID_PATTERN)
    target_id: str = Field(min_length=1, max_length=128, pattern=_ID_PATTERN)
    evidence: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    provenance: list[ProvenanceRecord] = Field(default_factory=list)


class ExtractionMetadata(SchemaModel):
    extractor: str = Field(min_length=1)
    extractor_version: str | None = None
    extracted_at: datetime | None = None
    status: ExtractionStatus = ExtractionStatus.SUCCESS
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    configuration: dict[str, Any] = Field(default_factory=dict)


class DocumentMetadata(SchemaModel):
    document_id: str = Field(min_length=1, max_length=128, pattern=_ID_PATTERN)
    version_id: str | None = Field(default=None, max_length=128, pattern=_ID_PATTERN)
    logical_document_key: str | None = Field(default=None, min_length=1)
    domain: str | None = None
    document_kind: str | None = None
    source_file_name: str = Field(min_length=1)
    source_type: SourceType
    mime_type: str = Field(min_length=1)
    sha256: str | None = Field(default=None, pattern=r"^[A-Fa-f0-9]{64}$")
    title: str | None = None
    author: str | None = None
    language: str | None = None
    revision: str | None = None
    vehicle_program: str | None = None
    vehicle_system: str | None = None
    subsystem: str | None = None
    extraction: ExtractionMetadata


class Diagnostics(SchemaModel):
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    normalization_status: NormalizationStatus = NormalizationStatus.NOT_STARTED


class CanonicalDocument(SchemaModel):
    schema_name: Literal[SCHEMA_NAME] = SCHEMA_NAME
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    document: DocumentMetadata
    structure: DocumentStructure = Field(default_factory=DocumentStructure)
    elements: dict[str, CanonicalElement] = Field(default_factory=dict)
    assets: dict[str, Asset] = Field(default_factory=dict)
    relationships: list[Relationship] = Field(default_factory=list)
    diagnostics: Diagnostics = Field(default_factory=Diagnostics)

    @model_validator(mode="after")
    def validate_references(self) -> "CanonicalDocument":
        element_ids = set(self.elements)
        asset_ids = set(self.assets)
        for registry_name, registry in (("element", self.elements), ("asset", self.assets)):
            for registry_id, item in registry.items():
                item_id = item.element_id if registry_name == "element" else item.asset_id
                if registry_id != item_id:
                    raise ValueError(f"{registry_name} registry key must match its ID: {registry_id}")
        all_ids = element_ids | asset_ids
        if element_ids & asset_ids:
            raise ValueError("element_id and asset_id values must not overlap")
        if len(self.structure.element_order) != len(set(self.structure.element_order)):
            raise ValueError("structure.element_order must not contain duplicates")
        missing_roots = set(self.structure.root_element_ids) - element_ids
        if missing_roots:
            raise ValueError(f"structure.root_element_ids reference unknown elements: {sorted(missing_roots)}")
        missing_order = set(self.structure.element_order) - element_ids
        if missing_order:
            raise ValueError(f"structure.element_order references unknown elements: {sorted(missing_order)}")

        for element in self.elements.values():
            if element.parent_id is not None:
                if element.parent_id not in element_ids:
                    raise ValueError(f"element {element.element_id} has an unknown parent_id")
                if element.parent_id == element.element_id:
                    raise ValueError(f"element {element.element_id} cannot be its own parent")
            if element.element_id in element.child_ids:
                raise ValueError(f"element {element.element_id} cannot be its own child")
            for child_id in element.child_ids:
                child = self.elements.get(child_id)
                if child is None:
                    raise ValueError(f"element {element.element_id} has an unknown child_id: {child_id}")
                if child.parent_id != element.element_id:
                    raise ValueError(
                        f"parent/child mismatch: {element.element_id} lists {child_id}, "
                        f"but the child parent_id is {child.parent_id!r}"
                    )
            if element.parent_id is not None:
                parent = self.elements[element.parent_id]
                if element.element_id not in parent.child_ids:
                    raise ValueError(
                        f"parent/child mismatch: {element.element_id} points to {element.parent_id}, "
                        "but is not listed in parent.child_ids"
                    )

            if isinstance(element, TableElement):
                for row_id in [*element.row_ids, *element.header_row_ids]:
                    row = self.elements.get(row_id)
                    if not isinstance(row, TableRowElement):
                        raise ValueError(f"table {element.element_id} references a non-table-row: {row_id}")
                if element.caption_reference is not None and element.caption_reference not in element_ids:
                    raise ValueError(f"table {element.element_id} has an unknown caption_reference")
            if isinstance(element, TableRowElement):
                for cell_id in element.cell_ids:
                    cell = self.elements.get(cell_id)
                    if not isinstance(cell, TableCellElement):
                        raise ValueError(f"table row {element.element_id} references a non-table-cell: {cell_id}")
            if isinstance(element, (ImageElement, FigureElement, ChartElement, DiagramElement)):
                if element.asset_id is not None and element.asset_id not in asset_ids:
                    raise ValueError(f"element {element.element_id} references an unknown asset_id")

        relationship_ids = [relationship.relationship_id for relationship in self.relationships]
        if len(relationship_ids) != len(set(relationship_ids)):
            raise ValueError("relationship_id values must be unique")
        for relationship in self.relationships:
            if relationship.source_id == relationship.target_id:
                raise ValueError(f"relationship {relationship.relationship_id} cannot reference itself")
            if relationship.source_id not in all_ids or relationship.target_id not in all_ids:
                raise ValueError(
                    f"relationship {relationship.relationship_id} references an unknown source or target"
                )
        return self


BaseElement = BaseDocumentElement


for _model in (
    BaseDocumentElement, PageElement, SlideElement, WorksheetElement, SectionElement,
    HeadingElement, ParagraphElement, TextBlockElement, ListElement, ListItemElement,
    TableElement, TableRowElement, TableCellElement, ImageElement, FigureElement,
    CaptionElement, ChartElement, DiagramElement, EquationElement, CodeBlockElement,
    HeaderElement, FooterElement, FootnoteElement,
):
    _model.model_rebuild()


__all__ = [
    "Asset", "AssetType", "AutomotiveSemantic", "BaseDocumentElement", "BaseElement",
    "BoundingBox", "CanonicalDocument", "CanonicalElement", "CaptionElement",
    "ChartElement", "CodeBlockElement", "Diagnostics", "DocumentMetadata",
    "DocumentStructure", "DocxSourceLocator", "ElementStatus", "ElementType", "EnrichmentStatus",
    "EquationElement", "ExtractionMetadata", "ExtractionStatus", "FigureElement",
    "FooterElement", "FootnoteElement", "HeadingElement", "ImageElement", "LayoutInfo",
    "ListElement", "ListItemElement", "NormalizationStatus", "OcrEnrichment", "PageElement",
    "ParagraphElement", "PdfSourceLocator", "PptxSourceLocator", "ProvenanceRecord",
    "Relationship", "RelationshipType", "SCHEMA_NAME", "SCHEMA_VERSION", "SchemaModel",
    "SectionElement", "SlideElement", "SourceLocator", "SourceType", "TableCellElement",
    "TableElement", "TableRowElement", "TextBlockElement", "Tolerance",
    "VlmEnrichment", "VisualEnrichment", "WorksheetElement", "XlsxSourceLocator",
]
