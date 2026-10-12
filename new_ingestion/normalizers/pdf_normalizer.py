"""Normalize raw Docling PDF output into the canonical document schema.

This module is intentionally a conservative structural adapter.  It does not
run Docling, infer automotive semantics, or persist anything.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from new_ingestion.extraction.pdf_extractor import PdfExtractionResult
from new_ingestion.injestion_control.hashing import hash_normalized_content
from new_ingestion.schema.schema_define import (
    Asset, AssetType, BoundingBox, CanonicalDocument, CaptionElement,
    ChartElement, CodeBlockElement, Diagnostics, DocumentMetadata,
    DocumentStructure, ElementStatus, ElementType, EquationElement,
    ExtractionMetadata, ExtractionStatus, FigureElement, FooterElement,
    HeadingElement, ImageElement, LayoutInfo, ListElement, ListItemElement,
    NormalizationStatus, PageElement, ParagraphElement, PdfSourceLocator,
    ProvenanceRecord, Relationship, RelationshipType, TableCellElement,
    TableElement, TableRowElement, TextBlockElement, HeaderElement,
    FootnoteElement, SourceType, DiagramElement,
)


class PdfNormalizationError(ValueError):
    """Raised when raw PDF output cannot be normalized safely."""


def _text(value: Any, name: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise PdfNormalizationError(f"{name} must be a non-empty string")
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if required and not value:
        raise PdfNormalizationError(f"{name} must be a non-empty string")
    return value or None


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        if isinstance(dumped, Mapping):
            return dumped
    if hasattr(value, "to_dict"):
        dumped = value.to_dict()
        if isinstance(dumped, Mapping):
            return dumped
    return {name: getattr(value, name) for name in dir(value)
            if not name.startswith("_") and not callable(getattr(value, name))}


def _get(value: Any, *names: str, default: Any = None) -> Any:
    data = _mapping(value)
    for name in names:
        if name in data:
            return data[name]
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [value[key] for key in sorted(value, key=str)]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return [value]


def _bbox(value: Any) -> BoundingBox | None:
    if value is None:
        return None
    data = _mapping(value)
    if all(key in data for key in ("x", "y", "width", "height")):
        return BoundingBox(x=float(data["x"]), y=float(data["y"]),
                           width=float(data["width"]), height=float(data["height"]))
    # Docling commonly uses l/r/t/b. Preserve its coordinate convention while
    # expressing the box in the canonical x/y/width/height representation.
    if all(key in data for key in ("l", "r", "t", "b")):
        return BoundingBox(x=float(data["l"]), y=float(data["t"]),
                           width=max(0.0, float(data["r"]) - float(data["l"])),
                           height=max(0.0, float(data["b"]) - float(data["t"])))
    return None


def _locator(item: Any, page: int | None, warnings: list[str]) -> PdfSourceLocator | None:
    prov = _get(item, "prov", "provenance", default=None)
    prov_items = _items(prov)
    first = prov_items[0] if prov_items else None
    page_value = _get(first, "page_no", "page", "page_number", default=page)
    try:
        page_value = int(page_value) if page_value is not None else None
    except (TypeError, ValueError):
        page_value = page
    if page_value is None or page_value < 1:
        warnings.append("missing_source_locator: raw PDF element has no page information")
        return None
    box = _bbox(_get(first, "bbox", "bounding_box", "box", default=None))
    ref = _text(_get(first, "ref", "self_ref", "native_reference", default=None), "native_reference")
    return PdfSourceLocator(page=page_value, bounding_box=box, native_reference=ref)


def _native_type(item: Any) -> str:
    return (_text(_get(item, "label", "type", "kind", "name", default=""), "native_type") or "unknown").casefold()


def _canonical_type(native: str) -> ElementType:
    if "heading" in native or native in {"title", "section_header"}:
        return ElementType.HEADING
    if "list_item" in native or native == "listitem":
        return ElementType.LIST_ITEM
    if native == "list" or "list" in native:
        return ElementType.LIST
    if "table" in native:
        return ElementType.TABLE
    if "caption" in native:
        return ElementType.CAPTION
    if "equation" in native or "formula" in native:
        return ElementType.EQUATION
    if "code" in native:
        return ElementType.CODE_BLOCK
    if "footnote" in native:
        return ElementType.FOOTNOTE
    if native == "header" or "header" in native:
        return ElementType.HEADER
    if native == "footer" or "footer" in native:
        return ElementType.FOOTER
    if "diagram" in native:
        return ElementType.DIAGRAM
    if "chart" in native:
        return ElementType.CHART
    if "picture" in native or "image" in native:
        return ElementType.IMAGE
    if "figure" in native:
        return ElementType.FIGURE
    if "text" in native:
        return ElementType.TEXT_BLOCK
    return ElementType.PARAGRAPH


def _content_hash(element_type: ElementType, content: Any, **fields: Any) -> str:
    return hash_normalized_content({"type": element_type.value, "content": content, **fields})


def _raw_document(result: PdfExtractionResult) -> Mapping[str, Any]:
    if result.exported is not None:
        data = _mapping(result.exported)
        if data:
            return data
    if result.document is not None:
        data = _mapping(result.document)
        if data and any(key in data for key in ("pages", "texts", "text_blocks", "tables", "pictures", "figures", "images", "page_count")):
            return data
    raise PdfNormalizationError("raw extraction contains no usable Docling document/export data")


def _status(result: PdfExtractionResult) -> ExtractionStatus:
    value = str(result.conversion_status or "success").casefold()
    if "fail" in value or "error" in value:
        return ExtractionStatus.FAILED
    if "partial" in value or result.errors:
        return ExtractionStatus.PARTIAL
    return ExtractionStatus.SUCCESS


def _add_element(elements: dict[str, Any], order: list[str], element: Any) -> None:
    if element.element_id in elements:
        raise PdfNormalizationError(f"duplicate deterministic element ID: {element.element_id}")
    elements[element.element_id] = element
    order.append(element.element_id)


def _image_bytes_and_mime(item: Any) -> tuple[bytes | None, str | None]:
    """Extracts binary data and mime type from a Docling picture item.

    Docling nests picture data under an "image" object shaped like
    {"mimetype": ..., "uri": "data:<mime>;base64,<data>", ...} rather than
    exposing a flat bytes/path field, so the base64 data URI must be decoded.
    """
    image_info = _get(item, "image", default=None)
    if isinstance(image_info, bytes):
        return image_info, None
    if isinstance(image_info, Mapping):
        uri = image_info.get("uri")
        mime = image_info.get("mimetype")
        if isinstance(uri, str) and uri.startswith("data:"):
            _, _, encoded = uri.partition(",")
            if encoded:
                try:
                    return base64.b64decode(encoded), mime
                except (ValueError, TypeError):
                    return None, mime
        if isinstance(uri, str) and Path(uri).is_file():
            return Path(uri).read_bytes(), mime
        return None, mime
    if isinstance(image_info, (str, Path)) and Path(image_info).is_file():
        return Path(image_info).read_bytes(), None
    raw = _get(item, "data", "image_bytes", "uri", "path", default=None)
    if isinstance(raw, bytes):
        return raw, None
    if isinstance(raw, (str, Path)) and Path(raw).is_file():
        return Path(raw).read_bytes(), None
    return None, None


def _visual_asset(item: Any, document_id: str, assets: dict[str, Asset], artifact: Any,
                  warnings: list[str]) -> str | None:
    data, detected_mime = _image_bytes_and_mime(item)
    if data is None:
        warnings.append("unmaterialized_asset: visual has no valid binary data or artifact path")
        return None
    digest = hashlib.sha256(data).hexdigest()
    asset_id = f"asset-{digest[:24]}"
    if asset_id not in assets:
        mime = _text(_get(item, "mime_type", "content_type", default=None), "mime_type") or detected_mime
        mime = mime or mimetypes.guess_type(str(_get(item, "filename", "name", default="")))[0] or "application/octet-stream"
        relative = f"assets/{asset_id}{mimetypes.guess_extension(mime) or '.bin'}"
        if artifact is not None:
            target = Path(artifact) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(data)
        assets[asset_id] = Asset(asset_id=asset_id, asset_type=AssetType.IMAGE,
                                 mime_type=mime, sha256=digest, relative_uri=relative,
                                 file_size=len(data), metadata={"source": "docling"})
    return asset_id


def normalize_pdf(
    extraction_result: PdfExtractionResult,
    *,
    document_id: str,
    version_id: str,
    source_file_hash: str,
    logical_document_key: str | None = None,
) -> CanonicalDocument:
    """Convert one raw PDF extraction result into a validated canonical document."""
    if not isinstance(extraction_result, PdfExtractionResult):
        raise TypeError("extraction_result must be a PdfExtractionResult")
    document_id = _text(document_id, "document_id", required=True)  # type: ignore[assignment]
    version_id = _text(version_id, "version_id", required=True)  # type: ignore[assignment]
    if logical_document_key is not None:
        logical_document_key = _text(logical_document_key, "logical_document_key", required=True)
    source_file_hash = _text(source_file_hash, "source_file_hash", required=True)  # type: ignore[assignment]
    if len(source_file_hash) != 64 or any(char not in "0123456789abcdefABCDEF" for char in source_file_hash):
        raise ValueError("source_file_hash must be a valid 64-character hexadecimal SHA-256 hash")
    source_file_hash = source_file_hash.lower()
    raw = _raw_document(extraction_result)
    warnings: list[str] = [f"extraction_warning: {item}" for item in extraction_result.warnings]
    errors: list[str] = [f"extraction_error: {item}" for item in extraction_result.errors]
    elements: dict[str, Any] = {}
    assets: dict[str, Asset] = {}
    order: list[str] = []
    roots: list[str] = []
    relationships: list[Relationship] = []
    artifact = extraction_result.processing_configuration.get("artifact_dir")

    pages = _items(_get(raw, "pages", default=None))
    if not pages:
        page_count = int(_get(raw, "page_count", default=0) or 0)
        pages = [{"page_no": index} for index in range(1, page_count + 1)]
    texts = _items(_get(raw, "texts", "text_blocks", default=None))
    tables = _items(_get(raw, "tables", default=None))
    pictures = _items(_get(raw, "pictures", "figures", "images", default=None))

    page_ids: dict[int, str] = {}
    for page_index, page_item in enumerate(pages, 1):
        page_no = int(_get(page_item, "page_no", "page", "page_number", default=page_index) or page_index)
        page_id = f"pdf-page-{page_no:03d}"
        page_ids[page_no] = page_id
        page = PageElement(element_id=page_id, source_locator=_locator(page_item, page_no, warnings),
                           content=None, metadata={"native_type": "page"},
                           content_hash=_content_hash(ElementType.PAGE, None, page=page_no))
        _add_element(elements, order, page); roots.append(page_id)

    counters: dict[tuple[int, str], int] = {}
    def next_id(page_no: int, kind: ElementType) -> str:
        key = (page_no, kind.value); counters[key] = counters.get(key, 0) + 1
        return f"pdf-page-{page_no:03d}-{kind.value}-{counters[key]:03d}"

    def attach(element: Any, page_no: int) -> None:
        parent_id = page_ids.get(page_no)
        if parent_id:
            element.parent_id = parent_id
            elements[parent_id].child_ids.append(element.element_id)
            relationships.append(Relationship(relationship_id=f"rel-contains-{len(relationships)+1:05d}",
                type=RelationshipType.CONTAINS, source_id=parent_id, target_id=element.element_id))
        else:
            roots.append(element.element_id)
            warnings.append("missing_source_locator: element could not be attached to a page")
        _add_element(elements, order, element)

    for item in texts:
        native = _native_type(item); kind = _canonical_type(native)
        prov = _get(item, "prov", "provenance", default=None); first = _items(prov)[0] if _items(prov) else None
        page_no = int(_get(first, "page_no", "page", default=1) or 1)
        text = _get(item, "text", "content", "orig", default="")
        text = "" if text is None else str(text)
        eid = next_id(page_no, kind)
        common = {"element_id": eid, "content": text, "source_locator": _locator(item, page_no, warnings),
                  "metadata": {"native_type": native}, "content_hash": _content_hash(kind, text)}
        if kind == ElementType.HEADING:
            element = HeadingElement(**common, level=_get(item, "level", "heading_level", default=None))
        elif kind == ElementType.LIST:
            element = ListElement(**common)
        elif kind == ElementType.LIST_ITEM:
            element = ListItemElement(**common)
        elif kind == ElementType.CAPTION:
            element = CaptionElement(**common)
        elif kind == ElementType.EQUATION:
            element = EquationElement(**common)
        elif kind == ElementType.CODE_BLOCK:
            element = CodeBlockElement(**common)
        elif kind == ElementType.HEADER:
            element = HeaderElement(**common)
        elif kind == ElementType.FOOTER:
            element = FooterElement(**common)
        elif kind == ElementType.FOOTNOTE:
            element = FootnoteElement(**common)
        else:
            if kind == ElementType.PARAGRAPH and native not in {"paragraph", "text", "text_block", "unknown"}:
                warnings.append(f"unsupported_native_type: mapped {native!r} to paragraph")
            element = TextBlockElement(**common) if kind == ElementType.TEXT_BLOCK else ParagraphElement(**common)
        attach(element, page_no)

    for table_index, item in enumerate(tables, 1):
        first = _items(_get(item, "prov", "provenance", default=None)); first = first[0] if first else None
        page_no = int(_get(first, "page_no", "page", default=1) or 1)
        table_id = next_id(page_no, ElementType.TABLE)
        rows = _items(_get(_get(item, "data", default=item), "grid", "rows", default=_get(item, "rows", default=[])))
        row_ids: list[str] = []; header_ids: list[str] = []
        table = TableElement(element_id=table_id, row_count=len(rows), column_count=max((len(_items(r)) for r in rows), default=0),
            source_locator=_locator(item, page_no, warnings), metadata={"native_type": "table"},
            content_hash=_content_hash(ElementType.TABLE, rows))
        attach(table, page_no)
        for row_index, row in enumerate(rows):
            row_id = f"{table_id}-row-{row_index+1:03d}"; cell_ids=[]
            row_element = TableRowElement(element_id=row_id, parent_id=table_id, row_index=row_index, cell_ids=cell_ids,
                content_hash=_content_hash(ElementType.TABLE_ROW, row_index))
            _add_element(elements, order, row_element); table.row_ids.append(row_id); table.child_ids.append(row_id)
            relationships.append(Relationship(relationship_id=f"rel-contains-{len(relationships)+1:05d}", type=RelationshipType.CONTAINS, source_id=table_id, target_id=row_id))
            for col_index, cell in enumerate(_items(row)):
                cell_data = _mapping(cell); value = _get(cell, "text", "value", "content", default=cell if not isinstance(cell, Mapping) else "")
                cell_id = f"{row_id}-cell-{col_index+1:03d}"
                cell_element = TableCellElement(element_id=cell_id, parent_id=row_id, row_index=row_index, column_index=col_index,
                    row_span=int(_get(cell, "row_span", "rowspan", default=1) or 1), column_span=int(_get(cell, "column_span", "colspan", default=1) or 1),
                    display_value=None if value is None else str(value), typed_value=value, content=value,
                    metadata={"native": cell_data}, content_hash=_content_hash(ElementType.TABLE_CELL, value, row_index=row_index, column_index=col_index))
                _add_element(elements, order, cell_element); cell_ids.append(cell_id)
                row_element.child_ids.append(cell_id)
                relationships.append(Relationship(relationship_id=f"rel-contains-{len(relationships)+1:05d}", type=RelationshipType.CONTAINS, source_id=row_id, target_id=cell_id))

    for item in pictures:
        native = _native_type(item); kind = _canonical_type(native)
        if kind not in {ElementType.IMAGE, ElementType.FIGURE, ElementType.CHART, ElementType.DIAGRAM}:
            kind = ElementType.FIGURE; warnings.append(f"unsupported_native_type: mapped {native!r} to figure")
        first = _items(_get(item, "prov", "provenance", default=None)); first = first[0] if first else None
        page_no = int(_get(first, "page_no", "page", default=1) or 1)
        eid = next_id(page_no, kind); asset_id = _visual_asset(item, document_id, assets, artifact, warnings)
        metadata = {"native_type": native}
        for key in ("classification", "classification_confidence", "diagram_kind"):
            value = _get(item, key, default=None)
            if value is not None: metadata[key] = value
        cls = {"element_id": eid, "asset_id": asset_id, "source_locator": _locator(item, page_no, warnings),
               "metadata": metadata, "content_hash": _content_hash(kind, metadata)}
        element = {ElementType.IMAGE: ImageElement, ElementType.FIGURE: FigureElement,
                   ElementType.CHART: ChartElement, ElementType.DIAGRAM: DiagramElement}[kind](**cls)
        attach(element, page_no)

    config = dict(extraction_result.processing_configuration)
    config["normalizer"] = "pdf_normalizer"
    metadata = DocumentMetadata(document_id=document_id, version_id=version_id,
        logical_document_key=logical_document_key, source_file_name=extraction_result.source_path.name,
        source_type=SourceType.PDF, mime_type="application/pdf", sha256=source_file_hash,
        title=_text(_get(raw, "title", "name", default=None), "title"),
        extraction=ExtractionMetadata(extractor="docling", extractor_version=config.get("docling_version"),
            status=_status(extraction_result), warnings=warnings, errors=errors, configuration=config))
    return CanonicalDocument(document=metadata, structure=DocumentStructure(root_element_ids=roots, element_order=order),
        elements=elements, assets=assets, relationships=relationships,
        diagnostics=Diagnostics(warnings=warnings, errors=errors, normalization_status=NormalizationStatus.COMPLETE))


__all__ = ["PdfNormalizationError", "normalize_pdf"]
