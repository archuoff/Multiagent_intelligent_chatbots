"""Source parsers that assemble canonical PDF and DOCX documents.

This file uses Docling as the primary extractor for PDF and DOCX files and
falls back to lightweight local libraries when Docling is unavailable.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
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
from backend.ingestion.extraction.fallbacks import PdfFallbackExtractor
from backend.ingestion.extraction.fallbacks import DocxFallbackExtractor
from backend.ingestion.extraction.pdf_inspector import PdfInspector
from backend.ingestion.extraction.merger import ExtractionResultMerger


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
                fallback = self._fallback.extract(source.file_path)
                extracted = self._merger.merge(extracted, fallback)
                warnings = [
                    "Docling and local PDF fallback were quality-reconciled page by page.",
                    *warnings,
                    *fallback.warnings,
                    *extracted.warnings,
                ]
                processing_details = extracted.processing_details
                requires_review = extracted.requires_review
                page_nodes = self._build_fallback_pages(document_id, root_node_id, source, extracted)
                docling_used = True
                extraction_mode = extracted.extraction_mode
                parser_name = "pdf_docling_parser"
            except Exception as error:
                fallback = self._fallback.extract(source.file_path)
                page_nodes = self._build_fallback_pages(document_id, root_node_id, source, fallback)
                parser_name = f"pdf_{fallback.extractor_name}_fallback_parser"
                warnings = [f"Docling PDF extraction failed; {fallback.extractor_name} fallback used: {type(error).__name__}", *fallback.warnings]
        else:
            fallback = self._fallback.extract(source.file_path)
            page_nodes = self._build_fallback_pages(document_id, root_node_id, source, fallback)
            parser_name = f"pdf_{fallback.extractor_name}_fallback_parser"
            warnings = [
                "Scanned or low-text PDF detected; OCR is deferred until a local OCR model policy is configured.",
                *fallback.warnings,
            ]

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
            children = self._builder._text_to_nodes(
                document_id=document_id,
                source_type=source.source_type,
                version=source.version,
                parent_node_id=page_node_id,
                location_key="page_number",
                location_value=page_number,
                text=extracted_text,
            )
            children.extend(
                self._builder.build_matrix_table_node(
                    document_id=document_id,
                    source_type=source.source_type,
                    version=source.version,
                    parent_node_id=page_node_id,
                    table_index=table_index,
                    rows=rows,
                )
                for table_index, rows in enumerate(extracted_page.tables, start=1)
                if rows
            )
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

    # This function loads a DOCX file and converts its paragraphs and tables into canonical nodes.
    def parse(self, source: IngestionSource, context: ParserContext) -> CanonicalDocument:
        document_id = source.document_id or make_id("doc")
        root_node_id = make_id("document")
        docling_used = False
        parser_name = "docx_python_docx_fallback_parser"
        extraction_mode = "fallback_library"

        if self._docling.is_available():
            try:
                extracted = self._docling.extract(source.file_path)
                children = self._builder.build_section_nodes(
                    document_id=document_id,
                    source_type=source.source_type,
                    version=source.version,
                    parent_node_id=root_node_id,
                    exported=extracted.exported,
                    markdown=extracted.markdown,
                )
                children.extend(
                    self._builder.build_table_nodes(
                        document_id=document_id,
                        source_type=source.source_type,
                        version=source.version,
                        parent_node_id=root_node_id,
                        exported={} if extracted.exported.get("body", {}).get("children") else extracted.exported,
                    )
                )
                layout_count = self._convert_layout_tables(children, document_id, source.source_type, source.version)
                image_count = self._attach_docx_image_assets(children, source, document_id)
                review_count = self._requires_review_count(children)
                warnings = [*extracted.warnings, *self._collect_docling_warnings(extracted.exported, extracted.markdown)]
                if layout_count:
                    warnings.append(f"[docx_layout_tables] Converted {layout_count} layout-style DOCX tables into narrative blocks.")
                if image_count:
                    warnings.append(f"[docx_images] Stored {image_count} DOCX image assets and attached locators to image nodes.")
                if review_count:
                    warnings.append(f"[docx_review_nodes] {review_count} canonical nodes require review.")
                docling_used = True
                parser_name = "docx_docling_parser"
                extraction_mode = "docling_primary"
            except Exception as error:
                fallback = self._fallback.extract(source.file_path)
                children = self._build_fallback_children(document_id, root_node_id, source, fallback)
                warnings = [f"Docling DOCX extraction failed; python-docx fallback used: {type(error).__name__}", *fallback.warnings]
        else:
            fallback = self._fallback.extract(source.file_path)
            children = self._build_fallback_children(document_id, root_node_id, source, fallback)
            warnings = fallback.warnings
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
                    attributes={"docling_enabled": docling_used},
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

    def _attach_docx_image_assets(self, children: list[CanonicalNode], source: IngestionSource, document_id: str) -> int:
        """Stores DOCX media files and maps them in order to Docling picture nodes."""
        images = self._docx_media_assets(source.file_path, source.agent_id, document_id, source.version)
        if not images:
            return 0
        image_index = 0
        for node in self._walk_nodes(children):
            if node.node_type != NodeType.IMAGE:
                continue
            if image_index >= len(images):
                node.attributes.update({"image_storage_status": "metadata_only", "locator": node.attributes.get("source_ref")})
                continue
            asset = images[image_index]
            image_index += 1
            node.attributes.update({
                "locator": asset["media_name"],
                "media_name": asset["media_name"],
                "saved_path": asset["saved_path"],
                "image_hash": asset["image_hash"],
                "image_extension": asset["extension"],
                "image_storage_status": "stored",
            })
        return min(image_index, len(images))

    def _docx_media_assets(self, source_path: Path, agent_id: str, document_id: str, version: str) -> list[dict[str, Any]]:
        """Extracts DOCX embedded media files without interpreting image content."""
        assets: list[dict[str, Any]] = []
        folder = Path("storage") / "visual-assets" / self._safe_component(agent_id) / self._safe_component(document_id) / self._safe_component(version)
        try:
            with zipfile.ZipFile(source_path) as archive:
                media_names = self._docx_media_sequence(archive)
                saved_by_hash: dict[str, Path] = {}
                for index, media_name in enumerate(media_names, start=1):
                    blob = archive.read(media_name)
                    digest = hashlib.sha256(blob).hexdigest()
                    extension = self._safe_extension(Path(media_name).suffix.lstrip(".") or "bin")
                    target = saved_by_hash.get(digest)
                    if target is None:
                        folder.mkdir(parents=True, exist_ok=True)
                        target = folder / f"docx_image_{len(saved_by_hash) + 1:03d}_{digest[:12]}.{extension}"
                        target.write_bytes(blob)
                        saved_by_hash[digest] = target
                    assets.append({"media_name": media_name, "saved_path": target.as_posix(),
                                   "image_hash": digest, "extension": extension})
        except Exception:
            return []
        return assets

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

        for block in extraction.blocks:
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
                    attributes={"style_name": style_name},
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
