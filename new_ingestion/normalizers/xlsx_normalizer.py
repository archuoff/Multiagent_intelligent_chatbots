"""Deterministic normalization of the raw XLSX extraction result."""

from __future__ import annotations

import hashlib
import mimetypes
import re
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from new_ingestion.extraction.excel_extractor import (
    ExcelCell, ExcelExtractionResult, ExcelImage, ExcelTable, ExcelWorksheet,
)
from new_ingestion.injestion_control.hashing import hash_normalized_content
from new_ingestion.normalizers.ooxml_reconcilation import (
    ReconciledFormula, reconcile_xlsx,
)
from new_ingestion.schema.schema_define import (
    Asset, AssetType, CanonicalDocument, Diagnostics, DocumentMetadata,
    DocumentStructure, ElementType, ExtractionMetadata, ExtractionStatus,
    ImageElement, LayoutInfo, NormalizationStatus, Relationship,
    RelationshipType, SourceType, TableCellElement, TableElement,
    TableRowElement, WorksheetElement, XlsxSourceLocator,
)


class XlsxNormalizationError(ValueError):
    """Raised when an XLSX extraction result cannot be normalized."""


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_COORDINATE = re.compile(r"^([A-Za-z]{1,3})([1-9][0-9]*)$")
_RANGE = re.compile(r"^([A-Za-z]{1,3})([1-9][0-9]*):([A-Za-z]{1,3})([1-9][0-9]*)$")


