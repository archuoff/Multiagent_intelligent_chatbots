"""Source parsers that assemble canonical PDF and DOCX documents.

This file uses Docling as the primary extractor for PDF and DOCX files and
falls back to lightweight local libraries when Docling is unavailable.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
import traceback
from typing import Any
import zipfile
from xml.etree import ElementTree

from backend.ingestion.extraction.contracts import ExtractionResult
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
from backend.ingestion.parsers.base import IngestionSource
from backend.ingestion.parsers.base import ParserContext
from backend.ingestion.parsers.base import SourceParser
from backend.ingestion.utils import hash_file
from backend.ingestion.utils import make_id
from backend.ingestion.utils import normalize_text
from backend.ingestion.extraction.docling import DoclingContentAdapter
from backend.ingestion.extraction.docx_renderer import WordComDocxRenderer
from backend.ingestion.extraction.fallbacks import PdfFallbackExtractor
from backend.ingestion.extraction.fallbacks import DocxFallbackExtractor
from backend.ingestion.extraction.raw_text_index import PdfRawTextIndex
from backend.ingestion.extraction.bbox_calibration import audit_axis_agreement
from backend.ingestion.extraction.pdf_inspector import PdfInspector
from backend.ingestion.extraction.merger import ExtractionResultMerger




@dataclass(slots=True)
class _RenderedWord:
    """One word from a rendered DOCX PDF used for best-effort layout alignment."""

    page_number: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str


class DoclingCanonicalBuilder:
    """Builds canonical nodes from Docling exported payloads without flattening structure early."""

    # This function builds page-oriented nodes from Docling exported payloads for PDF-like sources.
    def build_page_nodes(
        self,
        *,
        document_id: str,
        source_type: SourceType,
        version: str,
        parent_node_id: str,
        page_node_type: NodeType,
        exported: dict[str, Any],
        markdown: str,
    ) -> list[CanonicalNode]:
        page_sections = self._find_page_sections(exported)
        if not page_sections:
            page_sections = [
                {"page_number": index, "text": text}
                for index, text in enumerate(self._split_markdown_sections(markdown), start=1)
            ]

        page_nodes: list[CanonicalNode] = []
        for section in page_sections:
            page_number = section["page_number"]
            text = section["text"]
            page_node_id = make_id("page")
            page_nodes.append(
                CanonicalNode(
                    node_id=page_node_id,
                    node_type=page_node_type,
                    title=self._infer_title(text, f"Page {page_number}"),
                    children=self._text_to_nodes(
                        document_id=document_id,
                        source_type=source_type,
                        version=version,
                        parent_node_id=page_node_id,
                        location_key="page_number",
                        location_value=page_number,
                        text=text,
                    ),
                    attributes={"page_number": page_number, "docling_enabled": True},
                    provenance=self._make_provenance(
                        document_id=document_id,
                        source_type=source_type,
                        version=version,
                        parent_node_id=parent_node_id,
                        page_number=page_number,
                    ),
                    confidence=0.9 if normalize_text(text) else 0.45,
                )
            )
        return page_nodes

    # This function builds section-oriented nodes from Docling exported payloads for DOCX-like sources.
    def build_section_nodes(
        self,
        *,
        document_id: str,
        source_type: SourceType,
        version: str,
        parent_node_id: str,
        exported: dict[str, Any],
        markdown: str,
    ) -> list[CanonicalNode]:
        if exported.get("body", {}).get("children"):
            return self._ordered_docling_nodes(document_id, source_type, version, parent_node_id, exported)
        blocks = self._extract_text_blocks(exported)
        if not blocks:
            blocks = self._split_markdown_blocks(markdown)

        children: list[CanonicalNode] = []
        current_section: CanonicalNode | None = None
        for block in blocks:
            if self._is_heading(block):
                current_section = CanonicalNode(
                    node_id=make_id("section"),
                    node_type=NodeType.SECTION,
                    title=self._strip_heading(block),
                    children=[],
                    attributes={"docling_enabled": True},
                    provenance=self._make_provenance(
                        document_id=document_id,
                        source_type=source_type,
                        version=version,
                        parent_node_id=parent_node_id,
                    ),
                    confidence=0.9,
                )
                children.append(current_section)
                continue

            paragraph_node = CanonicalNode(
                node_id=make_id("paragraph"),
                node_type=NodeType.BULLET_BLOCK if self._is_bullet(block) else NodeType.PARAGRAPH,
                text=self._strip_bullet(block),
                attributes={"docling_enabled": True},
                provenance=self._make_provenance(
                    document_id=document_id,
                    source_type=source_type,
                    version=version,
                    parent_node_id=current_section.node_id if current_section else parent_node_id,
                ),
                confidence=0.86,
            )
            if current_section:
                current_section.children.append(paragraph_node)
            else:
                children.append(paragraph_node)
        return children

    def _ordered_docling_nodes(self, document_id: str, source_type: SourceType, version: str, parent_node_id: str, exported: dict[str, Any]) -> list[CanonicalNode]:
        """Traverses Docling references in source order without global text deduplication."""
        result: list[CanonicalNode] = []
        seen: set[str] = set()

        def visit(reference: dict[str, Any], destination: list[CanonicalNode], parent: str) -> None:
            """Resolves one source reference and preserves its nested content."""
            ref = reference.get("$ref", "")
            if ref in seen:
                return
            seen.add(ref)
            item: Any = exported
            for part in ref.removeprefix("#/").split("/"):
                item = item[int(part)] if isinstance(item, list) else item[part]
            label = item.get("label", "")
            node_id = make_id("node")
            provenance = Provenance(document_id=document_id, source_type=source_type, version=version, parent_node_id=parent)
            if label == "table":
                node = self.build_matrix_table_node(document_id=document_id, source_type=source_type, version=version, parent_node_id=parent, table_index=len(seen), rows={"source_table": item})
                node.attributes["source_ref"] = ref
                destination.append(node)
                return
            kind = {"section_header": NodeType.SECTION, "title": NodeType.TITLE_BLOCK, "list_item": NodeType.BULLET_BLOCK, "picture": NodeType.IMAGE, "caption": NodeType.CAPTION}.get(label, NodeType.PARAGRAPH if item.get("text") else NodeType.REGION)
            node = CanonicalNode(node_id=node_id, node_type=kind, text=item.get("text"), title=item.get("text") if kind in {NodeType.SECTION, NodeType.TITLE_BLOCK} else None, attributes={"source_ref": ref, "source_label": label, "raw_provenance": item.get("prov", [])}, provenance=provenance)
            destination.append(node)
            for child in item.get("children", []):
                visit(child, node.children, node_id)

        for reference in exported["body"]["children"]:
            visit(reference, result, parent_node_id)
        structured: list[CanonicalNode] = []
        current_section: CanonicalNode | None = None
        for node in result:
            if node.node_type == NodeType.SECTION:
                current_section = node
                structured.append(node)
            elif current_section is not None:
                node.provenance.parent_node_id = current_section.node_id
                current_section.children.append(node)
            else:
                structured.append(node)
        return structured

    # This function builds canonical table nodes from row matrices found in Docling exported payloads.
    def build_table_nodes(
        self,
        *,
        document_id: str,
        source_type: SourceType,
        version: str,
        parent_node_id: str,
        exported: dict[str, Any],
    ) -> list[CanonicalNode]:
        tables = [{"source_table": table} for table in exported.get("tables", []) if isinstance(table, dict)]
        if not tables:
            tables = self._find_table_candidates(exported)
        table_nodes: list[CanonicalNode] = []
        for table_index, rows in enumerate(tables, start=1):
            if not rows:
                continue
            table_nodes.append(
                self.build_matrix_table_node(
                    document_id=document_id,
                    source_type=source_type,
                    version=version,
                    parent_node_id=parent_node_id,
                    table_index=table_index,
                    rows=rows,
                )
            )
        return table_nodes

    # This function builds table nodes from a simple matrix while preserving row-level semantics.
    def build_matrix_table_node(
        self,
        *,
        document_id: str,
        source_type: SourceType,
        version: str,
        parent_node_id: str,
        table_index: int,
        rows: list[list[str]] | dict[str, Any],
        slide_number: int | None = None,
    ) -> CanonicalNode:
        if isinstance(rows, dict):
            return self.build_structured_table_node(document_id=document_id, source_type=source_type, version=version,
                parent_node_id=parent_node_id, table_index=table_index, table=rows, slide_number=slide_number)
        table_node_id = make_id("table")
        header_rows = [rows[0]] if rows else []
        columns = self._normalize_column_names(header_rows[0]) if header_rows else []

        row_nodes: list[CanonicalNode] = []
        for row_offset, values in enumerate(rows[1:], start=2):
            row_node_id = make_id("row")
            cell_nodes = [
                CanonicalNode(
                    node_id=make_id("cell"),
                    node_type=NodeType.TABLE_CELL,
                    text=value,
                    attributes={
                        "row_index": row_offset,
                        "column_index": column_index,
                        "column_name": columns[column_index - 1] if column_index - 1 < len(columns) else f"column_{column_index}",
                        "value": value,
                        "normalized_value": value,
                        "unit": None,
                        "value_type": "string",
                        "formula": None,
                    },
                    provenance=self._make_provenance(
                        document_id=document_id,
                        source_type=source_type,
                        version=version,
                        parent_node_id=row_node_id,
                        slide_number=slide_number,
                    ),
                    confidence=0.88,
                )
                for column_index, value in enumerate(values, start=1)
            ]
            row_nodes.append(
                CanonicalNode(
                    node_id=row_node_id,
                    node_type=NodeType.TABLE_ROW,
                    children=cell_nodes,
                    attributes={
                        "row_index": row_offset,
                        "row_id": f"table_{table_index}_row_{row_offset}",
                        "is_total_row": False,
                        "is_header_repeat": False,
                        "semantic_text": self._row_semantic_text(columns, values),
                    },
                    provenance=self._make_provenance(
                        document_id=document_id,
                        source_type=source_type,
                        version=version,
                        parent_node_id=table_node_id,
                        slide_number=slide_number,
                    ),
                    confidence=0.88,
                )
            )

        header_node = CanonicalNode(
            node_id=make_id("header"),
            node_type=NodeType.TABLE_HEADER,
            children=[
                CanonicalNode(
                    node_id=make_id("cell"),
                    node_type=NodeType.TABLE_CELL,
                    text=value,
                    attributes={
                        "row_index": 1,
                        "column_index": column_index,
                        "column_name": columns[column_index - 1] if column_index - 1 < len(columns) else f"column_{column_index}",
                        "value": value,
                        "normalized_value": value,
                        "unit": None,
                        "value_type": "string",
                        "formula": None,
                        "is_header": True,
                    },
                    provenance=self._make_provenance(
                        document_id=document_id,
                        source_type=source_type,
                        version=version,
                        parent_node_id=table_node_id,
                        slide_number=slide_number,
                    ),
                    confidence=0.9,
                )
                for column_index, value in enumerate(header_rows[0], start=1)
            ] if header_rows else [],
            attributes={"header_rows": header_rows},
            provenance=self._make_provenance(
                document_id=document_id,
                source_type=source_type,
                version=version,
                parent_node_id=table_node_id,
                slide_number=slide_number,
            ),
            confidence=0.9,
        )

        return CanonicalNode(
            node_id=table_node_id,
            node_type=NodeType.TABLE,
            title=f"Table {table_index}",
            children=[header_node, *row_nodes],
            attributes={
                "table_name": f"table_{table_index}",
                "header_depth": 1 if header_rows else 0,
                "column_count": len(columns),
                "has_numeric_data": False,
                "is_nested": False,
                "units": [],
                "surrounding_text": {"text_above": None, "text_below": None},
                "columns": columns,
                "docling_enabled": True,
            },
            provenance=self._make_provenance(
                document_id=document_id,
                source_type=source_type,
                version=version,
                parent_node_id=parent_node_id,
                slide_number=slide_number,
            ),
            confidence=0.88,
        )

    def build_structured_table_node(self, *, document_id: str, source_type: SourceType, version: str,
                                    parent_node_id: str, table_index: int, table: dict[str, Any],
                                    slide_number: int | None = None) -> CanonicalNode:
        """Maps source cells directly to canonical cells without expanding merged spans."""
        payload = table.get("source_table", {})
        data = payload.get("data", {})
        cells = data.get("table_cells", []) if isinstance(data, dict) else []
        if not isinstance(cells, list):
            cells = []
        if not cells and table.get("matrix"):
            node = self.build_matrix_table_node(document_id=document_id, source_type=source_type, version=version,
                parent_node_id=parent_node_id, table_index=table_index, rows=table["matrix"], slide_number=slide_number)
            node.attributes.update(source_table=payload, table_warnings=table.get("warnings", []),
                                   reconciliation=table.get("reconciliation"))
            return node
        table_id = make_id("table")
        def provenance(parent: str) -> Provenance:
            """Attaches canonical ownership while retaining raw locations separately."""
            return self._make_provenance(document_id=document_id, source_type=source_type, version=version,
                parent_node_id=parent, slide_number=slide_number)
        rows: dict[int, CanonicalNode] = {}
        invalid = not cells or bool(table.get("requires_review"))
        for cell in cells:
            if not isinstance(cell, dict):
                invalid = True
                continue
            row = cell.get("start_row_offset_idx")
            column = cell.get("start_col_offset_idx")
            if type(row) is not int or type(column) is not int or row < 0 or column < 0:
                invalid = True
                continue
            for start, end_key, span_key, count_key in (
                (row, "end_row_offset_idx", "row_span", "num_rows"),
                (column, "end_col_offset_idx", "col_span", "num_cols"),
            ):
                end = cell.get(end_key)
                span = cell.get(span_key, 1)
                count = data.get(count_key)
                if (type(end) is not int or type(span) is not int or span < 1 or end != start + span
                        or type(count) is not int or end > count):
                    invalid = True
            if cell.get("ref"):
                # Rich cells need reference resolution before their text is safe to index.
                invalid = True
            if row not in rows:
                rows[row] = CanonicalNode(node_id=make_id("row"), node_type=NodeType.TABLE_ROW,
                    attributes={"row_index": row + 1}, provenance=provenance(table_id))
            parent = rows[row]
            parent.children.append(CanonicalNode(node_id=make_id("cell"), node_type=NodeType.TABLE_CELL,
                text=str(cell.get("text", "")), attributes={**cell, "row_index": row + 1,
                    "column_index": column + 1, "value": cell.get("text", "")}, provenance=provenance(parent.node_id)))
        for row in rows.values():
            row.children.sort(key=lambda cell: cell.attributes["column_index"])
            row.attributes["semantic_text"] = " | ".join(cell.text or "" for cell in row.children)
        return CanonicalNode(node_id=table_id, node_type=NodeType.TABLE, title=f"Table {table_index}",
            children=[rows[index] for index in sorted(rows)], provenance=provenance(parent_node_id),
            attributes={"representation": "structured_cells", "source_table": payload,
                "raw_provenance": payload.get("prov", []), "column_count": data.get("num_cols") if isinstance(data, dict) else None,
                "table_warnings": table.get("warnings", []), "requires_review": invalid,
                "reconciliation": table.get("reconciliation")})

    # This function extracts page sections from exported Docling payloads.
    def _find_page_sections(self, payload: Any) -> list[dict[str, Any]]:
        page_payloads = self._find_payload_list(payload, "pages")
        sections: list[dict[str, Any]] = []
        for index, page_payload in enumerate(page_payloads, start=1):
            text = "\n".join(self._extract_text_fragments(page_payload))
            if normalize_text(text):
                sections.append({"page_number": index, "text": text})
        return sections

    # This function extracts paragraph-like text blocks from exported Docling payloads.
    def _extract_text_blocks(self, payload: Any) -> list[str]:
        blocks = self._extract_text_fragments(payload)
        return [block for block in blocks if normalize_text(block)]

    # This function extracts text fragments recursively from exported Docling payloads.
    def _extract_text_fragments(self, payload: Any) -> list[str]:
        fragments: list[str] = []
        if isinstance(payload, dict):
            for key in ("text",):
                value = payload.get(key)
                if isinstance(value, str):
                    normalized = normalize_text(value)
                    if normalized:
                        fragments.append(normalized)
            for value in payload.values():
                fragments.extend(self._extract_text_fragments(value))
        elif isinstance(payload, list):
            for item in payload:
                fragments.extend(self._extract_text_fragments(item))
        return fragments

    # This function finds all payload lists stored under a given key in the exported structure.
    def _find_payload_list(self, payload: Any, key_name: str) -> list[Any]:
        found: list[Any] = []
        if isinstance(payload, dict):
            value = payload.get(key_name)
            if isinstance(value, list):
                found.extend(value)
            for child in payload.values():
                found.extend(self._find_payload_list(child, key_name))
        elif isinstance(payload, list):
            for item in payload:
                found.extend(self._find_payload_list(item, key_name))
        return found

    # This function searches the exported Docling dictionary for simple table row matrices.
    def _find_table_candidates(self, payload: Any) -> list[list[list[str]]]:
        tables: list[list[list[str]]] = []
        if isinstance(payload, dict):
            for key in ("data", "rows", "grid"):
                value = payload.get(key)
                if isinstance(value, list):
                    matrix = self._normalize_table_matrix(value)
                    if matrix:
                        tables.append(matrix)
            for value in payload.values():
                tables.extend(self._find_table_candidates(value))
        elif isinstance(payload, list):
            for item in payload:
                tables.extend(self._find_table_candidates(item))
        return tables

    # This function normalizes a table-like payload into a pure string matrix when possible.
    def _normalize_table_matrix(self, data: list[Any]) -> list[list[str]]:
        matrix: list[list[str]] = []
        for row in data:
            if isinstance(row, dict):
                row = row.get("cells") or row.get("data") or row.get("values")
            if not isinstance(row, list):
                return []
            normalized_row: list[str] = []
            for cell in row:
                if isinstance(cell, dict):
                    normalized_row.append(normalize_text(str(cell.get("text") or cell.get("content") or "")) or "")
                else:
                    normalized_row.append(normalize_text(str(cell)) or "")
            matrix.append(normalized_row)
        return matrix

    # This function converts extracted text into title, bullet, and paragraph nodes while preserving line boundaries.
    def _text_to_nodes(
        self,
        *,
        document_id: str,
        source_type: SourceType,
        version: str,
        parent_node_id: str,
        location_key: str,
        location_value: int,
        text: str,
    ) -> list[CanonicalNode]:
        lines = [line.strip() for line in text.splitlines() if normalize_text(line)]
        nodes: list[CanonicalNode] = []
        paragraph_buffer: list[str] = []

        def flush_paragraph() -> None:
            if not paragraph_buffer:
                return
            paragraph_text = normalize_text(" ".join(paragraph_buffer))
            if paragraph_text:
                nodes.append(
                    CanonicalNode(
                        node_id=make_id("paragraph"),
                        node_type=NodeType.PARAGRAPH,
                        text=paragraph_text,
                        provenance=self._make_provenance(
                            document_id=document_id,
                            source_type=source_type,
                            version=version,
                            parent_node_id=parent_node_id,
                            **{location_key: location_value},
                        ),
                        confidence=0.86,
                    )
                )
            paragraph_buffer.clear()

        for line in lines:
            if self._is_heading(line):
                flush_paragraph()
                stripped = self._strip_heading(line)
                nodes.append(
                    CanonicalNode(
                        node_id=make_id("title"),
                        node_type=NodeType.TITLE_BLOCK,
                        title=stripped,
                        text=stripped,
                        provenance=self._make_provenance(
                            document_id=document_id,
                            source_type=source_type,
                            version=version,
                            parent_node_id=parent_node_id,
                            **{location_key: location_value},
                        ),
                        confidence=0.82,
                    )
                )
            elif self._is_bullet(line):
                flush_paragraph()
                bullet_text = self._strip_bullet(line)
                if bullet_text:
                    nodes.append(
                        CanonicalNode(
                            node_id=make_id("bullet"),
                            node_type=NodeType.BULLET_BLOCK,
                            text=bullet_text,
                            provenance=self._make_provenance(
                                document_id=document_id,
                                source_type=source_type,
                                version=version,
                                parent_node_id=parent_node_id,
                                **{location_key: location_value},
                            ),
                            confidence=0.84,
                        )
                    )
            else:
                paragraph_buffer.append(line)

        flush_paragraph()
        return nodes

    # This function normalizes column names and keeps duplicates distinct.
    def _normalize_column_names(self, header_row: list[str]) -> list[str]:
        seen_names: dict[str, int] = {}
        columns: list[str] = []
        for index, value in enumerate(header_row, start=1):
            base_name = self._sanitize_column_name(value or f"column_{index}")
            duplicate_count = seen_names.get(base_name, 0)
            seen_names[base_name] = duplicate_count + 1
            columns.append(base_name if duplicate_count == 0 else f"{base_name}_{duplicate_count + 1}")
        return columns

    # This function converts one row into a readable semantic sentence.
    def _row_semantic_text(self, columns: list[str], values: list[str]) -> str:
        parts = []
        for column_name, value in zip(columns, values, strict=False):
            if value:
                parts.append(f"{column_name.replace('_', ' ')} is {value}")
        return "; ".join(parts)

    # This function builds a provenance object while allowing optional page or slide location.
    def _make_provenance(
        self,
        *,
        document_id: str,
        source_type: SourceType,
        version: str,
        parent_node_id: str,
        page_number: int | None = None,
        slide_number: int | None = None,
    ) -> Provenance:
        return Provenance(
            document_id=document_id,
            source_type=source_type,
            version=version,
            parent_node_id=parent_node_id,
            page_number=page_number,
            slide_number=slide_number,
        )

    # This function identifies heading-like lines using markdown or text-shape heuristics.
    def _is_heading(self, line: str) -> bool:
        normalized = normalize_text(line) or ""
        words = normalized.lstrip("# ").split()
        if not words or len(words) > 12:
            return False
        if normalized.startswith("#") or normalized.endswith(":"):
            return True
        uppercase_ratio = sum(1 for character in normalized if character.isupper()) / max(len(normalized), 1)
        return uppercase_ratio > 0.45

    # This function removes heading markers from a heading line.
    def _strip_heading(self, line: str) -> str:
        return normalize_text(line.lstrip("# ").strip()) or line

    # This function identifies bullet-style lines from extracted text.
    def _is_bullet(self, line: str) -> bool:
        normalized = normalize_text(line) or ""
        return normalized.startswith(("-", "*", "o ", ">"))

    # This function removes a leading bullet marker while preserving semantic content.
    def _strip_bullet(self, line: str) -> str:
        normalized = normalize_text(line) or ""
        for marker in ("- ", "* ", "> ", "o "):
            if normalized.startswith(marker):
                return normalized[len(marker):].strip()
        return "" if normalized in {"-", "*", ">"} else normalized

    # This function infers a usable title from the first heading-like line.
    def _infer_title(self, text: str, fallback: str) -> str:
        for line in text.splitlines():
            normalized = normalize_text(line)
            if normalized and self._is_heading(normalized):
                return self._strip_heading(normalized)
        return fallback

    # This function splits markdown into section-sized groups without flattening line breaks.
    def _split_markdown_sections(self, markdown: str) -> list[str]:
        if not markdown:
            return []
        sections: list[str] = []
        current: list[str] = []
        for line in markdown.replace("\r\n", "\n").split("\n"):
            if line.startswith("#") and current:
                sections.append("\n".join(current).strip())
                current = [line]
            else:
                current.append(line)
        if current:
            sections.append("\n".join(current).strip())
        return [section for section in sections if normalize_text(section)]

    # This function splits markdown into paragraph-like blocks for DOCX normalization fallback.
    def _split_markdown_blocks(self, markdown: str) -> list[str]:
        if not markdown:
            return []
        blocks = [block.strip() for block in markdown.replace("\r\n", "\n").split("\n\n")]
        return [block for block in blocks if normalize_text(block)]

    # This function removes duplicate extracted blocks while preserving order.
    def _dedupe_preserve_order(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            ordered.append(value)
        return ordered

    # This function normalizes raw labels into SQL- and JSON-friendly names.
    def _sanitize_column_name(self, label: str) -> str:
        cleaned = "".join(character if character.isalnum() else "_" for character in label.lower())
        parts = [part for part in cleaned.split("_") if part]
        return "_".join(parts) if parts else "column"


class PdfDocumentParser(SourceParser):
    """Parses PDF files into page and text block nodes."""

    source_types = (SourceType.PDF,)

    def __init__(self) -> None:
        self._docling = DoclingContentAdapter()
        self._fallback = PdfFallbackExtractor()
        self._inspector = PdfInspector()
        self._merger = ExtractionResultMerger()
        self._builder = DoclingCanonicalBuilder()

    # This function loads a PDF file and converts each page into canonical content nodes.
    def parse(self, source: IngestionSource, context: ParserContext) -> CanonicalDocument:
        document_id = source.document_id or make_id("doc")
        root_node_id = make_id("document")
        docling_used = False
        extraction_mode = "fallback_library"
        parser_name = "pdf_pypdf_fallback_parser"
        processing_details: dict[str, Any] = {}
        requires_review = False
        inspection = self._inspector.inspect(source.file_path)
        if self._docling.is_available() and (inspection.has_usable_embedded_text or os.getenv("JLR_DOCLING_OCR", "false").lower() == "true"):
            try:
                extracted = self._docling.extract(source.file_path, page_count=inspection.page_count)
                warnings = [*extracted.warnings, *self._collect_docling_warnings(extracted.exported, extracted.markdown, source.source_type)]
                missing_pages = set(range(1, inspection.page_count + 1)) - {page.number for page in extracted.pages}
                if missing_pages:
                    warnings.append(f"Docling omitted {len(missing_pages)} source pages; review recovery output.")
                if missing_pages or extracted.requires_review:
                    fallback = self._fallback.extract(source.file_path)
                    raw_text_index = self._build_raw_text_index(source.file_path, extracted, inspection, warnings)
                    extracted = self._merger.merge(extracted, fallback, raw_text_index=raw_text_index)
                    if raw_text_index is not None:
                        if raw_text_index.engine_used and raw_text_index.engine_used == fallback.extractor_name:
                            warnings.append(
                                "[verifier_shared_engine] Raw-text position verification used the same "
                                "library as the fallback extraction for this document; resolutions "
                                "favoring the fallback are capped at medium confidence."
                            )
                        raw_text_index.close()
                    warnings = [
                        "Docling required local PDF fallback recovery for missing or review-needed pages.",
                        *warnings,
                        *fallback.warnings,
                        *extracted.warnings,
                    ]
                else:
                    warnings = [*warnings, *extracted.warnings]
                processing_details = extracted.processing_details
                requires_review = extracted.requires_review
                page_nodes = self._build_fallback_pages(document_id, root_node_id, source, extracted)
                docling_used = True
                extraction_mode = extracted.extraction_mode
                parser_name = "pdf_docling_parser"
            except Exception as error:
                if os.getenv("JLR_DOCLING_DEBUG_ERRORS", "false").lower() == "true":
                    print("[docling] PDF extraction failed with full traceback:", flush=True)
                    traceback.print_exc()
                fallback = self._fallback.extract(source.file_path)
                page_nodes = self._build_fallback_pages(document_id, root_node_id, source, fallback)
                parser_name = f"pdf_{fallback.extractor_name}_fallback_parser"
                warnings = [
                    f"Docling PDF extraction failed; {fallback.extractor_name} fallback used: {type(error).__name__}: {error}",
                    *fallback.warnings,
                ]
        else:
            fallback = self._fallback.extract(source.file_path)
            page_nodes = self._build_fallback_pages(document_id, root_node_id, source, fallback)
            parser_name = f"pdf_{fallback.extractor_name}_fallback_parser"
            warnings = [
                "Scanned or low-text PDF detected; OCR is deferred until a local OCR model policy is configured.",
                *fallback.warnings,
            ]
        suspicious_numeric_count = self._count_nodes_with_attribute(page_nodes, "suspicious_numeric")
        if suspicious_numeric_count:
            warnings.append(f"[pdf_suspicious_numeric] {suspicious_numeric_count} PDF node(s) were excluded from retrieval due to possible numeric/exponent corruption.")
        low_confidence_count = sum(node.attributes.get("low_confidence_block_count", 0) for node in page_nodes)
        if low_confidence_count:
            warnings.append(
                f"[extraction_confidence] {low_confidence_count} PDF text block(s) could not be verified "
                "against the source (unresolved conflict, noise heuristic, or position mismatch) and were "
                "excluded from retrieval; retained in Master JSON for review."
            )

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
                parser_name=parser_name,
                parser_version=context.parser_version,
                extraction_mode=extraction_mode,
                processing_details=processing_details,
            ),
            metadata=DocumentMetadata(
                title=source.file_path.stem,
                document_family=DocumentFamily.PAGE_CENTRIC,
                page_count=len(page_nodes) or None,
            ),
            root_nodes=[
                CanonicalNode(
                    node_id=root_node_id,
                    node_type=NodeType.DOCUMENT_ROOT,
                    title=source.file_path.stem,
                    children=page_nodes,
                    attributes={"page_count": len(page_nodes), "docling_enabled": docling_used},
                    provenance=Provenance(
                        document_id=document_id,
                        source_type=source.source_type,
                        version=source.version,
                    ),
                    confidence=0.97,
                )
            ],
            quality=QualityMetadata(
                overall_confidence=0.84 if not warnings else 0.76,
                warnings=warnings,
                errors=(["[reconciliation_conflict] Primary and fallback extraction disagree; review retained evidence before chunking."]
                        if processing_details.get("reconciliation", {}).get("summary", {}).get("conflict")
                        else ["[incomplete_extraction] Docling did not complete successfully; review the preserved output before chunking."])
                if requires_review else [],
            ),
        )

    def _count_nodes_with_attribute(self, nodes: list[CanonicalNode], attribute_name: str) -> int:
        """Counts canonical nodes carrying a specific quality/safety flag."""
        return sum(
            (1 if node.attributes.get(attribute_name) else 0) + self._count_nodes_with_attribute(node.children, attribute_name)
            for node in nodes
        )

    def _build_raw_text_index(self, source_path, extracted, inspection, warnings: list[str]) -> PdfRawTextIndex | None:
        """Builds the PDF raw-text referee, or explains why it isn't available.

        Never trusted blindly: a per-document calibration audit checks that
        the bboxes we already normalized actually land on their own text
        before the referee is handed to the merger, so a mis-aimed referee
        degrades to "unavailable" instead of silently verifying against the
        wrong region of the page.
        """
        if not inspection.has_usable_embedded_text:
            warnings.append(
                "[verifier_unavailable] PDF has no usable embedded text layer; "
                "raw-text position verification is unavailable."
            )
            return None
        try:
            raw_text_index = PdfRawTextIndex(source_path, engine="auto")
        except Exception as error:
            warnings.append(f"[verifier_unavailable] PDF raw-text position verification could not be initialized: {type(error).__name__}.")
            return None
        try:
            calibration = audit_axis_agreement(extracted.pages, raw_text_index)
        except Exception as error:
            warnings.append(f"[verifier_unavailable] PDF raw-text position calibration failed: {type(error).__name__}.")
            raw_text_index.close()
            return None
        if not calibration["trust_verification"]:
            warnings.append(
                f"[bbox_axis_mismatch] Disabling PDF raw-text position verification for this "
                f"document: {calibration['low_overlap']} of {calibration['checked']} sampled "
                f"text blocks did not match their own claimed position in the source text layer."
            )
            raw_text_index.close()
            return None
        return raw_text_index

    # This function builds fallback page nodes from raw pypdf extraction results.
    def _build_fallback_pages(
        self,
        document_id: str,
        root_node_id: str,
        source: IngestionSource,
        extraction: ExtractionResult,
    ) -> list[CanonicalNode]:
        page_nodes: list[CanonicalNode] = []
        for extracted_page in extraction.pages:
            page_number = extracted_page.number
            page_node_id = make_id("page")
            extracted_text = extracted_page.text
            text_nodes = self._builder._text_to_nodes(
                document_id=document_id,
                source_type=source.source_type,
                version=source.version,
                parent_node_id=page_node_id,
                location_key="page_number",
                location_value=page_number,
                text=extracted_text,
            )
            table_nodes = [
                self._builder.build_structured_table_node(
                    document_id=document_id,
                    source_type=source.source_type,
                    version=source.version,
                    parent_node_id=page_node_id,
                    table_index=table_index,
                    table=rows,
                ) if isinstance(rows, dict) else self._builder.build_matrix_table_node(
                    document_id=document_id,
                    source_type=source.source_type,
                    version=source.version,
                    parent_node_id=page_node_id,
                    table_index=table_index,
                    rows=rows,
                )
                for table_index, rows in enumerate(extracted_page.tables, start=1)
                if rows
            ]
            image_nodes = self._build_image_nodes(
                document_id, source, page_node_id, page_number, extracted_page.images, text_nodes,
            )
            page_visual_node = self._build_pdf_page_visual_node(
                document_id, source, page_node_id, page_number, extracted_text, text_nodes, extracted_page.images,
            )
            if page_visual_node is not None:
                image_nodes.append(page_visual_node)
            self._suppress_duplicate_table_text(text_nodes, table_nodes)
            low_confidence_nodes = self._build_low_confidence_nodes(
                document_id, source, page_node_id, page_number, extracted_page,
            )
            children = self._order_page_children([*text_nodes, *table_nodes, *image_nodes, *low_confidence_nodes])
            self._flag_suspicious_pdf_numbers(children)
            for child in children:
                self._set_page_provenance(child, page_number)
            page_nodes.append(
                CanonicalNode(
                    node_id=page_node_id,
                    node_type=NodeType.PAGE,
                    title=f"Page {page_number}",
                    children=children,
                    attributes={
                        "page_number": page_number,
                        "text_block_count": len(extracted_page.text_blocks),
                        "image_count": len(extracted_page.images),
                        "images": extracted_page.images,
                        "reconciliation": extracted_page.reconciliation,
                        "low_confidence_block_count": len(low_confidence_nodes),
                    },
                    provenance=Provenance(
                        document_id=document_id,
                        source_type=source.source_type,
                        version=source.version,
                        parent_node_id=root_node_id,
                        page_number=page_number,
                    ),
                    confidence=0.9 if extracted_text else 0.45,
                )
            )
        return page_nodes

    def _build_image_nodes(
        self,
        document_id: str,
        source: IngestionSource,
        page_node_id: str,
        page_number: int,
        images: list[dict[str, Any]],
        text_nodes: list[CanonicalNode],
    ) -> list[CanonicalNode]:
        """Creates one canonical PDF image node per extracted image metadata record."""
        nodes: list[CanonicalNode] = []
        seen: set[tuple] = set()
        for image_index, image in enumerate(images, start=1):
            if not isinstance(image, dict):
                continue
            bbox = image.get("bbox")
            key = self._image_identity_key(page_number, image, bbox)
            if key in seen:
                continue
            seen.add(key)
            attributes = dict(image)
            attributes.update({
                "page_number": page_number,
                "image_index": image_index,
                "nearby_text": self._nearby_text_for_image(bbox, text_nodes),
                "image_storage_status": attributes.get("image_storage_status", "metadata_only"),
            })
            nodes.append(CanonicalNode(
                node_id=make_id("image"),
                node_type=NodeType.IMAGE,
                title=normalize_text(str(image.get("caption") or "")) or f"Image {image_index}",
                text=normalize_text(str(image.get("description") or image.get("caption") or "")),
                attributes=attributes,
                provenance=Provenance(
                    document_id=document_id,
                    source_type=source.source_type,
                    version=source.version,
                    parent_node_id=page_node_id,
                    page_number=page_number,
                ),
                confidence=0.72 if bbox else 0.55,
            ))
        return nodes

    def _build_pdf_page_visual_node(
        self,
        document_id: str,
        source: IngestionSource,
        page_node_id: str,
        page_number: int,
        extracted_text: str,
        text_nodes: list[CanonicalNode],
        images: list[dict[str, Any]],
    ) -> CanonicalNode | None:
        """Adds a rendered page asset only when native visual extraction missed a large visual.

        Full-page renders are a last fallback for pages such as architecture diagrams,
        flow diagrams, large technical diagrams, or charts that were not captured as
        their own usable image/table/chart asset. If an extractable visual already
        exists on the page, retrieval should use that crop instead of duplicating the
        whole page.
        """
        if os.getenv("JLR_PDF_PAGE_VISUAL_ASSETS", "true").lower() != "true":
            return None
        page_text = normalize_text(extracted_text or "\n".join(node.text or "" for node in text_nodes))
        if not self._is_pdf_page_visual_candidate(page_text, images):
            return None
        visual_type = self._pdf_page_visual_type(page_text)
        attributes = {
            "page_number": page_number,
            "image_index": len(images) + 1,
            "nearby_text": page_text[:1500],
            "image_storage_status": "render_required",
            "needs_vision_description": True,
            "vision_reason": "Native PDF visual extraction did not produce a usable crop for a page that appears to contain a large technical visual, so a rendered page screenshot is required for visual retrieval.",
            "force_full_page_asset": True,
            "classification": visual_type,
            "image_type": "pdf_page_render",
            "visual_object_type": visual_type,
            "is_decorative": False,
            "fallback_reason": "large_visual_extraction_failed",
        }
        return CanonicalNode(
            node_id=make_id("image"),
            node_type=NodeType.IMAGE,
            title=f"Page {page_number} visual figure",
            text=page_text[:500],
            attributes=attributes,
            provenance=Provenance(
                document_id=document_id,
                source_type=source.source_type,
                version=source.version,
                parent_node_id=page_node_id,
                page_number=page_number,
            ),
            confidence=0.78,
        )

    def _is_pdf_page_visual_candidate(self, page_text: str, images: list[dict[str, Any]]) -> bool:
        """Gates full-page screenshots to extraction failures for large visual pages."""
        if self._has_usable_extracted_visual(images):
            return False
        text = page_text.lower()
        strong_visual_terms = (
            "architecture", "flow diagram", "block diagram", "schematic", "wiring diagram",
            "technical diagram", "diagram", "chart", "graph", "plot", "figure", "fig.",
            "section view", "cad", "drawing", "layout", "exploded view",
        )
        if any(term in text for term in strong_visual_terms):
            return True
        # Low-text pages with image metadata but no usable crop are often vector-only visuals.
        return bool(images) and len(page_text) < 180 and any(
            str(image.get("xref") or image.get("bbox") or "") and not self._is_decorative_pdf_image(image)
            for image in images if isinstance(image, dict)
        )

    def _has_usable_extracted_visual(self, images: list[dict[str, Any]]) -> bool:
        return any(self._is_usable_extracted_visual(image) for image in images if isinstance(image, dict))

    def _is_usable_extracted_visual(self, image: dict[str, Any]) -> bool:
        if self._is_decorative_pdf_image(image):
            return False
        if image.get("bbox") or image.get("xref") or image.get("saved_path") or image.get("image_storage_status") in {"stored", "stored_crop"}:
            area_ratio = self._pdf_image_area_ratio(image)
            if area_ratio <= 0:
                width = self._as_float(image.get("width") or image.get("image_width"))
                height = self._as_float(image.get("height") or image.get("image_height"))
                return width >= 240 and height >= 160
            return area_ratio >= float(os.getenv("JLR_PDF_USABLE_IMAGE_MIN_AREA_RATIO", "0.015"))
        return False

    def _is_decorative_pdf_image(self, image: dict[str, Any]) -> bool:
        fields = " ".join(str(image.get(key) or "") for key in ("caption", "description", "classification", "image_type", "title")).lower()
        if any(term in fields for term in ("diagram", "chart", "graph", "plot", "figure", "architecture", "flow", "cad", "drawing")):
            return False
        if any(term in fields for term in ("logo", "icon", "header", "footer", "watermark", "decorative")):
            return True
        width = self._as_float(image.get("width") or image.get("image_width"))
        height = self._as_float(image.get("height") or image.get("image_height"))
        if width > 0 and height > 0 and (width < 240 or height < 120):
            return True
        return False

    def _pdf_image_area_ratio(self, image: dict[str, Any]) -> float:
        bbox = image.get("bbox")
        if not isinstance(bbox, dict):
            return 0.0
        width = self._as_float(bbox.get("width"))
        height = self._as_float(bbox.get("height"))
        if width <= 0 or height <= 0:
            return 0.0
        page_width = self._as_float(bbox.get("page_width") or image.get("page_width"))
        page_height = self._as_float(bbox.get("page_height") or image.get("page_height"))
        if page_width <= 0 or page_height <= 0:
            page_width = float(os.getenv("JLR_PDF_DEFAULT_PAGE_WIDTH", "612"))
            page_height = float(os.getenv("JLR_PDF_DEFAULT_PAGE_HEIGHT", "792"))
        if page_width <= 0 or page_height <= 0:
            return 0.0
        return (width * height) / (page_width * page_height)

    @staticmethod
    def _as_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _pdf_page_visual_type(self, page_text: str) -> str:
        text = page_text.lower()
        if "flow" in text or "process" in text:
            return "flow_diagram"
        if "cad" in text or "3d" in text or "section view" in text:
            return "cad_3d_visual"
        if "chart" in text or "graph" in text or "plot" in text:
            return "chart"
        if "formula" in text or "equation" in text:
            return "formula"
        return "diagram"

    def _nearby_text_for_image(self, bbox: Any, text_nodes: list[CanonicalNode]) -> str:
        """Finds same-page text nearest to an image bbox for OCR/VLM grounding."""
        if not isinstance(bbox, dict):
            return normalize_text("\n".join(node.text or "" for node in text_nodes[:4])) or ""
        image_rect = self._rect_from_bbox(bbox)
        if image_rect is None:
            return normalize_text("\n".join(node.text or "" for node in text_nodes[:4])) or ""
        ranked: list[tuple[float, str]] = []
        for node in text_nodes:
            text = normalize_text(node.text or "")
            if not text:
                continue
            node_rect = self._rect_from_bbox(node.attributes.get("bbox"))
            if node_rect is None:
                ranked.append((999999.0, text))
                continue
            ranked.append((self._rect_distance(image_rect, node_rect), text))
        ranked.sort(key=lambda item: item[0])
        return "\n".join(text for _, text in ranked[:4])[:1200]

    def _order_page_children(self, children: list[CanonicalNode]) -> list[CanonicalNode]:
        """Keeps page children in visual reading order when bbox metadata exists."""
        return sorted(children, key=lambda node: (*self._node_position(node), node.node_type.value))

    def _node_position(self, node: CanonicalNode) -> tuple[float, float]:
        bbox = node.attributes.get("bbox")
        rect = self._rect_from_bbox(bbox)
        if rect is None:
            return (999999.0, 999999.0)
        return (rect[1], rect[0])

    def _bbox_key(self, bbox: Any) -> tuple:
        rect = self._rect_from_bbox(bbox)
        return tuple(round(value, 1) for value in rect) if rect else ()

    def _image_identity_key(self, page_number: int, image: dict[str, Any], bbox: Any) -> tuple:
        bbox_key = self._bbox_key(bbox)
        caption = normalize_text(str(image.get("caption") or ""))
        if bbox_key or caption:
            return (page_number, bbox_key, caption)
        if image.get("xref") is not None:
            return (page_number, "xref", str(image.get("xref")))
        return (
            page_number,
            "metadata",
            str(image.get("width") or ""),
            str(image.get("height") or ""),
            str(image.get("extension") or ""),
        )

    def _rect_from_bbox(self, bbox: Any) -> tuple[float, float, float, float] | None:
        if not isinstance(bbox, dict):
            return None
        try:
            if all(key in bbox for key in ("x0", "y0", "x1", "y1")):
                return float(bbox["x0"]), float(bbox["y0"]), float(bbox["x1"]), float(bbox["y1"])
            if all(key in bbox for key in ("left", "top", "right", "bottom")):
                return float(bbox["left"]), float(bbox["top"]), float(bbox["right"]), float(bbox["bottom"])
            if all(key in bbox for key in ("left", "top", "width", "height")):
                left = float(bbox["left"])
                top = float(bbox["top"])
                return left, top, left + float(bbox["width"]), top + float(bbox["height"])
        except Exception:
            return None
        return None

    def _rect_distance(self, left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> float:
        left_center = ((left[0] + left[2]) / 2, (left[1] + left[3]) / 2)
        right_center = ((right[0] + right[2]) / 2, (right[1] + right[3]) / 2)
        return abs(left_center[0] - right_center[0]) + abs(left_center[1] - right_center[1])

    def _build_low_confidence_nodes(
        self,
        document_id: str,
        source: IngestionSource,
        page_node_id: str,
        page_number: int,
        extracted_page,
    ) -> list[CanonicalNode]:
        """Turns merger-excluded (`retrieval_allowed=False`) text blocks into
        their own canonical nodes, instead of letting them silently
        disappear once they're kept out of the merged page text (Finding B):
        `_text_to_nodes` only ever sees `extracted_page.text`, so a block
        excluded there would otherwise never reach Master JSON at all. The
        `LOW_CONFIDENCE_BLOCK` node type is deliberately excluded from
        `CanonicalChunkBuilder._NARRATIVE_TYPES`, so this content is
        unchunkable by construction -- the explicit `retrieval_allowed=False`
        attribute is belt-and-braces, not the only defense.
        """
        nodes: list[CanonicalNode] = []
        for block in extracted_page.text_blocks:
            if block.get("retrieval_allowed") is not False:
                continue
            text = normalize_text(str(block.get("text", ""))) or ""
            if not text:
                continue
            reconciliation = block.get("reconciliation")
            nodes.append(CanonicalNode(
                node_id=make_id("low_confidence"),
                node_type=NodeType.LOW_CONFIDENCE_BLOCK,
                text=text,
                attributes={
                    "retrieval_allowed": False,
                    "requires_review": True,
                    "extraction_confidence": (reconciliation or {}).get("confidence_tier"),
                    "extraction_confidence_reason": (reconciliation or {}).get("confidence_reason"),
                    "reconciliation": reconciliation,
                    "extractor_role": block.get("extractor_role"),
                    "bbox": block.get("bbox"),
                },
                provenance=Provenance(
                    document_id=document_id,
                    source_type=source.source_type,
                    version=source.version,
                    parent_node_id=page_node_id,
                    page_number=page_number,
                ),
                confidence=0.3,
            ))
        return nodes

    def _flag_suspicious_pdf_numbers(self, nodes: list[CanonicalNode]) -> int:
        """Marks likely PDF exponent-corruption values as review-only retrieval content."""
        flagged = 0
        for node in nodes:
            text = normalize_text(
                node.text
                or str(node.attributes.get("semantic_text", ""))
                or str(node.attributes.get("value", ""))
            ) or ""
            if self._is_suspicious_pdf_number_context(text):
                node.attributes["requires_review"] = True
                node.attributes["retrieval_allowed"] = False
                node.attributes["suspicious_numeric"] = True
                node.attributes["suspicious_numeric_reason"] = (
                    "PDF extraction produced a long digit run in a technical numeric context; "
                    "possible superscript/exponent corruption."
                )
                flagged += 1
            flagged += self._flag_suspicious_pdf_numbers(node.children)
        return flagged

    def _is_suspicious_pdf_number_context(self, text: str) -> bool:
        """Detects known risky PDF numeric corruption without guessing corrections."""
        normalized = normalize_text(text) or ""
        if not normalized:
            return False
        technical_context = re.search(
            r"\b(resistivity|conductivity|dielectric|thermal|expansion|coefficient|ohm|volume|surface)\b",
            normalized,
            re.IGNORECASE,
        )
        if not technical_context:
            return False
        if re.search(r"10\s*(\^|[⁰¹²³⁴⁵⁶⁷⁸⁹])", normalized):
            return False
        return bool(re.search(r"\b10\d{3,}\b", normalized))

    def _suppress_duplicate_table_text(self, text_nodes: list[CanonicalNode], table_nodes: list[CanonicalNode]) -> None:
        """Keeps PDF audit text while preventing duplicated table prose from retrieval."""
        table_text = " ".join(self._node_text_values(table) for table in table_nodes)
        table_tokens = self._coverage_tokens(table_text)
        if not table_tokens:
            return
        for node in text_nodes:
            node_tokens = self._coverage_tokens(node.text or "")
            if len(node_tokens) < 12:
                continue
            coverage = self._token_coverage(table_tokens, node_tokens)
            if coverage >= 0.85:
                node.attributes["retrieval_allowed"] = False
                node.attributes["suppression_reason"] = "Text block substantially duplicates structured table content on the same PDF page."
                node.attributes["table_text_coverage"] = round(coverage, 3)

    def _node_text_values(self, node: CanonicalNode) -> str:
        """Collects text from a canonical subtree for same-page duplicate checks."""
        values = [normalize_text(node.text) or ""]
        for child in node.children:
            values.append(self._node_text_values(child))
        return " ".join(value for value in values if value)

    def _coverage_tokens(self, text: str) -> Counter[str]:
        """Tokenizes text for duplicate coverage without using fuzzy embeddings."""
        return Counter(re.findall(r"[a-z0-9]+(?:[./_-][a-z0-9]+)*", (normalize_text(text) or "").casefold()))

    def _token_coverage(self, reference: Counter[str], candidate: Counter[str]) -> float:
        """Measures how much candidate wording is already represented in reference."""
        total = sum(candidate.values())
        if not total:
            return 1.0
        shared = sum(min(reference[token], count) for token, count in candidate.items())
        return shared / total

    def _set_page_provenance(self, node: CanonicalNode, page_number: int) -> None:
        """Carries PDF page citations through table rows and cells."""
        node.provenance.page_number = page_number
        for child in node.children:
            self._set_page_provenance(child, page_number)

    # This function collects warning signals from Docling extraction output.
    def _collect_docling_warnings(
        self,
        exported: dict[str, Any],
        markdown: str,
        source_type: SourceType,
    ) -> list[str]:
        warnings: list[str] = []
        if not normalize_text(markdown):
            warnings.append(f"No textual markdown produced by Docling for {source_type.value}")
        if not self._builder._find_page_sections(exported) and not normalize_text(markdown):
            warnings.append(f"No page-like structure produced by Docling for {source_type.value}")
        return warnings


class DocxDocumentParser(SourceParser):
    """Parses DOCX files into section and paragraph nodes."""

    source_types = (SourceType.DOCX,)

    def __init__(self) -> None:
        self._docling = DoclingContentAdapter()
        self._fallback = DocxFallbackExtractor()
        self._builder = DoclingCanonicalBuilder()
        self._renderer = WordComDocxRenderer()

    def _docx_render_skipped_result(self):
        """Returns a renderer-compatible result when DOCX layout rendering is disabled."""
        from backend.ingestion.extraction.docx_renderer import DocxRenderResult

        return DocxRenderResult(status="skipped", renderer="word_com", warning="DOCX render disabled by JLR_DOCX_RENDER_ENABLED=false.")

    # This function loads a DOCX file using native OOXML structure and enriches it with Docling semantics.
    def parse(self, source: IngestionSource, context: ParserContext) -> CanonicalDocument:
        document_id = source.document_id or make_id("doc")
        root_node_id = make_id("document")
        native = self._fallback.extract(source.file_path)
        if os.getenv("JLR_DOCX_RENDER_ENABLED", "true").lower() == "true":
            render_result = self._renderer.render_pdf(source.file_path, source.agent_id, document_id, source.version)
        else:
            render_result = self._docx_render_skipped_result()
        children = self._build_fallback_children(document_id, root_node_id, source, native)
        warnings = list(native.warnings)
        if render_result.status != "success" and os.getenv("JLR_DOCX_RENDER_REQUIRED", "false").lower() == "true":
            warnings.append(f"[docx_render_{render_result.status}] Word COM render status: {render_result.warning or render_result.error_type or render_result.status}")
        docling_used = False
        docling_details: dict[str, Any] = {"conversion_status": "not_available"}
        parser_name = "docx_native_docling_semantic_parser"
        extraction_mode = "native_structure_semantic_enriched"

        if self._docling.is_available():
            try:
                extracted = self._docling.extract(source.file_path)
                docling_used = True
                docling_details = self._docling_semantic_summary(extracted)
                warnings.extend(extracted.warnings)
                warnings.extend(self._collect_docling_warnings(extracted.exported, extracted.markdown))
                self._enrich_docx_native_children(children, extracted)
            except Exception as error:
                parser_name = "docx_python_docx_native_parser"
                extraction_mode = "native_structure"
                docling_details = {"conversion_status": "failed", "error_type": type(error).__name__}
                warnings.append(f"Docling DOCX semantic enrichment failed; native python-docx extraction used: {type(error).__name__}")
        else:
            parser_name = "docx_python_docx_native_parser"
            extraction_mode = "native_structure"

        layout_count = self._convert_layout_tables(children, document_id, source.source_type, source.version)
        image_count = self._attach_docx_image_assets(children, source, document_id)
        review_count = self._requires_review_count(children)
        if render_result.status == "success" and render_result.pdf_path:
            aligned_count = self._align_docx_nodes_to_rendered_pdf(children, Path(render_result.pdf_path))
            if aligned_count:
                warnings.append(f"[docx_rendered_layout] Assigned rendered page/bbox metadata to {aligned_count} DOCX nodes.")
        if layout_count:
            warnings.append(f"[docx_layout_tables] Converted {layout_count} layout-style DOCX tables into narrative blocks.")
        if image_count:
            warnings.append(f"[docx_images] Stored {image_count} DOCX image assets and attached locators to image nodes.")
        if review_count:
            warnings.append(f"[docx_review_nodes] {review_count} canonical nodes require review.")
        self._normalize_docx_text(children)

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
                parser_name=parser_name,
                parser_version=context.parser_version,
                extraction_mode=extraction_mode,
            ),
            metadata=DocumentMetadata(
                title=source.file_path.stem,
                document_family=DocumentFamily.HIERARCHICAL,
            ),
            root_nodes=[
                CanonicalNode(
                    node_id=root_node_id,
                    node_type=NodeType.DOCUMENT_ROOT,
                    title=source.file_path.stem,
                    children=children,
                    attributes={"docling_enabled": docling_used, "native_extractor": "python-docx", "docling_semantic_enrichment": docling_details, "rendered_layout": render_result.as_dict()},
                    provenance=Provenance(
                        document_id=document_id,
                        source_type=source.source_type,
                        version=source.version,
                    ),
                    confidence=0.97,
                )
            ],
            quality=QualityMetadata(
                overall_confidence=0.9 if not warnings else 0.82,
                warnings=warnings,
            ),
        )

    def _align_docx_nodes_to_rendered_pdf(self, children: list[CanonicalNode], rendered_pdf_path: Path) -> int:
        """Best-effort alignment of native DOCX nodes to rendered PDF page/bbox metadata."""
        words_by_page = self._rendered_pdf_words(rendered_pdf_path)
        if not words_by_page:
            return 0
        aligned = 0
        for node in self._walk_nodes(children):
            text = self._node_alignment_text(node)
            if not text:
                continue
            match = self._find_rendered_text_match(words_by_page, text)
            if match is None:
                continue
            page_number, bbox = match
            node.provenance.page_number = page_number
            node.attributes.setdefault("rendered_layout", {})
            if node.node_type == NodeType.IMAGE:
                node.attributes["rendered_layout"].update({
                    "source": "word_com_pdf",
                    "page_number": page_number,
                    "context_bbox": bbox,
                    "alignment": "nearby_text_context_match",
                    "bbox_scope": "context_only",
                })
            else:
                node.provenance.bbox = [bbox["x0"], bbox["y0"], bbox["x1"], bbox["y1"]]
                node.attributes["rendered_layout"].update({
                    "source": "word_com_pdf",
                    "page_number": page_number,
                    "bbox": bbox,
                    "alignment": "text_sequence_match",
                    "bbox_scope": "node_text",
                })
            aligned += 1
        return aligned

    def _rendered_pdf_words(self, rendered_pdf_path: Path) -> dict[int, list[_RenderedWord]]:
        """Reads rendered PDF words with PyMuPDF coordinates."""
        try:
            import pymupdf as fitz
        except ImportError:
            return {}
        words_by_page: dict[int, list[_RenderedWord]] = {}
        try:
            with fitz.open(str(rendered_pdf_path)) as pdf:
                for page_number, page in enumerate(pdf, start=1):
                    page_words: list[_RenderedWord] = []
                    for item in page.get_text("words", sort=True):
                        if len(item) < 5:
                            continue
                        text = normalize_text(str(item[4]) or "") or ""
                        if not text:
                            continue
                        page_words.append(_RenderedWord(page_number, float(item[0]), float(item[1]), float(item[2]), float(item[3]), text))
                    words_by_page[page_number] = page_words
        except Exception:
            return {}
        return words_by_page

    def _node_alignment_text(self, node: CanonicalNode) -> str:
        """Chooses compact text for rendered-layout matching."""
        if node.node_type in {NodeType.PARAGRAPH, NodeType.BULLET_BLOCK, NodeType.TEXT_BLOCK, NodeType.SECTION, NodeType.TITLE_BLOCK}:
            return normalize_text(str(node.text or node.title or "")) or ""
        if node.node_type == NodeType.TABLE:
            values = [normalize_text(str(cell.text or cell.attributes.get("value") or "")) or "" for row in node.children for cell in row.children if cell.node_type == NodeType.TABLE_CELL]
            return normalize_text(" ".join(value for value in values if value)) or ""
        if node.node_type == NodeType.IMAGE:
            return normalize_text(str(node.attributes.get("nearby_text") or "")) or ""
        return ""

    def _find_rendered_text_match(self, words_by_page: dict[int, list[_RenderedWord]], text: str) -> tuple[int, dict[str, float]] | None:
        """Finds a contiguous token sequence in rendered PDF words and returns its bbox."""
        tokens = re.findall(r"[a-z0-9]+(?:[./_^-][a-z0-9]+)*", (normalize_text(text) or "").casefold())
        if not tokens:
            return None
        max_tokens = int(os.getenv("JLR_DOCX_LAYOUT_MATCH_MAX_TOKENS", "24"))
        query = tokens[:max_tokens]
        if len(query) < int(os.getenv("JLR_DOCX_LAYOUT_MATCH_MIN_TOKENS", "3")):
            return None
        for page_number, words in words_by_page.items():
            page_tokens = [re.findall(r"[a-z0-9]+(?:[./_^-][a-z0-9]+)*", word.text.casefold()) for word in words]
            flat_tokens = [items[0] if items else "" for items in page_tokens]
            limit = len(flat_tokens) - len(query) + 1
            for start in range(max(limit, 0)):
                if flat_tokens[start:start + len(query)] != query:
                    continue
                matched_words = words[start:start + len(query)]
                x0 = min(word.x0 for word in matched_words)
                y0 = min(word.y0 for word in matched_words)
                x1 = max(word.x1 for word in matched_words)
                y1 = max(word.y1 for word in matched_words)
                padding = float(os.getenv("JLR_DOCX_LAYOUT_BBOX_PADDING", "4"))
                return page_number, {"x0": max(x0 - padding, 0.0), "y0": max(y0 - padding, 0.0), "x1": x1 + padding, "y1": y1 + padding}
        return None

    def _attach_docx_image_assets(self, children: list[CanonicalNode], source: IngestionSource, document_id: str) -> int:
        """Stores DOCX media files and maps them by media name before order fallback."""
        images = self._docx_media_assets(source.file_path, source.agent_id, document_id, source.version)
        if not images:
            return 0
        by_media_name = {asset["media_name"]: asset for asset in images}
        used: set[int] = set()
        matched = 0
        for node in self._walk_nodes(children):
            if node.node_type != NodeType.IMAGE:
                continue
            asset = None
            media_name = str(node.attributes.get("media_name") or "")
            if media_name:
                asset = by_media_name.get(media_name)
            if asset is None:
                for index, candidate in enumerate(images):
                    if index not in used:
                        asset = candidate
                        used.add(index)
                        break
            else:
                try:
                    used.add(images.index(asset))
                except ValueError:
                    pass
            if asset is None:
                node.attributes.update({"image_storage_status": "metadata_only", "locator": node.attributes.get("source_ref") or media_name})
                continue
            matched += 1
            node.attributes.update({
                "locator": asset["media_name"],
                "media_name": asset["media_name"],
                "saved_path": asset["saved_path"],
                "image_hash": asset["image_hash"],
                "image_extension": asset["extension"],
                "image_width": asset.get("image_width"),
                "image_height": asset.get("image_height"),
                "image_occurrence_count": asset.get("image_occurrence_count"),
                "classification": asset.get("classification"),
                "image_type": asset.get("image_type"),
                "is_decorative": asset.get("is_decorative"),
                "image_storage_status": "stored",
                "image_match": "media_name" if media_name and asset["media_name"] == media_name else "document_order",
            })
        return matched

    def _docx_media_assets(self, source_path: Path, agent_id: str, document_id: str, version: str) -> list[dict[str, Any]]:
        """Extracts DOCX embedded media files without interpreting image content."""
        assets: list[dict[str, Any]] = []
        folder = Path("storage") / "visual-assets" / self._safe_component(agent_id) / self._safe_component(document_id) / self._safe_component(version)
        try:
            with zipfile.ZipFile(source_path) as archive:
                media_names = self._docx_media_sequence(archive)
                saved_by_hash: dict[str, Path] = {}
                hash_counts: Counter[str] = Counter()
                prepared: list[dict[str, Any]] = []
                for media_name in media_names:
                    blob = archive.read(media_name)
                    digest = hashlib.sha256(blob).hexdigest()
                    hash_counts[digest] += 1
                    prepared.append({"media_name": media_name, "blob": blob, "image_hash": digest})
                for item in prepared:
                    media_name = item["media_name"]
                    blob = item["blob"]
                    digest = item["image_hash"]
                    extension = self._safe_extension(Path(media_name).suffix.lstrip(".") or "bin")
                    target = saved_by_hash.get(digest)
                    if target is None:
                        folder.mkdir(parents=True, exist_ok=True)
                        target = folder / f"docx_image_{len(saved_by_hash) + 1:03d}_{digest[:12]}.{extension}"
                        target.write_bytes(blob)
                        saved_by_hash[digest] = target
                    width, height = self._image_dimensions(target)
                    occurrence_count = hash_counts[digest]
                    classification = self._classify_docx_media(width, height, occurrence_count)
                    assets.append({
                        "media_name": media_name,
                        "saved_path": target.as_posix(),
                        "image_hash": digest,
                        "extension": extension,
                        "image_width": width,
                        "image_height": height,
                        "image_occurrence_count": occurrence_count,
                        "classification": classification,
                        "image_type": classification,
                        "is_decorative": classification in {"icon", "decorative"},
                    })
        except Exception:
            return []
        return assets


    def _image_dimensions(self, image_path: Path) -> tuple[int | None, int | None]:
        """Returns stored image pixel dimensions when Pillow is available."""
        try:
            from PIL import Image

            with Image.open(image_path) as image:
                return int(image.width), int(image.height)
        except Exception:
            return None, None

    def _classify_docx_media(self, width: int | None, height: int | None, occurrence_count: int) -> str:
        """Classifies tiny/repeated DOCX media so icons do not look like figures."""
        if not width or not height:
            return "image"
        max_side = max(width, height)
        area = width * height
        if max_side <= 64 or area <= 4096:
            return "icon"
        if occurrence_count >= 3 and max_side <= 128:
            return "icon"
        return "image"

    def _docx_media_sequence(self, archive: zipfile.ZipFile) -> list[str]:
        """Returns DOCX media files in drawing-reference order, including repeats."""
        rels: dict[str, str] = {}
        try:
            root = ElementTree.fromstring(archive.read("word/_rels/document.xml.rels"))
            for relationship in root:
                relationship_id = relationship.attrib.get("Id")
                target = relationship.attrib.get("Target", "")
                if relationship_id and target.startswith("media/"):
                    rels[relationship_id] = f"word/{target}"
        except Exception:
            return sorted(name for name in archive.namelist() if name.startswith("word/media/"))
        try:
            document = ElementTree.fromstring(archive.read("word/document.xml"))
        except Exception:
            return sorted(rels.values())
        namespace = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
        ordered = [
            rels[element.attrib[namespace]]
            for element in document.iter()
            if namespace in element.attrib and element.attrib[namespace] in rels
        ]
        return ordered or sorted(rels.values())

    def _convert_layout_tables(self, children: list[CanonicalNode], document_id: str, source_type: SourceType, version: str) -> int:
        """Re-types Word layout tables into narrative form sections before chunking."""
        converted = 0
        for index, node in enumerate(list(children)):
            if node.node_type == NodeType.TABLE and self._is_layout_table(node):
                children[index] = self._layout_table_to_form_section(node, document_id, source_type, version)
                converted += 1
            if node.children:
                converted += self._convert_layout_tables(node.children, document_id, source_type, version)
        return converted

    def _is_layout_table(self, table: CanonicalNode) -> bool:
        """Detects DOCX tables used as page layout rather than structured data."""
        rows = [child for child in table.children if child.node_type == NodeType.TABLE_ROW]
        if not rows:
            return False
        widths = [len([cell for cell in row.children if cell.node_type == NodeType.TABLE_CELL]) for row in rows]
        if not widths or max(widths) > 3:
            return False
        text = " ".join(normalize_text(cell.text) or "" for row in rows for cell in row.children)
        label_like = any(label in text.casefold() for label in ("requirements:", "examples:", "requirement:", "example:"))
        return bool(table.attributes.get("requires_review")) and label_like

    def _layout_table_to_form_section(self, table: CanonicalNode, document_id: str, source_type: SourceType, version: str) -> CanonicalNode:
        """Preserves layout-table text as narrative blocks with the raw table retained."""
        children: list[CanonicalNode] = []
        for row in (child for child in table.children if child.node_type == NodeType.TABLE_ROW):
            text = self._layout_row_text(row)
            if not text:
                continue
            children.append(CanonicalNode(
                node_id=make_id("layout"),
                node_type=NodeType.LABEL_VALUE_BLOCK if ":" in text[:40] else NodeType.TEXT_BLOCK,
                text=text,
                attributes={"source_table_node_id": table.node_id, "layout_table_converted": True,
                            "source_row_index": row.attributes.get("row_index")},
                provenance=Provenance(document_id=document_id, source_type=source_type, version=version,
                                      parent_node_id=table.node_id),
                confidence=min(table.confidence, 0.84),
            ))
        return CanonicalNode(
            node_id=make_id("form_section"),
            node_type=NodeType.FORM_SECTION,
            title=table.title,
            children=children,
            attributes={"source_table_node_id": table.node_id, "layout_table_converted": True,
                        "raw_table_attributes": table.attributes},
            provenance=table.provenance,
            confidence=min(table.confidence, 0.84),
        )

    def _layout_row_text(self, row: CanonicalNode) -> str | None:
        """Combines label/prose layout cells while stripping padding noise."""
        values = [normalize_text((cell.text or "").replace("\xa0", " ")) for cell in row.children
                  if cell.node_type == NodeType.TABLE_CELL]
        return normalize_text(" ".join(value for value in values if value))

    def _requires_review_count(self, children: list[CanonicalNode]) -> int:
        """Counts remaining node-level review flags after DOCX normalization."""
        return sum(1 for node in self._walk_nodes(children) if node.attributes.get("requires_review"))

    def _normalize_docx_text(self, children: list[CanonicalNode]) -> None:
        """Normalizes DOCX non-breaking-space padding after parser conversion."""
        for node in self._walk_nodes(children):
            if isinstance(node.text, str):
                node.text = normalize_text(node.text.replace("\xa0", " "))
            if isinstance(node.title, str):
                node.title = normalize_text(node.title.replace("\xa0", " "))

    def _walk_nodes(self, children: list[CanonicalNode]):
        """Yields DOCX canonical nodes in source order."""
        for node in children:
            yield node
            yield from self._walk_nodes(node.children)

    def _safe_component(self, value: str) -> str:
        """Creates path-safe generated asset names."""
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
        return cleaned or "unknown"

    def _safe_extension(self, value: str) -> str:
        """Keeps file extensions simple and safe."""
        cleaned = re.sub(r"[^A-Za-z0-9]+", "", value.lower())
        return cleaned or "bin"

    def _docling_semantic_summary(self, extracted: ExtractionResult) -> dict[str, Any]:
        """Summarizes Docling output used as DOCX semantic enrichment."""
        exported = extracted.exported or {}
        body_children = exported.get("body", {}).get("children", []) if isinstance(exported.get("body"), dict) else []
        tables = exported.get("tables", []) if isinstance(exported.get("tables"), list) else []
        return {
            "conversion_status": extracted.processing_details.get("conversion_status", "success"),
            "semantic_extractor": extracted.extractor_name,
            "body_children": len(body_children),
            "table_count": len(tables),
            "markdown_chars": len(extracted.markdown or ""),
            "requires_review": extracted.requires_review,
        }

    def _enrich_docx_native_children(self, children: list[CanonicalNode], extracted: ExtractionResult) -> None:
        """Adds lightweight Docling semantic counts without overwriting native DOCX structure."""
        summary = self._docling_semantic_summary(extracted)
        native_counts = self._docx_node_counts(children)
        for node in children:
            node.attributes.setdefault("docling_enabled", True)
        if children:
            children[0].attributes.setdefault("semantic_enrichment", {
                "type": "docling_semantic_enrichment",
                "native_counts": native_counts,
                "docling_summary": summary,
            })

    def _docx_node_counts(self, children: list[CanonicalNode]) -> dict[str, int]:
        """Counts main DOCX native node types for enrichment audit metadata."""
        counts = {"sections": 0, "paragraphs": 0, "tables": 0, "images": 0}
        for node in self._walk_nodes(children):
            if node.node_type == NodeType.SECTION:
                counts["sections"] += 1
            elif node.node_type in {NodeType.PARAGRAPH, NodeType.BULLET_BLOCK, NodeType.TEXT_BLOCK}:
                counts["paragraphs"] += 1
            elif node.node_type == NodeType.TABLE:
                counts["tables"] += 1
            elif node.node_type == NodeType.IMAGE:
                counts["images"] += 1
        return counts

    # This function builds canonical DOCX fallback content from the shared extraction contract.
    def _build_fallback_children(
        self,
        document_id: str,
        root_node_id: str,
        source: IngestionSource,
        extraction: ExtractionResult,
    ) -> list[CanonicalNode]:
        children: list[CanonicalNode] = []
        current_section: CanonicalNode | None = None

        table_index = 0
        for block in extraction.blocks:
            block_type = str(block.get("block_type") or "paragraph")
            if block_type == "table":
                table_index += 1
                rows = block.get("rows") or []
                node = self._builder.build_matrix_table_node(
                    document_id=document_id,
                    source_type=source.source_type,
                    version=source.version,
                    parent_node_id=current_section.node_id if current_section else root_node_id,
                    table_index=int(block.get("table_index") or table_index),
                    rows=rows,
                )
                node.attributes.update({
                    "source_table_type": "native_docx_table",
                    "style_name": block.get("style_name") or "",
                    "extractor_role": block.get("extractor_role") or "native_structure",
                })
                if current_section:
                    current_section.children.append(node)
                else:
                    children.append(node)
                continue
            if block_type == "image":
                node = CanonicalNode(
                    node_id=make_id("image"),
                    node_type=NodeType.IMAGE,
                    title=f"Image {block.get('image_index') or ''}".strip(),
                    attributes={
                        "image_index": block.get("image_index"),
                        "paragraph_index": block.get("paragraph_index"),
                        "relationship_id": block.get("relationship_id"),
                        "media_name": block.get("media_name"),
                        "nearby_text": block.get("nearby_text") or "",
                        "extractor_role": block.get("extractor_role") or "native_structure",
                        "image_storage_status": block.get("image_storage_status") or "pending",
                    },
                    provenance=Provenance(
                        document_id=document_id,
                        source_type=source.source_type,
                        version=source.version,
                        parent_node_id=current_section.node_id if current_section else root_node_id,
                    ),
                    confidence=0.84,
                )
                if current_section:
                    current_section.children.append(node)
                else:
                    children.append(node)
                continue

            text = normalize_text(str(block.get("text", "")))
            if not text:
                continue

            style_name = str(block.get("style_name", ""))
            if style_name.lower().startswith("heading") or (text.endswith(":") and len(text.split()) <= 12):
                section_node = CanonicalNode(
                    node_id=make_id("section"),
                    node_type=NodeType.SECTION,
                    title=text,
                    children=[],
                    attributes={"style_name": style_name, "extractor_role": block.get("extractor_role") or "native_structure", "paragraph_index": block.get("paragraph_index")},
                    provenance=Provenance(
                        document_id=document_id,
                        source_type=source.source_type,
                        version=source.version,
                        parent_node_id=root_node_id,
                    ),
                    confidence=0.92,
                )
                children.append(section_node)
                current_section = section_node
            else:
                paragraph_node = CanonicalNode(
                    node_id=make_id("paragraph"),
                    node_type=NodeType.BULLET_BLOCK if style_name.lower().startswith("list") or text.startswith(("-", "*")) else NodeType.PARAGRAPH,
                    text=text,
                    attributes={"style_name": style_name, "extractor_role": block.get("extractor_role") or "native_structure", "paragraph_index": block.get("paragraph_index")},
                    provenance=Provenance(
                        document_id=document_id,
                        source_type=source.source_type,
                        version=source.version,
                        parent_node_id=current_section.node_id if current_section else root_node_id,
                    ),
                    confidence=0.9,
                )
                if current_section:
                    current_section.children.append(paragraph_node)
                else:
                    children.append(paragraph_node)

        if not extraction.blocks:
            for table_index, rows in enumerate(extraction.tables, start=1):
                children.append(
                    self._builder.build_matrix_table_node(
                        document_id=document_id,
                        source_type=source.source_type,
                        version=source.version,
                        parent_node_id=root_node_id,
                        table_index=table_index,
                        rows=rows,
                    )
                )
        return children

    # This function collects warning signals from the Docling DOCX extraction path.
    def _collect_docling_warnings(self, exported: dict[str, Any], markdown: str) -> list[str]:
        warnings: list[str] = []
        if not self._builder._extract_text_blocks(exported) and not normalize_text(markdown):
            warnings.append("No textual structure detected in Docling DOCX output")
        return warnings
