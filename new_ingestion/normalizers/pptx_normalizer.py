"""Deterministic normalization of raw python-pptx output."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from new_ingestion.extraction.pptx_extractor import (
    PptxExtractionResult, PptxImage, PptxShape,
)
from new_ingestion.injestion_control.hashing import hash_normalized_content
from new_ingestion.schema.schema_define import (
    Asset, AssetType, BoundingBox, CanonicalDocument, CaptionElement, ChartElement,
    Diagnostics, DiagramElement, DocumentMetadata, DocumentStructure, ElementType,
    ExtractionMetadata, ExtractionStatus, FigureElement, HeadingElement, ImageElement,
    LayoutInfo, ListItemElement, ListElement, NormalizationStatus, ParagraphElement,
    ProvenanceRecord, Relationship, RelationshipType, SlideElement, SourceType,
    TableCellElement, TableElement, TableRowElement, TextBlockElement,
)


class PptxNormalizationError(ValueError):
    """Raised when a PPTX extraction result cannot be normalized."""


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def _required(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PptxNormalizationError(f"{name} must be a non-empty string")
    return value.strip()


def _content_hash(kind: ElementType, content: Any, **fields: Any) -> str:
    return hash_normalized_content({"type": kind.value, "content": content, **fields})


def _box(left: int | None, top: int | None, width: int | None, height: int | None) -> BoundingBox | None:
    if None in (left, top, width, height):
        return None
    return BoundingBox(x=float(left), y=float(top), width=max(0.0, float(width)), height=max(0.0, float(height)))


def _locator(slide: int, shape: PptxShape | None = None):
    from new_ingestion.schema.schema_define import PptxSourceLocator
    if shape is None:
        return PptxSourceLocator(slide=slide)
    return PptxSourceLocator(slide=slide, shape_id=shape.shape_id,
                             bounding_box=_box(shape.left, shape.top, shape.width, shape.height),
                             native_reference=(f"shape:{shape.shape_id}" if shape.shape_id is not None else None))


def _shape_kind(shape: PptxShape) -> tuple[ElementType, bool]:
    native = (shape.shape_type or "").casefold()
    placeholder = str(shape.placeholder or {}).casefold()
    if "table" in native and shape.table is not None:
        return ElementType.TABLE, False
    if "chart" in native:
        return ElementType.CHART, False
    if "smartart" in native or "diagram" in native:
        return ElementType.DIAGRAM, False
    if "picture" in native or "image" in native:
        return ElementType.IMAGE, False
    if "title" in placeholder or "title" in (shape.name or "").casefold():
        return ElementType.HEADING, False
    if shape.text_frame is not None:
        return ElementType.TEXT_BLOCK, False
    return ElementType.FIGURE, True


def _asset_records(images: list[PptxImage], assets: dict[str, Asset], warnings: list[str]) -> dict[tuple[int, int | None], str]:
    placements: dict[tuple[int, int | None], str] = {}
    for image in images:
        digest = hashlib.sha256(image.binary_data).hexdigest() if image.binary_data is not None else image.sha256.lower()
        if not _SHA256.fullmatch(digest):
            warnings.append(f"normalization_warning: invalid image SHA-256 for {image.image_id}")
            continue
        asset_id = f"asset-{digest[:24]}"
        relative_uri = image.asset_reference
        if not relative_uri or Path(relative_uri).is_absolute() or not relative_uri.replace("\\", "/").startswith("images/"):
            suffix = Path(relative_uri).suffix or ".bin"
            relative_uri = f"images/image-{digest}{suffix}"
        if asset_id not in assets:
            assets[asset_id] = Asset(
                asset_id=asset_id, asset_type=AssetType.IMAGE,
                mime_type=image.content_type, sha256=digest,
                relative_uri=relative_uri,
                file_size=len(image.binary_data) if image.binary_data is not None else None,
                metadata={"filename": image.filename, "image_id": image.image_id},
            )
        for placement in image.placements or [{"slide_number": image.slide_number, "shape_id": image.shape_id}]:
            placements[(int(placement.get("slide_number", image.slide_number)), placement.get("shape_id", image.shape_id))] = asset_id
    return placements


def normalize_pptx(
    extraction_result: PptxExtractionResult,
    *,
    document_id: str,
    version_id: str,
    source_file_hash: str,
    logical_document_key: str | None = None,
) -> CanonicalDocument:
    """Convert a raw PPTX extraction into a validated canonical document."""
    if not isinstance(extraction_result, PptxExtractionResult):
        raise TypeError("extraction_result must be a PptxExtractionResult")
    document_id = _required(document_id, "document_id")
    version_id = _required(version_id, "version_id")
    source_file_hash = _required(source_file_hash, "source_file_hash")
    if not _SHA256.fullmatch(source_file_hash):
        raise ValueError("source_file_hash must be a valid 64-character hexadecimal SHA-256 hash")
    source_file_hash = source_file_hash.lower()
    if logical_document_key is not None:
        logical_document_key = _required(logical_document_key, "logical_document_key")
    if extraction_result.slides is None:
        raise PptxNormalizationError("PPTX extraction contains no slides collection")

    warnings = [f"extraction_warning: {item}" for item in extraction_result.warnings]
    errors = [f"extraction_error: {item}" for item in extraction_result.errors]
    elements: dict[str, Any] = {}
    assets: dict[str, Asset] = {}
    relationships: list[Relationship] = []
    order: list[str] = []
    roots: list[str] = []
    placements = _asset_records(extraction_result.images, assets, warnings)
    counters: dict[tuple[int, str], int] = {}

    def next_id(slide: int, kind: ElementType) -> str:
        key = (slide, kind.value)
        counters[key] = counters.get(key, 0) + 1
        return f"pptx-slide-{slide:03d}-{kind.value}-{counters[key]:03d}"

    def add(element: Any) -> None:
        if element.element_id in elements:
            raise PptxNormalizationError(f"duplicate deterministic element ID: {element.element_id}")
        elements[element.element_id] = element
        order.append(element.element_id)

    def link(parent: Any, child: Any, relation_type: RelationshipType = RelationshipType.CONTAINS) -> None:
        if child.element_id in parent.child_ids:
            raise PptxNormalizationError(f"duplicate child reference: {child.element_id}")
        child.parent_id = parent.element_id
        parent.child_ids.append(child.element_id)
        relationships.append(Relationship(
            relationship_id=f"rel-{len(relationships) + 1:05d}", type=relation_type,
            source_id=parent.element_id, target_id=child.element_id,
        ))
        add(child)

    for slide in sorted(extraction_result.slides, key=lambda item: (item.order, item.slide_number)):
        slide_id = f"pptx-slide-{slide.slide_number:03d}"
        slide_element = SlideElement(
            element_id=slide_id, content=slide.speaker_notes,
            source_locator=_locator(slide.slide_number),
            layout=LayoutInfo(page_width=float(extraction_result.presentation_metadata.get("slide_width", 0) or 0),
                              page_height=float(extraction_result.presentation_metadata.get("slide_height", 0) or 0),
                              coordinate_unit=extraction_result.presentation_metadata.get("coordinate_unit"),
                              order=slide.order),
            metadata={"layout_name": slide.layout_name, "background": slide.background},
            content_hash=_content_hash(ElementType.SLIDE, slide.speaker_notes, order=slide.order),
        )
        add(slide_element); roots.append(slide_id)
        for shape in sorted(slide.shapes, key=lambda item: (item.order_index, item.z_order, item.shape_id or 0)):
            kind, unsupported = _shape_kind(shape)
            native = shape.shape_type or "unknown"
            if unsupported:
                warnings.append(f"unsupported_shape: slide {slide.slide_number}, shape {shape.shape_id} ({native}) mapped to figure")
            asset_id = placements.get((slide.slide_number, shape.shape_id))
            content = shape.text_frame.text if shape.text_frame is not None else None
            metadata = {
                "native_shape_type": native, "shape_name": shape.name,
                "shape_id": shape.shape_id, "order_index": shape.order_index,
                "z_order": shape.z_order, "rotation": shape.rotation,
                "placeholder": shape.placeholder,
            }
            # Explicit metadata from an extractor can be preserved without
            # inventing diagram/chart classifications.
            shape_element_id = next_id(slide.slide_number, kind)
            common = dict(element_id=shape_element_id, content=content,
                          source_locator=_locator(slide.slide_number, shape),
                          layout=LayoutInfo(bounding_box=_box(shape.left, shape.top, shape.width, shape.height),
                                            coordinate_unit=extraction_result.presentation_metadata.get("coordinate_unit"),
                                            order=shape.order_index), metadata=metadata,
                          content_hash=_content_hash(kind, content, shape_type=native,
                                                     order_index=shape.order_index))
            if kind == ElementType.HEADING:
                element = HeadingElement(**common)
            elif kind == ElementType.TABLE:
                element = TableElement(**common)
            elif kind == ElementType.IMAGE:
                element = ImageElement(**common, asset_id=asset_id)
            elif kind == ElementType.FIGURE:
                element = FigureElement(**common, asset_id=asset_id)
            elif kind == ElementType.CHART:
                element = ChartElement(**common, asset_id=asset_id)
            elif kind == ElementType.DIAGRAM:
                element = DiagramElement(**common, asset_id=asset_id)
            else:
                element = TextBlockElement(**common)
            link(slide_element, element)

            if kind == ElementType.TABLE and shape.table is not None:
                table = element
                for row in range(shape.table.rows):
                    row_id = f"{table.element_id}-row-{row + 1:03d}"
                    row_element = TableRowElement(element_id=row_id, row_index=row, content_hash=_content_hash(ElementType.TABLE_ROW, row))
                    link(table, row_element)
                    table.row_ids.append(row_id)
                    cells = [cell for cell in shape.table.cells if cell.row == row]
                    for cell in sorted(cells, key=lambda item: item.column):
                        cell_id = f"{row_id}-cell-{cell.column + 1:03d}"
                        cell_element = TableCellElement(
                            element_id=cell_id, row_index=cell.row, column_index=cell.column,
                            display_value=cell.text, typed_value=cell.text, content=cell.text,
                            metadata={"fill": cell.fill, "margins": cell.margins},
                            content_hash=_content_hash(ElementType.TABLE_CELL, cell.text,
                                                       row_index=cell.row, column_index=cell.column),
                        )
                        link(row_element, cell_element)
                table.row_count = shape.table.rows
                table.column_count = shape.table.columns
                table.content_hash = _content_hash(
                    ElementType.TABLE,
                    [{"row": row, "cells": [cell.text for cell in sorted(
                        [item for item in shape.table.cells if item.row == row],
                        key=lambda item: item.column)]} for row in range(shape.table.rows)],
                    rows=shape.table.rows, columns=shape.table.columns,
                )

            if shape.text_frame is not None and kind in {ElementType.TEXT_BLOCK, ElementType.HEADING}:
                for paragraph_index, paragraph in enumerate(shape.text_frame.paragraphs):
                    paragraph_id = f"{element.element_id}-paragraph-{paragraph_index + 1:03d}"
                    paragraph_element = ParagraphElement(
                        element_id=paragraph_id, content=paragraph.text,
                        metadata={"level": paragraph.level, "alignment": paragraph.alignment,
                                  "runs": [{"text": run.text, "font": run.font, "hyperlink": run.hyperlink}
                                           for run in paragraph.runs]},
                        content_hash=_content_hash(ElementType.PARAGRAPH, paragraph.text, level=paragraph.level),
                    )
                    link(element, paragraph_element)

    properties = extraction_result.presentation_metadata.get("properties", {})
    status = ExtractionStatus.FAILED if extraction_result.errors else ExtractionStatus.SUCCESS
    if warnings and not extraction_result.errors:
        status = ExtractionStatus.PARTIAL
    configuration = dict(extraction_result.extraction_configuration)
    configuration["normalizer"] = "pptx_normalizer"
    metadata = DocumentMetadata(
        document_id=document_id, version_id=version_id, logical_document_key=logical_document_key,
        source_file_name=str(extraction_result.source_metadata.get("filename") or extraction_result.source_path.name),
        source_type=SourceType.PPTX, mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        sha256=source_file_hash, title=properties.get("title"), author=properties.get("author"),
        language=properties.get("language"), revision=str(properties.get("revision")) if properties.get("revision") is not None else None,
        extraction=ExtractionMetadata(extractor="python-pptx", extractor_version=configuration.get("python_pptx_version"),
                                      status=status, warnings=warnings, errors=errors, configuration=configuration),
    )
    return CanonicalDocument(
        document=metadata,
        structure=DocumentStructure(root_element_ids=roots, element_order=order),
        elements=elements, assets=assets, relationships=relationships,
        diagnostics=Diagnostics(warnings=warnings, errors=errors, normalization_status=NormalizationStatus.COMPLETE),
    )


__all__ = ["PptxNormalizationError", "normalize_pptx"]