def _required(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise XlsxNormalizationError(f"{name} must be a non-empty string")
    return value.strip()


def _safe_id(value: str) -> str:
    """Make a deterministic canonical-ID component while preserving source text elsewhere."""
    result = re.sub(r"[^A-Za-z0-9._:-]+", "_", value).strip("_")
    return result or "unnamed"


def _hash(kind: ElementType, content: Any, **fields: Any) -> str:
    return hash_normalized_content({"type": kind.value, "content": content, **fields})


def _column_number(letters: str) -> int:
    result = 0
    for character in letters.upper():
        result = result * 26 + ord(character) - 64
    return result


def _coordinate_parts(coordinate: str) -> tuple[int, int]:
    match = _COORDINATE.fullmatch(coordinate.strip())
    if not match:
        raise XlsxNormalizationError(f"invalid Excel cell coordinate: {coordinate!r}")
    return int(match.group(2)), _column_number(match.group(1))


def _range_parts(reference: str) -> tuple[int, int, int, int]:
    match = _RANGE.fullmatch(reference.strip())
    if not match:
        raise XlsxNormalizationError(f"invalid Excel range: {reference!r}")
    return (int(match.group(2)), _column_number(match.group(1)),
            int(match.group(4)), _column_number(match.group(3)))


def _persist_xlsx_asset(binary_data: bytes, relative_uri: str, artifact_dir: Path) -> Path:
    """Persist one XLSX asset below ``artifact_dir`` without allowing escapes.

    The URI is deliberately treated as a relative POSIX-style artifact
    reference even on Windows.  Existing identical files are left untouched,
    which keeps repeated ingestion deterministic and avoids needless writes.
    """
    if not isinstance(binary_data, bytes):
        raise TypeError("binary_data must be bytes")
    if not isinstance(relative_uri, str) or not relative_uri.strip():
        raise ValueError("relative_uri must be a non-empty relative path")
    if not isinstance(artifact_dir, Path):
        artifact_dir = Path(artifact_dir)

    normalized = relative_uri.strip().replace("\\", "/")
    posix_uri = PurePosixPath(normalized)
    windows_uri = PureWindowsPath(normalized)
    if posix_uri.is_absolute() or windows_uri.is_absolute() or windows_uri.drive:
        raise ValueError(f"asset relative_uri must not be absolute: {relative_uri!r}")
    if any(part in {"", ".", ".."} for part in posix_uri.parts):
        raise ValueError(f"asset relative_uri contains unsafe path components: {relative_uri!r}")

    root = artifact_dir.expanduser().resolve()
    destination = (root / Path(*posix_uri.parts)).resolve()
    try:
        destination.relative_to(root)
    except ValueError as error:
        raise ValueError(f"asset relative_uri escapes artifact_dir: {relative_uri!r}") from error

    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(binary_data).hexdigest()
    if destination.is_file():
        existing_digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        if existing_digest == digest:
            return destination
    destination.write_bytes(binary_data)
    return destination


def _is_safe_xlsx_asset_reference(reference: Any) -> bool:
    if not isinstance(reference, str) or not reference.strip():
        return False
    normalized = reference.strip().replace("\\", "/")
    posix_uri = PurePosixPath(normalized)
    windows_uri = PureWindowsPath(normalized)
    return (
        normalized.startswith("images/")
        and not posix_uri.is_absolute()
        and not windows_uri.is_absolute()
        and not windows_uri.drive
        and ".." not in posix_uri.parts
    )


def _remove_duplicate_persisted_assets(
    assets: dict[str, Asset],
    artifact_dir: Path,
) -> None:
    """Remove legacy/alternate filenames for binaries selected by ``assets``.

    The extractor historically used a short hash filename while reconciliation
    uses a full hash filename.  Both can be present after one run.  Only files
    in the generated ``images`` directory are considered, and canonical
    relative URIs are always retained.
    """
    image_root = (artifact_dir / "images").resolve()
    if not image_root.is_dir():
        return
    canonical_paths = {
        (artifact_dir / asset.relative_uri).resolve()
        for asset in assets.values()
        if asset.relative_uri
    }
    target_hashes = {asset.sha256.lower() for asset in assets.values() if asset.sha256}
    for candidate in image_root.rglob("*"):
        if not candidate.is_file() or candidate.resolve() in canonical_paths:
            continue
        try:
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError:
            continue
        if digest in target_hashes:
            candidate.unlink()


def _column_name(number: int) -> str:
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _coordinate(row: int, column: int) -> str:
    return f"{_column_name(column)}{row}"


def _json_safe_value(value: Any) -> Any:
    """Convert extractor values to values accepted by canonical JSON hashing."""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    return value


def _image_source_coordinate(image: ExcelImage) -> str | None:
    """Return the worksheet coordinate used to order an image.

    Rich-cell images are located by ``source_cell`` while floating images are
    located by their drawing anchor.  Some extractors populate both fields;
    the explicit source cell is the more precise source location in that case.
    """
    for value in (image.source_cell, image.anchor_start):
        if isinstance(value, str) and _COORDINATE.fullmatch(value.strip()):
            return value.strip().upper()
    return None


def _locator(sheet: str, *, cell: str | None = None, reference: str | None = None,
             native_reference: str | None = None) -> XlsxSourceLocator:
    return XlsxSourceLocator(sheet=sheet, cell=cell, range=reference, native_reference=native_reference)


def _table_for_coordinate(sheet: ExcelWorksheet, coordinate: str) -> tuple[ExcelTable, int, int] | None:
    row, column = _coordinate_parts(coordinate)
    for table in sheet.tables:
        start_row, start_column, end_row, end_column = _range_parts(table.reference)
        if start_row <= row <= end_row and start_column <= column <= end_column:
            return table, row - start_row, column - start_column
    return None


def _merged_span(ranges: list[str], coordinate: str) -> tuple[int, int, str | None]:
    row, column = _coordinate_parts(coordinate)
    for reference in ranges:
        try:
            start_row, start_column, end_row, end_column = _range_parts(reference)
        except XlsxNormalizationError:
            continue
        if start_row <= row <= end_row and start_column <= column <= end_column:
            if row == start_row and column == start_column:
                return end_row - start_row + 1, end_column - start_column + 1, reference
            return 1, 1, reference
    return 1, 1, None


def _cell_data(cell: ExcelCell) -> dict[str, Any]:
    return {
        "coordinate": cell.coordinate,
        "raw_value": _json_safe_value(cell.raw_value),
        "formula": cell.formula,
        "cached_value": _json_safe_value(cell.cached_value),
        "data_type": cell.data_type,
        "formula_data_type": cell.formula_data_type,
        "number_format": cell.number_format,
        "hyperlink": cell.hyperlink,
        "comment": cell.comment,
        "image_formula": cell.image_formula,
    }


def _cell_element(
    cell: ExcelCell,
    *,
    sheet: ExcelWorksheet,
    parent_id: str,
    table: ExcelTable | None = None,
    row_index: int | None = None,
    column_index: int | None = None,
    cell_role: str | None = None,
    merged_span: tuple[int, int, str | None] = (1, 1, None),
    reconciled_formula: ReconciledFormula | None = None,
) -> TableCellElement:
    absolute_row, absolute_column = _coordinate_parts(cell.coordinate)
    metadata = _cell_data(cell)
    formula = (
        reconciled_formula.formula
        if reconciled_formula is not None and reconciled_formula.formula
        else cell.formula
    )
    cached_value = reconciled_formula.cached_value if reconciled_formula is not None else cell.cached_value
    if reconciled_formula is not None:
        metadata["ooxml_reconciliation"] = {
            "worksheet_xml_path": reconciled_formula.worksheet_xml_path,
            "classification": reconciled_formula.classification,
            "status": reconciled_formula.status,
            "formula": reconciled_formula.formula,
            "cached_value": reconciled_formula.cached_value,
            "external_url": reconciled_formula.external_url,
            "value_metadata_index": reconciled_formula.value_metadata_index,
            "rich_value_index": reconciled_formula.rich_value_index,
            "local_image_identifier": reconciled_formula.local_image_identifier,
            "relationship_id": reconciled_formula.relationship_id,
            "media_path": reconciled_formula.media_path,
            "image_kind": reconciled_formula.image_kind,
        }
    if merged_span[2] is not None:
        metadata["merged_range"] = merged_span[2]
    if cell.image_formula is not None:
        metadata["image_formula"] = cell.image_formula
    display = cached_value if formula is not None and cached_value is not None else cell.raw_value
    safe_display = _json_safe_value(display)
    return TableCellElement(
        element_id=parent_id,
        parent_id=None,
        row_index=absolute_row - 1 if row_index is None else row_index,
        column_index=absolute_column - 1 if column_index is None else column_index,
        row_span=merged_span[0], column_span=merged_span[1], cell_role=cell_role,
        typed_value=_json_safe_value(cell.raw_value), display_value=None if safe_display is None else str(safe_display),
        formula=formula, content=safe_display, source_locator=_locator(
            sheet.name, cell=cell.coordinate,
            reference=table.reference if table else None,
            native_reference=cell.coordinate,
        ), metadata=metadata,
        content_hash=_hash(ElementType.TABLE_CELL, {
                           **_cell_data(cell), "formula": formula, "cached_value": _json_safe_value(cached_value),
                           "reconciliation": metadata.get("ooxml_reconciliation"),
                       },
                           row_index=row_index, column_index=column_index),
    )


def _image_asset(
    image: Any,
    assets: dict[str, Asset],
    warnings: list[str],
    binary_by_asset: dict[str, bytes] | None = None,
) -> str | None:
    if image.binary_data is None:
        return None
    digest = hashlib.sha256(image.binary_data).hexdigest()
    if digest is not None:
        digest = digest.lower()
    if image.binary_data is not None and image.sha256 and image.sha256.lower() != digest:
        warnings.append(f"normalization_warning: image hash mismatch for {image.image_id}; binary hash used")
    if digest is not None and not _SHA256.fullmatch(digest):
        warnings.append(f"normalization_warning: invalid image hash for {image.image_id}")
        digest = None
    if digest is None:
        return None
    asset_id = f"asset-{digest[:24]}"
    if binary_by_asset is not None:
        binary_by_asset.setdefault(asset_id, image.binary_data)
    association_status = getattr(image, "status", "embedded_extracted")
    if association_status == "duplicate_asset":
        association_status = "mapped_floating_image"
    media_path = getattr(image, "media_path", None)
    provenance = list(getattr(image, "provenance", []) or [])
    if asset_id not in assets:
        reference = image.asset_reference
        if not _is_safe_xlsx_asset_reference(reference):
            reference = f"images/image-{digest}{mimetypes.guess_extension(image.content_type or '') or '.bin'}"
        assets[asset_id] = Asset(
            asset_id=asset_id, asset_type=AssetType.IMAGE, mime_type=image.content_type,
            sha256=digest, relative_uri=reference,
            file_size=len(image.binary_data) if image.binary_data is not None else None,
            metadata={
                "image_id": image.image_id,
                "media_path": media_path,
                "association_status": association_status,
                "association_statuses": [association_status],
                "media_paths": [media_path] if media_path else [],
                "provenance": provenance,
            },
        )
    else:
        metadata = assets[asset_id].metadata
        statuses = metadata.setdefault("association_statuses", [])
        if association_status not in statuses:
            statuses.append(association_status)
        if metadata.get("association_status") == "unmapped_media" and association_status == "mapped_floating_image":
            metadata["association_status"] = association_status
        media_paths = metadata.setdefault("media_paths", [])
        if media_path and media_path not in media_paths:
            media_paths.append(media_path)
        for item in provenance:
            if item not in metadata.setdefault("provenance", []):
                metadata["provenance"].append(item)
    return asset_id


def normalize_xlsx(
    extraction_result: ExcelExtractionResult,
    *,
    document_id: str,
    version_id: str,
    source_file_hash: str,
    logical_document_key: str | None = None,
    artifact_dir: str | Path | None = None,
) -> CanonicalDocument:
    """Convert a raw Excel extraction into a validated canonical document."""
    if not isinstance(extraction_result, ExcelExtractionResult):
        raise TypeError("extraction_result must be an ExcelExtractionResult")
    document_id = _required(document_id, "document_id")
    version_id = _required(version_id, "version_id")
    source_file_hash = _required(source_file_hash, "source_file_hash")
    if not _SHA256.fullmatch(source_file_hash):
        raise ValueError("source_file_hash must be a valid 64-character hexadecimal SHA-256 hash")
    source_file_hash = source_file_hash.lower()
    if logical_document_key is not None:
        logical_document_key = _required(logical_document_key, "logical_document_key")
    if extraction_result.worksheets is None:
        raise XlsxNormalizationError("XLSX extraction contains no worksheet collection")

    warnings = [f"extraction_warning: {item}" for item in extraction_result.warnings]
    errors = [f"extraction_error: {item}" for item in extraction_result.errors]
    elements: dict[str, Any] = {}
    assets: dict[str, Asset] = {}
    binary_by_asset: dict[str, bytes] = {}
    relationships: list[Relationship] = []
    order: list[str] = []
    roots: list[str] = []
    reconciliation = None
    if extraction_result.source_path.exists() and extraction_result.source_path.is_file():
        reconciliation = reconcile_xlsx(extraction_result)
        warnings.extend(reconciliation.warnings)
        errors.extend(reconciliation.errors)
    reconciled_formulas = {
        (item.worksheet_name, item.coordinate.upper()): item
        for item in (reconciliation.formulas if reconciliation is not None else ())
    }
    reconciled_images = reconciliation.images if reconciliation is not None else [
        image for image in extraction_result.images
        if not image.status.startswith("formula") and image.formula is None
    ]
    if reconciliation is not None:
        # Register every physical package media file before registering any
        # placement. Unmapped media therefore remains visible as an asset.
        for media in reconciliation.media_inventory:
            _image_asset(media, assets, warnings, binary_by_asset)
    image_assets = {
        id(image): _image_asset(image, assets, warnings, binary_by_asset)
        for image in reconciled_images
    }
    image_element_ids_by_cell: dict[tuple[str, str], list[str]] = {}
    image_asset_ids_by_cell: dict[tuple[str, str], list[str]] = {}
    image_counts: dict[str, int] = {}

    def add(element: Any) -> None:
        if element.element_id in elements:
            raise XlsxNormalizationError(f"duplicate canonical element ID: {element.element_id}")
        elements[element.element_id] = element
        order.append(element.element_id)

    def link(parent: Any, child: Any) -> None:
        if child.element_id in parent.child_ids:
            raise XlsxNormalizationError(f"duplicate child reference: {child.element_id}")
        child.parent_id = parent.element_id
        parent.child_ids.append(child.element_id)
        relationships.append(Relationship(
            relationship_id=f"rel-{len(relationships) + 1:05d}",
            type=RelationshipType.CONTAINS, source_id=parent.element_id, target_id=child.element_id,
        ))
        add(child)

    for sheet in sorted(extraction_result.worksheets, key=lambda item: item.order):
        if not isinstance(sheet.name, str) or not sheet.name.strip():
            raise XlsxNormalizationError("worksheet name must be a non-empty string")
        # Preserve the source worksheet name exactly for XML matching and
        # source locators; sanitize only the canonical ID component below.
        sheet_name = sheet.name
        if not sheet.cells and not sheet.tables and sheet.max_row == 0 and sheet.max_column == 0:
            # Empty worksheets are still meaningful and must be preserved.
            pass
        sheet_id = f"xlsx-sheet-{_safe_id(sheet_name)}"
        worksheet = WorksheetElement(
            element_id=sheet_id,
            source_locator=_locator(sheet_name, reference=sheet.dimensions, native_reference=f"sheet:{sheet_name}"),
            layout=LayoutInfo(order=sheet.order),
            metadata={"visibility": sheet.visibility, "dimensions": sheet.dimensions,
                      "max_row": sheet.max_row, "max_column": sheet.max_column,
                      "merged_ranges": list(sheet.merged_ranges),
                      "tables": [{"name": table.name, "display_name": table.display_name,
                                  "reference": table.reference} for table in sheet.tables]},
            content_hash=_hash(ElementType.WORKSHEET, {"name": sheet_name, "visibility": sheet.visibility},
                               dimensions=sheet.dimensions, order=sheet.order),
        )
        add(worksheet); roots.append(sheet_id)
        seen_coordinates: set[str] = set()
        cells_by_coordinate: dict[str, ExcelCell] = {}
        for cell in sheet.cells:
            coordinate = _required(cell.coordinate, "cell coordinate").upper()
            if not _COORDINATE.fullmatch(coordinate):
                raise XlsxNormalizationError(f"invalid Excel cell coordinate: {cell.coordinate!r}")
            if coordinate in seen_coordinates:
                raise XlsxNormalizationError(f"duplicate cell coordinate in worksheet {sheet_name}: {coordinate}")
            seen_coordinates.add(coordinate); cells_by_coordinate[coordinate] = cell

        table_coordinates: set[str] = set()
        for table_index, table in enumerate(sheet.tables, 1):
            start_row, start_column, end_row, end_column = _range_parts(table.reference)
            table_id = f"{sheet_id}-table-{_safe_id(table.name)}"
            table_element = TableElement(
                element_id=table_id, title=table.display_name or table.name,
                row_count=end_row - start_row + 1, column_count=end_column - start_column + 1,
                table_kind="excel_table", source_locator=_locator(sheet_name, reference=table.reference,
                                                                    native_reference=f"table:{table.name}"),
                metadata={"name": table.name, "display_name": table.display_name,
                          "reference": table.reference, "columns": list(table.columns), "style": table.style},
                content_hash=_hash(ElementType.TABLE, {"name": table.name, "reference": table.reference,
                                                       "columns": table.columns, "style": table.style}),
            )
            link(worksheet, table_element)
            for row_offset, row in enumerate(range(start_row, end_row + 1)):
                row_id = f"{table_id}-row-{row_offset + 1:03d}"
                row_element = TableRowElement(element_id=row_id, row_index=row_offset,
                    content_hash=_hash(ElementType.TABLE_ROW, row_offset, table=table.name))
                link(table_element, row_element); table_element.row_ids.append(row_id)
                if row_offset == 0:
                    table_element.header_row_ids.append(row_id)
                for column_offset, column in enumerate(range(start_column, end_column + 1)):
                    coordinate = _coordinate(row, column)
                    table_coordinates.add(coordinate)
                    source_cell = cells_by_coordinate.get(coordinate, ExcelCell(coordinate=coordinate))
                    cell_id = f"{row_id}-cell-{column_offset + 1:03d}"
                    cell_element = _cell_element(source_cell, sheet=sheet, parent_id=cell_id, table=table,
                                                 row_index=row_offset, column_index=column_offset,
                                                 cell_role="header" if row_offset == 0 else None,
                                                 merged_span=_merged_span(sheet.merged_ranges, coordinate),
                                                 reconciled_formula=reconciled_formulas.get((sheet_name, coordinate)))
                    link(row_element, cell_element); row_element.cell_ids.append(cell_id)

        for cell in sheet.cells:
            coordinate = cell.coordinate.upper()
            if coordinate in table_coordinates:
                continue
            cell_id = f"{sheet_id}-cell-{coordinate}"
            cell_element = _cell_element(cell, sheet=sheet, parent_id=cell_id,
                                         merged_span=_merged_span(sheet.merged_ranges, coordinate),
                                         reconciled_formula=reconciled_formulas.get((sheet_name, coordinate)))
            link(worksheet, cell_element)
            formula_record = reconciled_formulas.get((sheet_name, coordinate))
            if formula_record is not None:
                if formula_record.classification == "external_formula_image" or formula_record.status == "image_formula_unresolved":
                    warnings.append(
                        f"unresolved IMAGE() formula: worksheet={sheet_name}, cell={coordinate}"
                    )
            elif cell.image_formula is not None:
                status = str(cell.image_formula.get("status", ""))
                if "unresolved" in status or "external" in status:
                    warnings.append(
                        f"unresolved IMAGE() formula: worksheet={sheet_name}, cell={coordinate}"
                    )

        for image in reconciled_images:
            if image.worksheet_name != sheet_name:
                continue
            image_counts[sheet_name] = image_counts.get(sheet_name, 0) + 1
            index = image_counts[sheet_name]
            image_id = f"{sheet_id}-image-{index:03d}"
            reference = image.anchor_start
            anchor_range = f"{image.anchor_start}:{image.anchor_end}" if image.anchor_start and image.anchor_end else None
            asset_id = image_assets[id(image)]
            if image.binary_data is None:
                warnings.append(f"unmaterialized_asset: image {image.image_id} has no binary data")
            if image.status in {"unresolved_relationship", "unmapped_media", "external_formula_image", "image_formula_unresolved"} or image.external_url or image.formula:
                warnings.append(f"normalization_warning: external or unresolved image source for {image.image_id}")
            element = ImageElement(
                element_id=image_id, asset_id=asset_id,
                content=image.formula or image.external_url,
                source_locator=_locator(sheet_name, cell=reference or image.source_cell, reference=anchor_range,
                                        native_reference=(image.drawing_ids[0] if image.drawing_ids else image.image_id)),
                metadata={"image_id": image.image_id, "status": image.status,
                          "anchor_start": image.anchor_start, "anchor_end": image.anchor_end,
                          "anchors": image.anchors, "drawing_ids": image.drawing_ids,
                          "provenance": image.provenance, "pixel_width": image.pixel_width,
                          "pixel_height": image.pixel_height, "asset_reference": image.asset_reference,
                          "formula": image.formula, "source_cell": image.source_cell,
                          "external_url": image.external_url,
                          "image_kind": getattr(image, "image_kind", None),
                          "value_metadata_index": getattr(image, "value_metadata_index", None),
                          "rich_value_index": getattr(image, "rich_value_index", None),
                          "local_image_identifier": getattr(image, "local_image_identifier", None),
                          "worksheet_xml_path": getattr(image, "worksheet_xml_path", None),
                          "drawing_xml_path": getattr(image, "drawing_xml_path", None),
                          "media_path": getattr(image, "media_path", None),
                          "relationship_ids": getattr(image, "relationship_ids", []),
                          "association_status": getattr(image, "status", None)},
                content_hash=_hash(ElementType.IMAGE, {"sha256": assets[asset_id].sha256 if asset_id else None,
                                                       "worksheet": sheet_name, "anchor_start": image.anchor_start,
                                                       "anchor_end": image.anchor_end, "image_id": image.image_id}),
            )
            link(worksheet, element)
            source_coordinate = _image_source_coordinate(image)
            if source_coordinate is not None:
                key = (sheet_name, source_coordinate)
                image_element_ids_by_cell.setdefault(key, []).append(element.element_id)
                if asset_id is not None:
                    image_asset_ids_by_cell.setdefault(key, []).append(asset_id)

        # Keep the full cell and image elements separate, but make the
        # association explicit for consumers that want a logical cell-image
        # projection later.
        for cell_element in elements.values():
            if not isinstance(cell_element, TableCellElement):
                continue
            cell_locator = cell_element.source_locator
            if getattr(cell_locator, "sheet", None) != sheet_name:
                continue
            coordinate = getattr(cell_locator, "cell", None)
            if not coordinate:
                continue
            key = (sheet_name, coordinate.upper())
            image_ids = image_element_ids_by_cell.get(key)
            asset_ids = image_asset_ids_by_cell.get(key)
            if image_ids:
                cell_element.metadata["image_element_ids"] = list(image_ids)
            if asset_ids:
                cell_element.metadata["image_asset_ids"] = list(dict.fromkeys(asset_ids))

    for image in (reconciled_images if reconciliation is not None else extraction_result.images):
        if image.worksheet_name is None:
            warnings.append(f"missing worksheet association: image {image.image_id}")

    def source_order_key(element_id: str) -> tuple[int, int, int, int, str]:
        """Build a stable worksheet/row/column order independent of creation order."""
        element = elements[element_id]
        locator = element.source_locator
        sheet_name = getattr(locator, "sheet", None)
        sheet_order = next(
            (item.layout.order for item in elements.values()
             if isinstance(item, WorksheetElement)
             and getattr(item.source_locator, "sheet", None) == sheet_name
             and item.layout is not None and item.layout.order is not None),
            10**9,
        )
        row = column = 10**9
        if isinstance(element, WorksheetElement):
            row = column = 0
        elif isinstance(element, TableRowElement):
            parent = elements.get(element.parent_id or "")
            reference = getattr(getattr(parent, "source_locator", None), "range", None)
            if reference:
                try:
                    start_row, start_column, _, _ = _range_parts(reference)
                    row, column = start_row + element.row_index, start_column
                except XlsxNormalizationError:
                    pass
        else:
            coordinate = getattr(locator, "cell", None)
            if not coordinate:
                reference = getattr(locator, "range", None)
                if reference:
                    coordinate = reference.split(":", 1)[0]
            if coordinate:
                try:
                    row, column = _coordinate_parts(coordinate)
                except XlsxNormalizationError:
                    pass
        type_rank = {
            ElementType.WORKSHEET: 0,
            ElementType.TABLE: 1,
            ElementType.TABLE_ROW: 2,
            ElementType.TABLE_CELL: 3,
            ElementType.IMAGE: 4,
        }.get(element.type, 5)
        return sheet_order, row, column, type_rank, element_id

    # Elements are keyed by stable IDs; only the explicit order list is
    # source-ordered. Rebuild the dictionary in that same order as well: this
    # keeps JSON serializers and consumers that iterate ``elements`` directly
    # aligned with ``structure.element_order``.
    order = sorted(order, key=source_order_key)
    elements = {element_id: elements[element_id] for element_id in order}

    # Reconciliation can discover package media that openpyxl did not expose.
    # Persist each physical binary once, after all sources have been merged by
    # SHA-256.  For real extraction results, None uses the extractor's default
    # convention; synthetic/in-memory results without a source file remain
    # purely in memory for backwards compatibility.
    effective_artifact_dir: Path | None
    if artifact_dir is not None:
        effective_artifact_dir = Path(artifact_dir)
    elif extraction_result.source_path.exists() and extraction_result.source_path.is_file():
        effective_artifact_dir = Path("xlsx_artifacts") / extraction_result.source_path.stem
    else:
        effective_artifact_dir = None
    if effective_artifact_dir is not None:
        for asset_id, binary_data in binary_by_asset.items():
            asset = assets[asset_id]
            try:
                _persist_xlsx_asset(binary_data, asset.relative_uri, effective_artifact_dir)
            except (OSError, TypeError, ValueError) as error:
                errors.append(f"asset persistence error: {asset_id}: {error}")
        if not errors:
            try:
                _remove_duplicate_persisted_assets(assets, effective_artifact_dir)
            except OSError as error:
                errors.append(f"asset cleanup error: {error}")

    properties = extraction_result.workbook_metadata.get("properties", {})
    status = ExtractionStatus.FAILED if errors else (ExtractionStatus.PARTIAL if warnings else ExtractionStatus.SUCCESS)
    configuration = dict(extraction_result.extraction_configuration)
    configuration["normalizer"] = "xlsx_normalizer"
    metadata = DocumentMetadata(
        document_id=document_id, version_id=version_id, logical_document_key=logical_document_key,
        source_file_name=str(extraction_result.source_metadata.get("filename") or extraction_result.source_path.name),
        source_type=SourceType.XLSX,
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        sha256=source_file_hash, title=properties.get("title"), author=properties.get("creator"),
        language=properties.get("language"), revision=str(properties.get("revision")) if properties.get("revision") is not None else None,
        extraction=ExtractionMetadata(extractor="openpyxl", extractor_version=configuration.get("openpyxl_version"),
                                      status=status, warnings=warnings, errors=errors, configuration=configuration),
    )
    return CanonicalDocument(
        document=metadata, structure=DocumentStructure(root_element_ids=roots, element_order=order),
        elements=elements, assets=assets, relationships=relationships,
        diagnostics=Diagnostics(warnings=warnings, errors=errors, normalization_status=NormalizationStatus.COMPLETE),
    )


__all__ = ["XlsxNormalizationError", "normalize_xlsx", "_persist_xlsx_asset"]
