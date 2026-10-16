"""Run one PDF through Docling only and write a production-style quality JSON.

This tool intentionally bypasses the JLR fallback/reconciliation/canonical parser
pipeline. Its purpose is to inspect Docling's own extraction quality and shape the
output into the schema we want downstream systems to consume:

metadata + nodes + assets + chunks + quality

Example:
    .\\.JLR\\Scripts\\python.exe tests\\ingestion\\tools\\extract_docling_production_json.py ^
      "storage\\raw\\adas-agent\\CONTI_US_Sensor_Mounting Guideline.pdf" ^
      --agent-id adas-agent ^
      --output temp\\docling-production.json
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.ingestion.extraction.docling import DoclingContentAdapter
from backend.ingestion.utils import normalize_text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract one PDF with Docling only and write production-style JSON.")
    parser.add_argument("source_path", type=Path, help="PDF file to extract with Docling.")
    parser.add_argument("--agent-id", default="validation", help="Logical agent/domain ID.")
    parser.add_argument("--output", type=Path, default=Path("temp/docling-production.json"), help="JSON output path.")
    parser.add_argument("--ocr", action="store_true", help="Enable Docling OCR for scanned PDFs.")
    parser.add_argument("--no-formula", action="store_true", help="Disable Docling formula enrichment.")
    parser.add_argument("--code", action="store_true", help="Enable Docling code enrichment.")
    parser.add_argument("--chart", action="store_true", help="Enable Docling chart extraction if local models are available.")
    return parser.parse_args()


def configure_docling_environment(args: argparse.Namespace) -> None:
    """Sets Docling model options for this one standalone quality run."""
    os.environ["JLR_DOCLING_OCR"] = "true" if args.ocr else os.getenv("JLR_DOCLING_OCR", "false")
    os.environ["JLR_DOCLING_FORMULA_ENRICHMENT"] = "false" if args.no_formula else "true"
    os.environ["JLR_DOCLING_CODE_ENRICHMENT"] = "true" if args.code else os.getenv("JLR_DOCLING_CODE_ENRICHMENT", "false")
    os.environ["JLR_DOCLING_CHART_EXTRACTION"] = "true" if args.chart else os.getenv("JLR_DOCLING_CHART_EXTRACTION", "false")
    os.environ.setdefault("JLR_DOCLING_VERBOSE", "true")


def node_id(prefix: str, *parts: Any) -> str:
    safe = "_".join(re.sub(r"[^a-zA-Z0-9]+", "_", str(part)).strip("_") for part in parts if part is not None)
    return f"{prefix}_{safe}".strip("_")


def bbox_from(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def text_node(page_number: int, index: int, block: dict[str, Any], parent_id: str, document_id: str) -> dict[str, Any]:
    text = normalize_text(str(block.get("text") or "")) or ""
    block_type = str(block.get("type") or "paragraph")
    kind = "section" if block_type == "heading" else "list_item" if block_type == "list_item" else "paragraph"
    title = text if kind == "section" else None
    return {
        "node_id": node_id(kind, page_number, index),
        "node_type": kind,
        "parent_node_id": parent_id,
        "page_number": page_number,
        "order_index": index,
        "title": title,
        "text": text,
        "semantic_text": text,
        "hierarchy_path": f"Page {page_number}" + (f" > {title[:80]}" if title else ""),
        "display": {"render_type": "text"},
        "attributes": {
            "docling_item_type": block_type,
            "level": block.get("level"),
        },
        "provenance": {
            "document_id": document_id,
            "source_type": "pdf",
            "page_number": page_number,
            "bbox": bbox_from(block.get("bbox")),
            "extractor": "docling",
        },
        "quality": {
            "confidence": 0.9,
            "requires_review": False,
            "warnings": [],
        },
        "children": [],
    }


def table_cells(source_table: dict[str, Any]) -> list[dict[str, Any]]:
    data = source_table.get("data") if isinstance(source_table, dict) else None
    cells = data.get("table_cells") if isinstance(data, dict) else None
    return cells if isinstance(cells, list) else []


def cell_text(cell: dict[str, Any]) -> str:
    for key in ("text", "content", "value", "label"):
        value = cell.get(key)
        if isinstance(value, str) and normalize_text(value):
            return normalize_text(value) or ""
    return ""


def cell_row_col(cell: dict[str, Any]) -> tuple[int, int]:
    row = cell.get("start_row_offset_idx", cell.get("row_index", cell.get("row", 0)))
    col = cell.get("start_col_offset_idx", cell.get("column_index", cell.get("col", 0)))
    try:
        return int(row), int(col)
    except (TypeError, ValueError):
        return 0, 0


def matrix_from_table(table: dict[str, Any]) -> list[list[str]]:
    if isinstance(table.get("matrix"), list):
        return [[normalize_text(str(cell)) or "" for cell in row] for row in table["matrix"] if isinstance(row, list)]
    cells = table_cells(table.get("source_table") or {})
    if not cells:
        return []
    max_row = max((cell_row_col(cell)[0] for cell in cells), default=0)
    max_col = max((cell_row_col(cell)[1] for cell in cells), default=0)
    matrix = [["" for _ in range(max_col + 1)] for _ in range(max_row + 1)]
    for cell in cells:
        row, col = cell_row_col(cell)
        matrix[row][col] = cell_text(cell)
    return matrix


def table_node(page_number: int, index: int, table: dict[str, Any], parent_id: str, document_id: str) -> dict[str, Any]:
    table_id = node_id("table", page_number, index)
    matrix = matrix_from_table(table)
    headers = matrix[0] if matrix else []
    rows = matrix[1:] if len(matrix) > 1 else []
    children: list[dict[str, Any]] = []
    if headers:
        header_id = node_id("table_header", page_number, index)
        children.append({
            "node_id": header_id,
            "node_type": "table_header",
            "parent_node_id": table_id,
            "page_number": page_number,
            "order_index": 0,
            "title": None,
            "text": None,
            "semantic_text": " | ".join(headers),
            "hierarchy_path": f"Page {page_number} > Table {index} > Header",
            "display": {"render_type": "table_header"},
            "attributes": {"values": headers},
            "provenance": {"document_id": document_id, "source_type": "pdf", "page_number": page_number, "extractor": "docling"},
            "quality": {"confidence": 0.88, "requires_review": False, "warnings": []},
            "children": [],
        })
    for row_index, values in enumerate(rows, start=1):
        pairs = []
        for col_index, value in enumerate(values):
            header = headers[col_index] if col_index < len(headers) and headers[col_index] else f"column_{col_index + 1}"
            if value:
                pairs.append(f"{header}: {value}")
        children.append({
            "node_id": node_id("row", page_number, index, row_index),
            "node_type": "table_row",
            "parent_node_id": table_id,
            "page_number": page_number,
            "order_index": row_index,
            "title": None,
            "text": None,
            "semantic_text": "; ".join(pairs),
            "hierarchy_path": f"Page {page_number} > Table {index} > Row {row_index}",
            "display": {"render_type": "table_row"},
            "attributes": {
                "row_index": row_index,
                "cells": {headers[i] if i < len(headers) and headers[i] else f"column_{i + 1}": value for i, value in enumerate(values)},
            },
            "provenance": {"document_id": document_id, "source_type": "pdf", "page_number": page_number, "extractor": "docling"},
            "quality": {"confidence": 0.88, "requires_review": False, "warnings": []},
            "children": [],
        })
    warnings = list(table.get("warnings") or [])
    if table.get("requires_review"):
        warnings.append("Docling table export requires review.")
    return {
        "node_id": table_id,
        "node_type": "table",
        "parent_node_id": parent_id,
        "page_number": page_number,
        "order_index": index,
        "title": f"Table {index}",
        "text": None,
        "semantic_text": f"Table {index} on page {page_number} with {len(rows)} rows and {len(headers)} columns.",
        "hierarchy_path": f"Page {page_number} > Table {index}",
        "display": {
            "render_type": "table",
            "columns": [{"column_id": f"column_{i + 1}", "label": header or f"Column {i + 1}"} for i, header in enumerate(headers)],
        },
        "attributes": {
            "row_count": len(rows),
            "column_count": len(headers),
            "matrix": matrix,
            "docling_source_table_present": bool(table.get("source_table")),
        },
        "provenance": {
            "document_id": document_id,
            "source_type": "pdf",
            "page_number": page_number,
            "bbox": bbox_from(table.get("bbox")),
            "extractor": "docling",
        },
        "quality": {
            "confidence": 0.88 if matrix else 0.55,
            "requires_review": bool(table.get("requires_review") or not matrix),
            "warnings": warnings,
        },
        "children": children,
    }


def image_node(page_number: int, index: int, image: dict[str, Any], parent_id: str, document_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    image_id = node_id("visual", page_number, index)
    asset_id = node_id("asset_visual", page_number, index)
    caption = normalize_text(str(image.get("caption") or "")) or ""
    description = normalize_text(str(image.get("description") or "")) or ""
    classification = normalize_text(str(image.get("classification") or "")) or ""
    semantic = description or caption or f"Docling picture {index} on page {page_number}."
    node = {
        "node_id": image_id,
        "node_type": "image",
        "parent_node_id": parent_id,
        "page_number": page_number,
        "order_index": index,
        "title": caption or f"Image {index}",
        "text": caption,
        "semantic_text": semantic,
        "hierarchy_path": f"Page {page_number} > Image {index}",
        "display": {"render_type": "image", "asset_id": asset_id},
        "attributes": {
            "visual_object_type": classification or "picture",
            "caption": caption,
            "description": description,
            "classification": classification,
            "is_decorative": classification.lower() in {"decorative", "logo", "icon"},
        },
        "provenance": {
            "document_id": document_id,
            "source_type": "pdf",
            "page_number": page_number,
            "bbox": bbox_from(image.get("bbox")),
            "extractor": "docling",
        },
        "quality": {
            "confidence": 0.75 if (caption or description or image.get("bbox")) else 0.45,
            "requires_review": not bool(caption or description or image.get("bbox")),
            "warnings": [] if (caption or description or image.get("bbox")) else ["Picture has no caption, description, or bbox."],
        },
        "children": [],
    }
    asset = {
        "asset_id": asset_id,
        "asset_type": "docling_picture_reference",
        "source_node_id": image_id,
        "page_number": page_number,
        "locator": None,
        "public_url": None,
        "mime_type": None,
        "storage_status": "metadata_only",
        "bbox": bbox_from(image.get("bbox")),
        "quality": {
            "confidence": node["quality"]["confidence"],
            "warnings": ["Docling-only audit does not persist image crops."],
        },
    }
    return node, asset


def flatten_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []

    def visit(node: dict[str, Any]) -> None:
        flat.append(node)
        for child in node.get("children") or []:
            visit(child)

    for node in nodes:
        visit(node)
    return flat


def build_chunks(flat_nodes: list[dict[str, Any]], metadata: dict[str, Any]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for node in flat_nodes:
        node_type = node.get("node_type")
        if node_type not in {"paragraph", "section", "list_item", "table_row", "table", "image"}:
            continue
        content = normalize_text(str(node.get("semantic_text") or node.get("text") or "")) or ""
        if not content:
            continue
        heading = node.get("title") or node_type
        hierarchy = node.get("hierarchy_path") or ""
        chunk_id = node_id("chunk", node.get("node_id"))
        display_refs = {
            "page_number": node.get("page_number"),
            "node_id": node.get("node_id"),
            "asset_id": (node.get("display") or {}).get("asset_id"),
            "highlight_node_id": node.get("node_id"),
        }
        chunks.append({
            "chunk_id": chunk_id,
            "chunk_type": "visual" if node_type == "image" else "table_row" if node_type == "table_row" else "text",
            "heading": heading,
            "hierarchy_path": hierarchy,
            "content": content,
            "embedding_text": "\n".join(part for part in (str(heading), hierarchy, content) if part),
            "source_node_ids": [node.get("node_id")],
            "display_refs": display_refs,
            "chunk_metadata": {
                "document_id": metadata["document_id"],
                "agent_id": metadata["agent_id"],
                "source_file": metadata["source_file"],
                "node_type": node_type,
                "page_number": node.get("page_number"),
            },
            "quality": node.get("quality", {}),
        })
    return chunks


def build_production_json(source_path: Path, agent_id: str) -> dict[str, Any]:
    adapter = DoclingContentAdapter()
    result = adapter.extract(source_path)
    document_id = "docling_audit_" + re.sub(r"[^a-zA-Z0-9]+", "_", source_path.stem).strip("_").lower()
    metadata = {
        "document_id": document_id,
        "agent_id": agent_id,
        "source_file": source_path.name,
        "source_path": str(source_path),
        "source_type": "pdf",
        "title": source_path.stem,
        "schema_purpose": "docling_only_quality_audit",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "docling": {
            "extractor_name": result.extractor_name,
            "extraction_mode": result.extraction_mode,
            "processing_details": result.processing_details,
        },
    }
    root_id = node_id("document", source_path.stem)
    root = {
        "node_id": root_id,
        "node_type": "document",
        "parent_node_id": None,
        "page_number": None,
        "order_index": 0,
        "title": source_path.stem,
        "text": None,
        "semantic_text": source_path.stem,
        "hierarchy_path": source_path.stem,
        "display": {"render_type": "document"},
        "attributes": {"page_count": len(result.pages)},
        "provenance": {"document_id": document_id, "source_type": "pdf", "extractor": "docling"},
        "quality": {"confidence": 0.9 if not result.requires_review else 0.6, "requires_review": result.requires_review, "warnings": result.warnings},
        "children": [],
    }
    assets: list[dict[str, Any]] = []
    for page in result.pages:
        page_id = node_id("page", page.number)
        page_node = {
            "node_id": page_id,
            "node_type": "page",
            "parent_node_id": root_id,
            "page_number": page.number,
            "order_index": page.number,
            "title": f"Page {page.number}",
            "text": None,
            "semantic_text": f"Page {page.number}",
            "hierarchy_path": f"{source_path.stem} > Page {page.number}",
            "display": {"render_type": "page"},
            "attributes": {
                "text_block_count": len(page.text_blocks),
                "table_count": len(page.tables),
                "picture_count": len(page.images),
            },
            "provenance": {"document_id": document_id, "source_type": "pdf", "page_number": page.number, "extractor": "docling"},
            "quality": {"confidence": 0.9 if (page.text_blocks or page.tables or page.images) else 0.4, "requires_review": not bool(page.text_blocks or page.tables or page.images), "warnings": []},
            "children": [],
        }
        for index, block in enumerate(page.text_blocks, start=1):
            page_node["children"].append(text_node(page.number, index, block, page_id, document_id))
        for index, table in enumerate(page.tables, start=1):
            page_node["children"].append(table_node(page.number, index, table, page_id, document_id))
        for index, image in enumerate(page.images, start=1):
            visual_node, asset = image_node(page.number, index, image, page_id, document_id)
            page_node["children"].append(visual_node)
            assets.append(asset)
        root["children"].append(page_node)
    nodes = [root]
    flat_nodes = flatten_nodes(nodes)
    chunks = build_chunks(flat_nodes, metadata)
    counts = Counter(node.get("node_type") for node in flat_nodes)
    quality_warnings = list(result.warnings)
    quality_warnings.extend(
        f"{node['node_type']} {node['node_id']}: {warning}"
        for node in flat_nodes
        for warning in (node.get("quality") or {}).get("warnings", [])
    )
    return {
        "schema_version": "docling_production_audit_v1",
        "metadata": metadata,
        "nodes": nodes,
        "assets": assets,
        "chunks": chunks,
        "quality": {
            "extraction_status": result.processing_details.get("conversion_status", "unknown"),
            "primary_extractor": "docling",
            "fallback_extractors": [],
            "requires_review": result.requires_review or any((node.get("quality") or {}).get("requires_review") for node in flat_nodes),
            "warnings": quality_warnings,
            "errors": [],
            "counts": {
                "pages": len(result.pages),
                "text_blocks": sum(len(page.text_blocks) for page in result.pages),
                "tables": sum(len(page.tables) for page in result.pages),
                "pictures": sum(len(page.images) for page in result.pages),
                "nodes_by_type": dict(counts),
                "chunks": len(chunks),
                "assets": len(assets),
            },
        },
        "raw_docling_summary": {
            "markdown_chars": len(result.markdown or ""),
            "exported_top_level_keys": sorted((result.exported or {}).keys()),
        },
    }


def main() -> int:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    if args.source_path.suffix.lower() != ".pdf":
        raise ValueError("This Docling quality audit script currently expects a PDF input.")
    if not args.source_path.is_file():
        raise FileNotFoundError(args.source_path)
    configure_docling_environment(args)
    print(f"Using DOCLING_ARTIFACTS_PATH={os.getenv('DOCLING_ARTIFACTS_PATH')}", flush=True)
    payload = build_production_json(args.source_path, args.agent_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    counts = payload["quality"]["counts"]
    print(f"Docling-only production JSON: {args.output}")
    print(f"Status: {payload['quality']['extraction_status']}")
    print(f"Pages: {counts['pages']}; text blocks: {counts['text_blocks']}; tables: {counts['tables']}; pictures: {counts['pictures']}")
    print(f"Nodes: {sum(counts['nodes_by_type'].values())}; chunks: {counts['chunks']}; assets: {counts['assets']}")
    print(f"Warnings: {len(payload['quality']['warnings'])}; requires_review: {payload['quality']['requires_review']}")
    return 0 if not payload["quality"]["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
