"""Compacts xlsx CanonicalDocument elements into row-level content units.

Handles both shapes the xlsx normalizer can produce: cells wrapped in a
TableElement/TableRowElement (named Excel Tables) and cells attached directly
to the worksheet (plain ranges, grouped here by row_index).
"""

from __future__ import annotations

import re
from typing import Any

from new_ingestion.compaction.envelope import ContentUnit
from new_ingestion.schema.schema_define import (
    CanonicalDocument, ImageElement, TableCellElement, TableElement,
    TableRowElement, WorksheetElement,
)

_ROW_SUFFIX = re.compile(r"(\d+)$")


def compact_xlsx(document: CanonicalDocument) -> list[ContentUnit]:
    units: list[ContentUnit] = []
    for element in document.elements.values():
        if not isinstance(element, WorksheetElement):
            continue
        units.extend(_compact_worksheet(document, element))
    return units


def _compact_worksheet(document: CanonicalDocument, worksheet: WorksheetElement) -> list[ContentUnit]:
    sheet_name = _sheet_name(worksheet)
    sheet_key = _slug(sheet_name)
    children = [document.elements[child_id] for child_id in worksheet.child_ids]

    tables = [child for child in children if isinstance(child, TableElement)]
    row_units: list[ContentUnit]
    rows_by_index: dict[int, ContentUnit]
    if tables:
        row_units, rows_by_index = _rows_from_tables(document, tables, sheet_name, sheet_key)
    else:
        flat_cells = [child for child in children if isinstance(child, TableCellElement)]
        row_units, rows_by_index = _rows_from_flat_cells(flat_cells, sheet_name, sheet_key)

    images = [child for child in children if isinstance(child, ImageElement)]
    image_units = _attach_and_build_image_units(images, sheet_name, sheet_key, rows_by_index)

    return [*row_units, *image_units]


def _sheet_name(worksheet: WorksheetElement) -> str:
    if worksheet.source_locator is not None:
        sheet = getattr(worksheet.source_locator, "sheet", None)
        if sheet:
            return sheet
    return worksheet.element_id


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_") or "sheet"


def _rows_from_tables(
    document: CanonicalDocument, tables: list[TableElement], sheet_name: str, sheet_key: str,
) -> tuple[list[ContentUnit], dict[int, ContentUnit]]:
    units: list[ContentUnit] = []
    rows_by_index: dict[int, ContentUnit] = {}
    for table in tables:
        header_ids = set(table.header_row_ids)
        header_row = next(
            (document.elements[row_id] for row_id in table.header_row_ids
             if isinstance(document.elements.get(row_id), TableRowElement)),
            None,
        )
        headers = _headers_from_row(document, header_row) if header_row else {}
        for row_id in table.row_ids:
            if row_id in header_ids:
                continue
            row = document.elements.get(row_id)
            if not isinstance(row, TableRowElement):
                continue
            cells = [document.elements[cell_id] for cell_id in row.cell_ids
                     if isinstance(document.elements.get(cell_id), TableCellElement)]
            unit = _row_unit(cells, headers, sheet_name, sheet_key, row.row_index)
            if unit is None:
                continue
            units.append(unit)
            rows_by_index[row.row_index] = unit
    return units, rows_by_index


def _rows_from_flat_cells(
    cells: list[TableCellElement], sheet_name: str, sheet_key: str,
) -> tuple[list[ContentUnit], dict[int, ContentUnit]]:
    if not cells:
        return [], {}
    by_row: dict[int, list[TableCellElement]] = {}
    for cell in cells:
        by_row.setdefault(cell.row_index, []).append(cell)
    ordered_indexes = sorted(by_row)

    # Leading single-cell rows are title/banner rows (e.g. a merged "Market Study"
    # caption above the real header), not column headers. Keep them as standalone
    # title content units instead of letting them displace the real header row.
    units: list[ContentUnit] = []
    title_row_indexes: set[int] = set()
    header_row_index: int | None = None
    for row_index in ordered_indexes:
        if len(by_row[row_index]) >= 2 or row_index == ordered_indexes[-1]:
            header_row_index = row_index
            break
        title_row_indexes.add(row_index)
        title_unit = _title_unit(by_row[row_index], sheet_name, sheet_key, row_index)
        if title_unit is not None:
            units.append(title_unit)
    if header_row_index is None:
        header_row_index = ordered_indexes[0]
    headers = _headers_from_cells(by_row[header_row_index])

    skip = {header_row_index, *title_row_indexes}
    rows_by_index: dict[int, ContentUnit] = {}
    for row_index in sorted(index for index in by_row if index not in skip):
        row_cells = sorted(by_row[row_index], key=lambda cell: cell.column_index)
        unit = _row_unit(row_cells, headers, sheet_name, sheet_key, row_index)
        if unit is None:
            continue
        units.append(unit)
        rows_by_index[row_index] = unit
    return units, rows_by_index


