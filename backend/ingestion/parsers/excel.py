"""Source parser that assembles canonical Excel workbook documents.

This file inspects workbook structure, detects mixed regions, and maps Excel
content into the canonical master JSON while preserving SQL- and semantic-ready
structure for downstream branching.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
import hashlib
from pathlib import Path
import os
import re
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from backend.ingestion.models import CanonicalDocument
from backend.ingestion.models import CanonicalNode
from backend.ingestion.models import DocumentFamily
from backend.ingestion.models import DocumentMetadata
from backend.ingestion.models import NodeType
from backend.ingestion.models import ParserInfo
from backend.ingestion.models import Provenance
from backend.ingestion.models import QualityMetadata
from backend.ingestion.models import SecurityScope
from backend.ingestion.models import SourceType
from backend.ingestion.utils import hash_file
from backend.ingestion.utils import make_cell_range
from backend.ingestion.utils import make_id
from backend.ingestion.utils import normalize_text
from backend.ingestion.parsers.base import IngestionSource
from backend.ingestion.parsers.base import ParserContext
from backend.ingestion.parsers.base import SourceParser


class ExcelWorkbookParser(SourceParser):
    """Parses `.xlsx` workbooks into workbook, sheet, region, and table node trees."""

    source_types = (SourceType.XLSX,)

    # This function loads an Excel workbook and converts it into the canonical document schema.
    def parse(self, source: IngestionSource, context: ParserContext) -> CanonicalDocument:
        workbook = load_workbook(source.file_path, data_only=False, read_only=False)
        document_id = source.document_id or make_id("doc")
        try:
            max_cells = int(os.getenv("JLR_MAX_SHEET_CELLS", "2000000"))
            if any(sheet.max_row * sheet.max_column > max_cells for sheet in workbook):
                raise ValueError("Worksheet exceeds JLR_MAX_SHEET_CELLS; split or review the workbook before ingestion.")
            workbook_node = self._build_workbook_node(document_id, source, workbook)
            warnings = self._collect_workbook_warnings(workbook, workbook_node)
        finally:
            workbook.close()

        return CanonicalDocument(
            document_id=document_id,
            agent_id=source.agent_id,
            source_type=source.source_type,
            file_name=source.file_path.name,
            source_path=str(source.file_path),
            source_name=source.file_path.stem,
            version=source.version,
            ingested_at=datetime.utcnow(),
            source_hash=hash_file(source.file_path),
            security_scope=SecurityScope(
                classification=context.classification,
                allowed_groups=list(context.allowed_groups),
            ),
            parser_info=ParserInfo(
                parser_name="excel_workbook_parser",
                parser_version=context.parser_version,
            ),
            metadata=DocumentMetadata(
                title=source.file_path.stem,
                document_family=DocumentFamily.WORKBOOK_BASED,
                sheet_count=len(workbook.sheetnames),
            ),
            root_nodes=[workbook_node],
            quality=QualityMetadata(
                overall_confidence=0.9 if not warnings else 0.82,
                warnings=warnings,
            ),
        )

    # This function builds the top-level workbook node and all sheet descendants.
    def _build_workbook_node(
        self,
        document_id: str,
        source: IngestionSource,
        workbook: Any,
    ) -> CanonicalNode:
        workbook_node_id = make_id("workbook")
        sheet_nodes = [
            self._build_sheet_node(
                document_id=document_id,
                source_type=source.source_type,
                version=source.version,
                agent_id=source.agent_id,
                parent_node_id=workbook_node_id,
                sheet_index=index,
                worksheet=worksheet,
            )
            for index, worksheet in enumerate(workbook.worksheets)
        ]

        return CanonicalNode(
            node_id=workbook_node_id,
            node_type=NodeType.WORKBOOK,
            title=source.file_path.stem,
            children=sheet_nodes,
            attributes={"sheet_count": len(sheet_nodes)},
            provenance=Provenance(
                document_id=document_id,
                source_type=source.source_type,
                version=source.version,
            ),
            confidence=0.98,
        )

    # This function converts one worksheet into a canonical sheet node and its regions.
    def _build_sheet_node(
        self,
        document_id: str,
        source_type: SourceType,
        version: str,
        agent_id: str,
        parent_node_id: str,
        sheet_index: int,
        worksheet: Any,
    ) -> CanonicalNode:
        sheet_node_id = make_id("sheet")
        regions = self._detect_regions(worksheet)
        region_nodes = [
            self._build_region_node(
                document_id=document_id,
                source_type=source_type,
                version=version,
                parent_node_id=sheet_node_id,
                sheet_name=worksheet.title,
                region=region,
            )
            for region in regions
        ]
        image_nodes = self._build_image_nodes(
            worksheet=worksheet,
            document_id=document_id,
            source_type=source_type,
            version=version,
            agent_id=agent_id,
            parent_node_id=sheet_node_id,
            regions=regions,
        )
        image_rows = [node.attributes.get("anchor_row") for node in image_nodes if node.attributes.get("anchor_row")]

        return CanonicalNode(
            node_id=sheet_node_id,
            node_type=NodeType.SHEET,
            title=worksheet.title,
            children=[*region_nodes, *image_nodes],
            attributes={
                "sheet_name": worksheet.title,
                "sheet_index": sheet_index,
                "classification": self._classify_sheet(regions),
                "used_range": self._used_range(worksheet),
                "is_hidden": worksheet.sheet_state != "visible",
                "merged_ranges": [str(item) for item in worksheet.merged_cells.ranges],
                "embedded_image_count": len(getattr(worksheet, "_images", []) or []),
                "extracted_image_count": len(image_nodes),
                "formula_error_count": self._sheet_formula_error_count(worksheet),
                "image_only_row_ranges": self._image_only_row_ranges(image_rows, regions),
                "image_heavy_region_count": sum(1 for region in regions if region["classification"] == "IMAGE_HEAVY"),
            },
            provenance=Provenance(
                document_id=document_id,
                source_type=source_type,
                version=version,
                parent_node_id=parent_node_id,
                sheet_name=worksheet.title,
            ),
            confidence=0.94,
        )

    # This function creates a region node and nested content nodes from detected worksheet structure.
    def _build_region_node(
        self,
        document_id: str,
        source_type: SourceType,
        version: str,
        parent_node_id: str,
        sheet_name: str,
        region: dict[str, Any],
    ) -> CanonicalNode:
        region_node_id = make_id("region")
        child_nodes: list[CanonicalNode] = []

        if region["classification"] in {"STRUCTURED", "MIXED"}:
            child_nodes.append(
                self._build_table_node(
                    document_id=document_id,
                    source_type=source_type,
                    version=version,
                    parent_node_id=region_node_id,
                    sheet_name=sheet_name,
                    region=region,
                )
            )

        if region["title"]:
            child_nodes.insert(
                0,
                CanonicalNode(
                    node_id=make_id("title"),
                    node_type=NodeType.TITLE_BLOCK,
                    title=region["title"],
                    text=region["title"],
                    provenance=Provenance(
                        document_id=document_id,
                        source_type=source_type,
                        version=version,
                        parent_node_id=region_node_id,
                        sheet_name=sheet_name,
                        cell_range=region["range"],
                    ),
                    confidence=region["confidence"],
                ),
            )

        for note_text in region["notes"]:
            child_nodes.append(
                CanonicalNode(
                    node_id=make_id("note"),
                    node_type=NodeType.NOTE_BLOCK,
                    text=note_text,
                    provenance=Provenance(
                        document_id=document_id,
                        source_type=source_type,
                        version=version,
                        parent_node_id=region_node_id,
                        sheet_name=sheet_name,
                        cell_range=region["range"],
                    ),
                    confidence=region["confidence"],
                )
            )

        return CanonicalNode(
            node_id=region_node_id,
            node_type=NodeType.REGION,
            title=region["title"],
            children=child_nodes,
            attributes={
                "region_kind": region["kind"],
                "classification": region["classification"],
                "range": region["range"],
                "header_depth": region["header_depth"],
                "source_cells": region["source_cells"],
                "populated_cell_count": region["populated_cell_count"],
                "cell_density": region["cell_density"],
                "image_count": region["image_count"],
                "image_anchor_rows": region["image_anchor_rows"],
                "needs_vision_description": region["needs_vision_description"],
            },
            provenance=Provenance(
                document_id=document_id,
                source_type=source_type,
                version=version,
                parent_node_id=parent_node_id,
                sheet_name=sheet_name,
                cell_range=region["range"],
            ),
            confidence=region["confidence"],
        )

    # This function converts a structured region into a nested table node with headers, rows, and cells.
    def _build_table_node(
        self,
        document_id: str,
        source_type: SourceType,
        version: str,
        parent_node_id: str,
        sheet_name: str,
        region: dict[str, Any],
    ) -> CanonicalNode:
        table_node_id = make_id("table")
        header_node = CanonicalNode(
            node_id=make_id("header"),
            node_type=NodeType.TABLE_HEADER,
            attributes={"header_rows": region["header_rows"]},
            provenance=Provenance(
                document_id=document_id,
                source_type=source_type,
                version=version,
                parent_node_id=table_node_id,
                sheet_name=sheet_name,
                cell_range=region["range"],
            ),
            confidence=region["confidence"],
        )

        row_nodes = [
            self._build_row_node(
                document_id=document_id,
                source_type=source_type,
                version=version,
                parent_node_id=table_node_id,
                sheet_name=sheet_name,
                region=region,
                row_data=row_data,
            )
            for row_data in region["rows"]
        ]

        return CanonicalNode(
            node_id=table_node_id,
            node_type=NodeType.TABLE,
            title=region["title"],
            children=[header_node, *row_nodes],
            attributes={
                "table_name": region["table_name"],
                "header_depth": region["header_depth"],
                "column_count": len(region["columns"]),
                "has_numeric_data": region["has_numeric_data"],
                "is_nested": False,
                "units": region["units"],
                "surrounding_text": {
                    "text_above": region["title"],
                    "text_below": None,
                },
                "columns": region["columns"],
            },
            provenance=Provenance(
                document_id=document_id,
                source_type=source_type,
                version=version,
                parent_node_id=parent_node_id,
                sheet_name=sheet_name,
                cell_range=region["range"],
            ),
            confidence=region["confidence"],
        )

    # This function converts one normalized table row into a row node and child cell nodes.
    def _build_row_node(
        self,
        document_id: str,
        source_type: SourceType,
        version: str,
        parent_node_id: str,
        sheet_name: str,
        region: dict[str, Any],
        row_data: dict[str, Any],
    ) -> CanonicalNode:
        row_node_id = make_id("row")
        cell_nodes = []
        for column_index, column_name in enumerate(region["columns"], start=1):
            cell_nodes.append(
                CanonicalNode(
                    node_id=make_id("cell"),
                    node_type=NodeType.TABLE_CELL,
                    text=self._cell_text(row_data["values"].get(column_name)),
                    attributes={
                        "row_index": row_data["row_index"],
                        "column_index": column_index,
                        "column_name": column_name,
                        "value": row_data["values"].get(column_name),
                        "normalized_value": row_data["values"].get(column_name),
                        "unit": self._infer_unit(column_name, row_data["values"].get(column_name)),
                        "value_type": self._value_type(row_data["values"].get(column_name)),
                        "formula": row_data["values"].get(column_name) if isinstance(row_data["values"].get(column_name), str) and row_data["values"][column_name].startswith("=") else None,
                        **row_data.get("cell_metadata", {}).get(column_name, {}),
                    },
                    provenance=Provenance(
                        document_id=document_id,
                        source_type=source_type,
                        version=version,
                        parent_node_id=row_node_id,
                        sheet_name=sheet_name,
                        cell_range=make_cell_range(row_data["row_index"], region["column_indexes"][column_index - 1], row_data["row_index"], region["column_indexes"][column_index - 1]),
                    ),
                    confidence=region["confidence"],
                )
            )

        return CanonicalNode(
            node_id=row_node_id,
            node_type=NodeType.TABLE_ROW,
            children=cell_nodes,
            attributes={
                "row_index": row_data["row_index"],
                "row_id": row_data["row_id"],
                "is_total_row": False,
                "is_header_repeat": False,
                "semantic_text": self._row_semantic_text(row_data["values"], row_data.get("cell_metadata", {})),
            },
            provenance=Provenance(
                document_id=document_id,
                source_type=source_type,
                version=version,
                parent_node_id=parent_node_id,
                sheet_name=sheet_name,
                cell_range=region["range"],
            ),
            confidence=region["confidence"],
        )

    # This function detects coarse worksheet regions so structured and narrative parts can branch separately.
    def _detect_regions(self, worksheet: Any) -> list[dict[str, Any]]:
        non_empty_rows = self._collect_non_empty_rows(worksheet)
        if not non_empty_rows:
            return []

        image_rows = self._worksheet_image_rows(worksheet)
        groups = self._group_consecutive_rows(non_empty_rows)
        regions: list[dict[str, Any]] = []
        for group in groups:
            rows = list(range(group[0], group[-1] + 1))
            sampled = [
                self._row_values(worksheet, row_index, worksheet.max_column)
                for row_index in rows[: min(len(rows), 12)]
            ]
            region = self._analyze_region(
                worksheet=worksheet,
                rows=rows,
                sampled_rows=sampled,
                max_column=worksheet.max_column,
                image_rows=image_rows,
            )
            regions.append(region)
        return regions

    # This function classifies a worksheet by looking at the mix of detected regions.
    def _classify_sheet(self, regions: list[dict[str, Any]]) -> str:
        if not regions:
            return "UNKNOWN"
        classes = {region["classification"] for region in regions}
        if len(classes) == 1:
            return classes.pop()
        return "MIXED"

    # This function summarizes parsing warnings that are worth surfacing in the document quality metadata.
    def _collect_workbook_warnings(self, workbook: Any, workbook_node: CanonicalNode) -> list[str]:
        warnings: list[str] = []
        for worksheet in workbook.worksheets:
            if worksheet.max_row > 5000:
                warnings.append(f"Large sheet detected: {worksheet.title}")
            if worksheet.sheet_state != "visible":
                warnings.append(f"Hidden sheet preserved: {worksheet.title}")
        for sheet_node in workbook_node.children:
            if sheet_node.node_type != NodeType.SHEET:
                continue
            image_count = int(sheet_node.attributes.get("embedded_image_count") or 0)
            extracted_count = int(sheet_node.attributes.get("extracted_image_count") or 0)
            formula_error_count = int(sheet_node.attributes.get("formula_error_count") or 0)
            image_only_ranges = sheet_node.attributes.get("image_only_row_ranges", [])
            image_heavy_count = int(sheet_node.attributes.get("image_heavy_region_count") or 0)
            if image_count:
                warnings.append(f"[excel_images] Sheet '{sheet_node.title}' has {image_count} embedded images; {extracted_count} image nodes created.")
            if image_count and extracted_count < image_count:
                warnings.append(f"[excel_image_extraction_gap] Sheet '{sheet_node.title}' has {image_count - extracted_count} embedded images without canonical image nodes.")
            if formula_error_count:
                warnings.append(f"[excel_formula_errors] Sheet '{sheet_node.title}' has {formula_error_count} formula error cells preserved for audit and excluded from retrieval text.")
            if image_only_ranges:
                warnings.append(f"[excel_image_only_rows] Sheet '{sheet_node.title}' has image-anchored rows outside detected cell regions: {', '.join(image_only_ranges[:8])}.")
            if image_heavy_count:
                warnings.append(f"[excel_image_heavy_regions] Sheet '{sheet_node.title}' has {image_heavy_count} image-heavy region(s); OCR/VLM enrichment may be needed.")
        return warnings

    # This function collects the row numbers that contain at least one non-empty cell.
    def _collect_non_empty_rows(self, worksheet: Any) -> list[int]:
        rows: list[int] = []
        for row_index in range(1, worksheet.max_row + 1):
            values = self._row_values(worksheet, row_index, worksheet.max_column)
            if any(value is not None and str(value).strip() for value in values):
                rows.append(row_index)
        return rows

    # This function groups nearby populated rows into coarse logical worksheet regions.
    def _group_consecutive_rows(self, row_numbers: Iterable[int]) -> list[list[int]]:
        groups: list[list[int]] = []
        current: list[int] = []
        for row_number in row_numbers:
            if not current or row_number - current[-1] <= 1:
                current.append(row_number)
            else:
                groups.append(current)
                current = [row_number]
        if current:
            groups.append(current)
        return groups

    # This function converts one worksheet row into a simple list of values for heuristic analysis.
    def _row_values(self, worksheet: Any, row_index: int, max_column: int) -> list[Any]:
        return [
            self._clean_cell_value(worksheet.cell(row=row_index, column=column_index).value)
            for column_index in range(1, max_column + 1)
        ]

    # This function infers whether a region is structured, document-like, or mixed from sampled rows.
    def _analyze_region(
        self,
        worksheet: Any,
        rows: list[int],
        sampled_rows: list[list[Any]],
        max_column: int,
        image_rows: list[int],
    ) -> dict[str, Any]:
        text_heavy_cells = 0
        numeric_cells = 0
        populated_cells = 0
        for row in sampled_rows:
            for value in row:
                if value is None or str(value).strip() == "":
                    continue
                populated_cells += 1
                if isinstance(value, (int, float)):
                    numeric_cells += 1
                elif isinstance(value, str) and len(value.strip()) > 24:
                    text_heavy_cells += 1

        total_cells = max(len(rows) * max_column, 1)
        cell_density = populated_cells / total_cells
        numeric_ratio = numeric_cells / populated_cells if populated_cells else 0.0
        text_ratio = text_heavy_cells / populated_cells if populated_cells else 0.0
        region_image_rows = [row for row in image_rows if rows[0] <= row <= rows[-1]]
        image_count = len(region_image_rows)

        if image_count and (cell_density < 0.08 or populated_cells <= 2):
            classification = "IMAGE_HEAVY"
        elif numeric_ratio > 0.25 or self._looks_like_table(sampled_rows):
            classification = "STRUCTURED" if text_ratio < 0.4 else "MIXED"
        else:
            classification = "DOCUMENT_LIKE"

        header_row_index = self._infer_header_row_index(sampled_rows)
        title = self._infer_title(sampled_rows, header_row_index)
        column_indexes = list(range(1, max_column + 1))
        columns = self._infer_columns(sampled_rows, header_row_index, column_indexes)
        header_rows = [sampled_rows[header_row_index]] if header_row_index is not None else []
        range_str = make_cell_range(rows[0], 1, rows[-1], max_column)
        row_payloads = self._materialize_rows(
            worksheet=worksheet,
            rows=rows,
            columns=columns,
            column_indexes=column_indexes,
            header_row_index=header_row_index,
        )

        return {
            "source_cells": [{"coordinate": worksheet.cell(row_number, column_number).coordinate, "value": self._clean_cell_value(worksheet.cell(row_number, column_number).value)} for row_number in rows for column_number in range(1, max_column + 1) if self._clean_cell_value(worksheet.cell(row_number, column_number).value) is not None],
            "row_numbers": rows,
            "kind": self._region_kind(classification),
            "classification": classification,
            "range": range_str,
            "header_depth": 1 if columns else 0,
            "title": title,
            "columns": columns,
            "column_indexes": column_indexes,
            "header_rows": header_rows,
            "rows": row_payloads,
            "has_numeric_data": numeric_ratio > 0.0,
            "units": self._infer_units(sampled_rows),
            "notes": self._extract_region_notes(worksheet, rows, title, header_row_index, classification),
            "confidence": 0.92 if classification != "DOCUMENT_LIKE" else 0.78,
            "table_name": self._table_name(title, columns),
            "populated_cell_count": populated_cells,
            "cell_density": round(cell_density, 4),
            "image_count": image_count,
            "image_anchor_rows": region_image_rows,
            "needs_vision_description": classification == "IMAGE_HEAVY",
        }

    def _region_kind(self, classification: str) -> str:
        """Maps region classifications to stable canonical region families."""
        if classification in {"STRUCTURED", "MIXED"}:
            return "STRUCTURED_TABLE"
        if classification == "IMAGE_HEAVY":
            return "IMAGE_HEAVY"
        return "TEXT_BLOCK"

    # This function checks whether sampled rows show enough repeated column structure to look tabular.
    def _looks_like_table(self, sampled_rows: list[list[Any]]) -> bool:
        populated_counts = []
        for row in sampled_rows:
            populated_counts.append(sum(1 for value in row if value not in (None, "")))
        if not populated_counts:
            return False
        return max(populated_counts) >= 3 and len(set(populated_counts[:4])) <= 3

    # This function infers a human-readable title from the first text-dominant sampled row.
    def _infer_title(self, sampled_rows: list[list[Any]], header_row_index: int | None) -> str | None:
        limit = header_row_index if header_row_index is not None else min(len(sampled_rows), 3)
        for row in sampled_rows[:limit]:
            text_values = [normalize_text(str(value)) for value in row if value not in (None, "")]
            if len(text_values) == 1:
                return text_values[0]
        return None

    # This function finds the most likely header row inside a sampled region.
    def _infer_header_row_index(self, sampled_rows: list[list[Any]]) -> int | None:
        for row_index, row in enumerate(sampled_rows):
            populated = [value for value in row if value not in (None, "")]
            text_cells = [value for value in populated if isinstance(value, str)]
            if len(populated) >= 2 and len(text_cells) >= max(1, len(populated) // 2):
                return row_index
        return None

    # This function records the real worksheet column positions used by the table header.
    def _infer_column_indexes(
        self,
        sampled_rows: list[list[Any]],
        header_row_index: int | None,
        max_column: int,
    ) -> list[int]:
        if header_row_index is None:
            return list(range(1, max_column + 1))
        row = sampled_rows[header_row_index]
        return [
            column_index
            for column_index, value in enumerate(row[:max_column], start=1)
            if value not in (None, "")
        ]

    # This function infers normalized column labels from the detected header row.
    def _infer_columns(
        self,
        sampled_rows: list[list[Any]],
        header_row_index: int | None,
        column_indexes: list[int],
    ) -> list[str]:
        if header_row_index is None:
            return [f"column_{index}" for index in column_indexes]
        row = sampled_rows[header_row_index]
        seen_names: dict[str, int] = {}
        columns: list[str] = []
        for fallback_index, column_index in enumerate(column_indexes, start=1):
            raw_value = row[column_index - 1] if column_index - 1 < len(row) else None
            base_name = self._sanitize_column_name(
                (normalize_text(str(raw_value)) if raw_value is not None else None) or f"column_{fallback_index}"
            )
            duplicate_count = seen_names.get(base_name, 0)
            seen_names[base_name] = duplicate_count + 1
            columns.append(base_name if duplicate_count == 0 else f"{base_name}_{duplicate_count + 1}")
        return columns

    # This function creates normalized row payloads from sampled region rows.
    def _materialize_rows(
        self,
        worksheet: Any,
        rows: list[int],
        columns: list[str],
        column_indexes: list[int],
        header_row_index: int | None,
    ) -> list[dict[str, Any]]:
        if not columns or not column_indexes:
            return []

        materialized: list[dict[str, Any]] = []
        data_start = (header_row_index + 1) if header_row_index is not None else 0
        for row_number in rows[data_start:]:
            row_values = self._row_values(worksheet, row_number, worksheet.max_column)
            selected_values = [
                row_values[column_index - 1] if column_index - 1 < len(row_values) else None
                for column_index in column_indexes
            ]
            if not any(value not in (None, "") for value in selected_values):
                continue
            metadata_map = {
                column_name: self._cell_metadata(worksheet.cell(row=row_number, column=column_indexes[column_offset]))
                for column_offset, column_name in enumerate(columns)
            }
            value_map = {
                column_name: selected_values[column_offset]
                for column_offset, column_name in enumerate(columns)
            }
            materialized.append(
                {
                    "row_index": row_number,
                    "row_id": f"row_{row_number}",
                    "values": value_map,
                    "cell_metadata": metadata_map,
                }
            )
        return materialized

    # This function extracts likely measurement units from sampled cell text.
    def _infer_units(self, sampled_rows: list[list[Any]]) -> list[str]:
        units = set()
        for row in sampled_rows:
            for value in row:
                if not isinstance(value, str):
                    continue
                lowered = value.lower()
                for candidate in ("mm", "cm", "kg", "%", "mpa", "°c"):
                    if candidate in lowered:
                        units.add(candidate)
        return sorted(units)

    # This function extracts note-like lines from a region when they are not acting as table headers.
    def _extract_region_notes(
        self,
        worksheet: Any,
        rows: list[int],
        title: str | None,
        header_row_index: int | None,
        classification: str,
    ) -> list[str]:
        if classification == "STRUCTURED":
            return []
        notes: list[str] = []
        note_start = 0 if classification == "DOCUMENT_LIKE" else ((header_row_index + 1) if header_row_index is not None else 0)
        for row_number in rows[note_start:]:
            row = self._row_values(worksheet, row_number, worksheet.max_column)
            joined = " ".join(
                normalize_text(str(value)) or ""
                for value in row
                if value not in (None, "")
            ).strip()
            if joined and joined != title:
                notes.append(joined)
        return notes

    # This function creates a normalized table name for structured regions.
    def _table_name(self, title: str | None, columns: list[str]) -> str:
        if title:
            return self._sanitize_column_name(title)
        if columns:
            return f"{columns[0]}_table"
        return "detected_table"

    # This function normalizes raw labels into SQL- and JSON-friendly field names.
    def _sanitize_column_name(self, label: str) -> str:
        cleaned = "".join(character if character.isalnum() else "_" for character in label.lower())
        parts = [part for part in cleaned.split("_") if part]
        return "_".join(parts) if parts else "column"

    # This function infers a rough unit from one cell value or column label.
    def _infer_unit(self, column_name: str, value: Any) -> str | None:
        raw_value = str(value).lower() if value is not None else ""
        raw_column = column_name.lower()
        for candidate in ("mm", "cm", "kg", "%", "mpa", "°c"):
            if candidate in raw_value or candidate in raw_column:
                return candidate
        return None

    # This function infers a simple semantic value type for one cell.
    def _value_type(self, value: Any) -> str:
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "float"
        if value is None:
            return "empty"
        return "string"

    # This function produces a readable row-level semantic sentence for hybrid retrieval support.
    def _row_semantic_text(self, row_values: dict[str, Any], cell_metadata: dict[str, dict[str, Any]] | None = None) -> str:
        parts = [
            f"{column_name.replace('_', ' ')} is {value}"
            for column_name, value in row_values.items()
            if value not in (None, "") and not (cell_metadata or {}).get(column_name, {}).get("formula_error")
        ]
        return "; ".join(parts)

    # This function creates canonical image nodes for embedded Excel worksheet images.
    def _build_image_nodes(
        self,
        worksheet: Any,
        document_id: str,
        source_type: SourceType,
        version: str,
        agent_id: str,
        parent_node_id: str,
        regions: list[dict[str, Any]],
    ) -> list[CanonicalNode]:
        nodes: list[CanonicalNode] = []
        for image_index, image in enumerate(getattr(worksheet, "_images", []) or [], start=1):
            metadata = self._excel_image_metadata(image, worksheet.title, image_index)
            metadata.update(self._image_context_metadata(worksheet, metadata, regions))
            blob = metadata.pop("_blob", None)
            if blob:
                saved = self._persist_excel_image(blob, metadata["extension"], agent_id, document_id, version,
                                                  worksheet.title, image_index, metadata["image_hash"])
                metadata["saved_path"] = saved.as_posix()
                metadata["image_storage_status"] = "stored"
            else:
                metadata["image_storage_status"] = "metadata_only"
            nodes.append(CanonicalNode(
                node_id=make_id("image"),
                node_type=NodeType.IMAGE,
                title=f"{worksheet.title} image {image_index}",
                attributes=metadata,
                provenance=Provenance(
                    document_id=document_id,
                    source_type=source_type,
                    version=version,
                    parent_node_id=parent_node_id,
                    sheet_name=worksheet.title,
                    cell_range=metadata.get("cell_range"),
                ),
                confidence=0.82 if metadata.get("saved_path") else 0.68,
            ))
        return nodes

    def _image_context_metadata(self, worksheet: Any, metadata: dict[str, Any], regions: list[dict[str, Any]]) -> dict[str, Any]:
        """Adds nearby source context so OCR/VLM image chunks are meaningful."""
        anchor_row = metadata.get("anchor_row")
        region = self._region_for_row(anchor_row, regions)
        needs_vision = bool(region and region.get("classification") == "IMAGE_HEAVY")
        return {
            "parent_region_range": region.get("range") if region else None,
            "parent_region_classification": region.get("classification") if region else None,
            "nearby_text": self._nearby_row_text(worksheet, anchor_row),
            "needs_vision_description": needs_vision,
            "vision_reason": "Image is anchored in an image-heavy Excel region." if needs_vision else "",
        }

    def _region_for_row(self, row_number: int | None, regions: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Finds the detected region containing an image anchor row."""
        if not row_number:
            return None
        return next((region for region in regions if row_number in region.get("row_numbers", [])), None)

    def _nearby_row_text(self, worksheet: Any, row_number: int | None, radius: int = 2) -> str:
        """Collects compact non-empty row context around an image anchor."""
        if not row_number:
            return ""
        parts: list[str] = []
        for row_index in range(max(1, row_number - radius), min(worksheet.max_row, row_number + radius) + 1):
            values = [
                normalize_text(str(value)) or ""
                for value in self._row_values(worksheet, row_index, worksheet.max_column)
                if value not in (None, "")
            ]
            if values:
                parts.append(f"Row {row_index}: {' | '.join(values)}")
        return "\n".join(parts)[:1200]

    # This function reads stable metadata and bytes from an openpyxl image object.
    def _excel_image_metadata(self, image: Any, sheet_name: str, image_index: int) -> dict[str, Any]:
        blob = self._image_bytes(image)
        extension = self._safe_extension(str(getattr(image, "format", "") or Path(str(getattr(image, "path", "image.png"))).suffix.lstrip(".") or "png"))
        anchor = getattr(image, "anchor", None)
        anchor_cell = self._anchor_cell(anchor)
        metadata = {
            "image_id": f"{self._safe_component(sheet_name)}_{image_index}",
            "image_type": extension,
            "extension": extension,
            "sheet_name": sheet_name,
            "image_index": image_index,
            "anchor_cell": anchor_cell,
            "anchor_row": self._anchor_row(anchor),
            "cell_range": self._anchor_range(anchor),
            "width": getattr(image, "width", None),
            "height": getattr(image, "height", None),
            "image_hash": hashlib.sha256(blob).hexdigest() if blob else None,
            "locator": anchor_cell or f"{sheet_name}!image_{image_index}",
            "_blob": blob,
        }
        return metadata

    # This function extracts image bytes from openpyxl while tolerating unsupported image objects.
    def _image_bytes(self, image: Any) -> bytes | None:
        try:
            data = image._data()
            return bytes(data) if data else None
        except Exception:
            return None

    # This function persists extracted workbook images beside other visual assets.
    def _persist_excel_image(self, blob: bytes, extension: str, agent_id: str, document_id: str, version: str,
                             sheet_name: str, image_index: int, image_hash: str | None) -> Path:
        folder = Path("storage") / "visual-assets" / self._safe_component(agent_id) / self._safe_component(document_id) / self._safe_component(version)
        folder.mkdir(parents=True, exist_ok=True)
        digest = (image_hash or hashlib.sha256(blob).hexdigest())[:12]
        target = folder / f"sheet_{self._safe_component(sheet_name)}_image_{image_index:03d}_{digest}.{extension}"
        target.write_bytes(blob)
        return target

    # This function returns the top-left anchor cell in one-based Excel coordinates.
    def _anchor_cell(self, anchor: Any) -> str | None:
        marker = getattr(anchor, "_from", None)
        if marker is None:
            return str(anchor) if isinstance(anchor, str) else None
        row = getattr(marker, "row", None)
        column = getattr(marker, "col", None)
        if type(row) is int and type(column) is int:
            return f"{get_column_letter(column + 1)}{row + 1}"
        return None

    # This function returns the top-left anchor row for image-gap quality checks.
    def _anchor_row(self, anchor: Any) -> int | None:
        marker = getattr(anchor, "_from", None)
        row = getattr(marker, "row", None) if marker is not None else None
        return row + 1 if type(row) is int else None

    # This function returns an approximate anchor range for one-cell or two-cell anchors.
    def _anchor_range(self, anchor: Any) -> str | None:
        start = getattr(anchor, "_from", None)
        end = getattr(anchor, "to", None) or getattr(anchor, "_to", None)
        if start is None:
            return self._anchor_cell(anchor)
        start_row, start_col = getattr(start, "row", None), getattr(start, "col", None)
        end_row, end_col = getattr(end, "row", None), getattr(end, "col", None)
        if type(start_row) is not int or type(start_col) is not int:
            return None
        if type(end_row) is not int or type(end_col) is not int:
            return make_cell_range(start_row + 1, start_col + 1, start_row + 1, start_col + 1)
        return make_cell_range(start_row + 1, start_col + 1, end_row + 1, end_col + 1)

    # This function flags Excel formula errors so retrieval text can skip them while audit preserves them.
    def _cell_metadata(self, cell: Any) -> dict[str, Any]:
        value = cell.value
        formula_error = getattr(cell, "data_type", None) == "e" or value in {"#VALUE!", "#DIV/0!", "#N/A", "#REF!", "#NAME?", "#NUM!", "#NULL!"}
        return {"formula_error": True, "retrieval_allowed": False} if formula_error else {}

    # This function counts formula error cells per worksheet for top-level warnings.
    def _sheet_formula_error_count(self, worksheet: Any) -> int:
        return sum(
            1
            for row in worksheet.iter_rows()
            for cell in row
            if self._cell_metadata(cell).get("formula_error")
        )

    # This function identifies image rows not covered by detected populated-cell regions.
    def _image_only_row_ranges(self, image_rows: list[int | None], regions: list[dict[str, Any]]) -> list[str]:
        covered = {row for region in regions for row in region.get("row_numbers", [])}
        rows = sorted({row for row in image_rows if row and row not in covered})
        return [self._format_row_range(group) for group in self._group_consecutive_rows(rows)]

    def _worksheet_image_rows(self, worksheet: Any) -> list[int]:
        """Returns one-based anchor rows for images embedded in a worksheet."""
        return [
            row
            for row in (
                self._anchor_row(getattr(image, "anchor", None))
                for image in getattr(worksheet, "_images", []) or []
            )
            if row
        ]

    # This function formats consecutive row groups for quality warnings.
    def _format_row_range(self, rows: list[int]) -> str:
        return f"{rows[0]}" if len(rows) == 1 else f"{rows[0]}-{rows[-1]}"

    # This function avoids converting Python None into the literal string "None".
    def _cell_text(self, value: Any) -> str | None:
        cleaned = self._clean_cell_value(value)
        return normalize_text(str(cleaned)) if cleaned not in (None, "") else None

    # This function treats exact Excel placeholder strings as blanks without hiding real errors.
    def _clean_cell_value(self, value: Any) -> Any:
        if isinstance(value, str) and (normalize_text(value) or "").casefold() == "none":
            return None
        return value

    # This function creates path-safe deterministic asset names.
    def _safe_component(self, value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
        return cleaned or "unknown"

    # This function keeps image extensions simple and path-safe.
    def _safe_extension(self, value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9]+", "", value.lower())
        return cleaned or "png"

    # This function formats the workbook's used range in A1 notation when the sheet is non-empty.
    def _used_range(self, worksheet: Any) -> str | None:
        if worksheet.max_row == 1 and worksheet.max_column == 1:
            if worksheet.cell(1, 1).value in (None, ""):
                return None
        return make_cell_range(1, 1, worksheet.max_row, worksheet.max_column)