def _title_unit(cells: list[TableCellElement], sheet_name: str, sheet_key: str, row_index: int) -> ContentUnit | None:
    texts = [str(_scalar_value(cell)) for cell in cells if _scalar_value(cell) not in (None, "")]
    if not texts:
        return None
    return ContentUnit(
        id=f"title-{sheet_key}-{row_index}",
        source_type="xlsx",
        container=sheet_name,
        embedding_text=" | ".join(texts),
        images=[],
        metadata={"row_index": row_index, "role": "title"},
    )


def _headers_from_row(document: CanonicalDocument, header_row: TableRowElement) -> dict[int, str]:
    cells = [document.elements[cell_id] for cell_id in header_row.cell_ids
             if isinstance(document.elements.get(cell_id), TableCellElement)]
    return _headers_from_cells(cells)


def _headers_from_cells(cells: list[TableCellElement]) -> dict[int, str]:
    headers: dict[int, str] = {}
    seen: set[str] = set()
    for cell in sorted(cells, key=lambda cell: cell.column_index):
        label = _scalar_value(cell)
        label = str(label).strip() if label not in (None, "") else ""
        if not label or label in seen:
            label = f"column_{cell.column_index}"
        seen.add(label)
        headers[cell.column_index] = label
    return headers


def _row_unit(
    cells: list[TableCellElement], headers: dict[int, str], sheet_name: str, sheet_key: str, row_index: int,
) -> ContentUnit | None:
    pairs: list[str] = []
    values: dict[str, Any] = {}
    source_element_ids: list[str] = []
    for cell in sorted(cells, key=lambda cell: cell.column_index):
        value = _scalar_value(cell)
        if value in (None, ""):
            continue
        label = headers.get(cell.column_index, f"column_{cell.column_index}")
        if cell.formula or cell.unit:
            field: dict[str, Any] = {"value": value}
            if cell.formula:
                field["formula"] = cell.formula
            if cell.unit:
                field["unit"] = cell.unit
            values[label] = field
            pairs.append(f"{label}: {value}")
        else:
            values[label] = value
            pairs.append(f"{label}: {value}")
        source_element_ids.append(cell.element_id)
    if not pairs:
        return None
    return ContentUnit(
        id=f"row-{sheet_key}-{row_index}",
        source_type="xlsx",
        container=sheet_name,
        embedding_text=" | ".join(pairs),
        images=[],
        metadata={
            "row_index": row_index,
            "values": values,
            "source_element_ids": source_element_ids,
        },
    )


def _scalar_value(cell: TableCellElement) -> Any:
    if cell.typed_value is not None:
        return cell.typed_value
    if cell.display_value is not None:
        return cell.display_value
    return cell.content


def _attach_and_build_image_units(
    images: list[ImageElement], sheet_name: str, sheet_key: str, rows_by_index: dict[int, ContentUnit],
) -> list[ContentUnit]:
    units: list[ContentUnit] = []
    for image in images:
        if image.asset_id is None:
            continue
        start_row, end_row = _anchor_row_range(image)
        for row_index, unit in rows_by_index.items():
            if start_row is not None and end_row is not None and start_row <= row_index <= end_row:
                unit.images.append(image.asset_id)

        embedding_text = _visual_embedding_text(image)
        if embedding_text is None:
            continue
        units.append(ContentUnit(
            id=image.element_id,
            source_type="xlsx",
            container=sheet_name,
            embedding_text=embedding_text,
            images=[image.asset_id],
            metadata={
                "anchor_cell": _anchor_cell(image),
                "row_index": start_row,
            },
        ))
    return units


def _anchor_cell(image: ImageElement) -> str | None:
    if image.source_locator is not None:
        cell = getattr(image.source_locator, "cell", None)
        if cell:
            return cell
    return image.metadata.get("anchor_start")


def _anchor_row_range(image: ImageElement) -> tuple[int | None, int | None]:
    start_cell = _anchor_cell(image)
    end_cell = image.metadata.get("anchor_end")
    start_row = _row_number(start_cell)
    end_row = _row_number(end_cell) or start_row
    if start_row is None:
        return None, None
    # Cell row_index is the excel row number minus one (row 2 -> row_index 1).
    return start_row - 1, (end_row - 1 if end_row is not None else start_row - 1)


def _row_number(cell_reference: str | None) -> int | None:
    if not cell_reference:
        return None
    match = _ROW_SUFFIX.search(cell_reference)
    return int(match.group(1)) if match else None


def _visual_embedding_text(image: ImageElement) -> str | None:
    enrichment = image.visual_enrichment
    if enrichment is None:
        return None
    parts: list[str] = []
    if enrichment.ocr is not None and enrichment.ocr.text:
        parts.append(f"OCR text: {enrichment.ocr.text}")
    if enrichment.vlm is not None and enrichment.vlm.description:
        parts.append(f"Description: {enrichment.vlm.description}")
    return ". ".join(parts) if parts else None
