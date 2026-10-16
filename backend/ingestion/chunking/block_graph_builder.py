"""Builds embedding chunks from the persisted Textract-style BlockGraph shape.

The BlockGraph is now the compact retrieval/indexing contract. This builder
keeps the same EmbeddingChunk output expected by embedding and Qdrant layers,
but derives narrative, table-row, and visual chunks from flat Blocks,
Relationships, and Assets instead of walking the nested canonical tree directly.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from backend.ingestion.block_graph import BlockGraphBuilder
from backend.ingestion.chunking.contracts import ChunkBuildResult
from backend.ingestion.chunking.contracts import ChunkRejection
from backend.ingestion.chunking.contracts import ChunkType
from backend.ingestion.chunking.contracts import ChunkingPolicy
from backend.ingestion.chunking.contracts import EmbeddingChunk
from backend.ingestion.models import CanonicalDocument
from backend.ingestion.utils import normalize_text


class BlockGraphChunkBuilder:
    """Creates deterministic retrieval chunks from a compact BlockGraph."""

    _NARRATIVE_BLOCKS = {
        "LAYOUT_TEXT", "LAYOUT_LIST", "LAYOUT_TITLE", "NOTE", "CAPTION",
        "GUIDELINE_BLOCK", "REFERENCE_BLOCK", "MEASUREMENT_BLOCK", "KEY_VALUE_SET",
    }
    _VISUAL_BLOCKS = {"IMAGE", "CHART"}

    def __init__(self, policy: ChunkingPolicy | None = None) -> None:
        self._policy = policy or ChunkingPolicy(policy_version="block-graph-chunks-v1")
        if self._policy.policy_version == "canonical-chunks-v2":
            self._policy = self._policy.model_copy(update={"policy_version": "block-graph-chunks-v1"})

    def build(self, document: CanonicalDocument) -> ChunkBuildResult:
        """Builds chunks from the document's BlockGraph representation."""
        result = ChunkBuildResult(policy_version=self._policy.policy_version)
        if self._policy.strict_quality_gate and document.quality.errors:
            result.rejected.append(ChunkRejection(
                reason="Document has blocking extraction or reconciliation errors.",
                severity="error",
            ))
            return result
        graph = BlockGraphBuilder().build(document)
        chunks, rejected = self.build_from_graph(graph)
        result.chunks.extend(chunks)
        result.rejected.extend(rejected)
        return result

    def build_from_graph(self, graph: dict[str, Any]) -> tuple[list[EmbeddingChunk], list[ChunkRejection]]:
        """Builds chunks from a serialized BlockGraph dictionary."""
        metadata = graph.get("DocumentMetadata") if isinstance(graph.get("DocumentMetadata"), dict) else {}
        blocks = [block for block in graph.get("Blocks", []) if isinstance(block, dict)]
        assets = [asset for asset in graph.get("Assets", []) if isinstance(asset, dict)]
        by_id = {str(block.get("Id")): block for block in blocks if block.get("Id")}
        children = {block_id: self._relationship_ids(block, "CHILD") for block_id, block in by_id.items()}
        parent = self._parent_map(children)
        assets_by_block = self._assets_by_block(assets)
        rejected: list[ChunkRejection] = []
        chunks: list[EmbeddingChunk] = []

        for table in (block for block in blocks if block.get("BlockType") == "TABLE"):
            if self._requires_review(table):
                rejected.append(ChunkRejection(reason="Table is marked incomplete or conflicting and requires review.",
                    node_id=str(table.get("Id") or ""), severity="warning"))
                continue
            chunks.extend(self._table_chunks(metadata, table, by_id, children, parent))

        table_descendants = self._descendants_for_types(blocks, children, {"TABLE"})
        for block in blocks:
            if block.get("BlockType") not in self._VISUAL_BLOCKS:
                continue
            if self._requires_review(block):
                rejected.append(ChunkRejection(reason="Image-derived content requires OCR, VLM, or human review before embedding.",
                    node_id=str(block.get("Id") or ""), severity="warning"))
                continue
            chunk = self._visual_chunk(metadata, block, parent, by_id, assets_by_block)
            if chunk:
                chunks.append(chunk)

        narrative_groups: dict[str, list[dict[str, Any]]] = {}
        for block in blocks:
            block_id = str(block.get("Id") or "")
            if block_id in table_descendants or block.get("BlockType") not in self._NARRATIVE_BLOCKS:
                continue
            if self._requires_review(block):
                continue
            if normalize_text(str(block.get("Text") or "")):
                narrative_groups.setdefault(parent.get(block_id) or block_id, []).append(block)
        for group in narrative_groups.values():
            chunks.extend(self._narrative_chunks(metadata, group, parent, by_id))
        return chunks, rejected

    def _table_chunks(self, metadata: dict[str, Any], table: dict[str, Any], by_id: dict[str, dict[str, Any]],
                      children: dict[str, list[str]], parent: dict[str, str]) -> list[EmbeddingChunk]:
        table_id = str(table.get("Id") or "")
        headers = self._table_headers(table, by_id, children)
        header_context = [" | ".join(headers)] if headers else []
        prefix = self._context_prefix(metadata, self._breadcrumbs(table_id, parent, by_id), table.get("Title") or "Table")
        chunks: list[EmbeddingChunk] = []
        for row_id in children.get(table_id, []):
            row = by_id.get(row_id, {})
            if row.get("BlockType") != "ROW" or self._requires_review(row):
                continue
            values = self._row_text(row, by_id, children, headers)
            if not values:
                continue
            for part_number, part in enumerate(self._split_text(values, self._policy.max_table_row_characters), start=1):
                row_children = [cid for cid in children.get(row_id, []) if by_id.get(cid, {}).get("BlockType") == "CELL"]
                metadata_fields = {
                    "breadcrumbs": self._breadcrumbs(row_id, parent, by_id),
                    "table_title": table.get("Title"),
                    "header_context": header_context,
                    "row_index": self._attributes(row).get("row_index"),
                    "part_number": part_number,
                    "field_policies": [],
                    **self._source_metadata(metadata, row),
                }
                chunks.append(self._new_chunk(metadata, ChunkType.TABLE_ROW, table_id,
                    [(row_id, part), *[(cell_id, "") for cell_id in row_children]], prefix,
                    metadata_fields["breadcrumbs"], extra_metadata=metadata_fields))
        return chunks

    def _visual_chunk(self, metadata: dict[str, Any], block: dict[str, Any], parent: dict[str, str],
                      by_id: dict[str, dict[str, Any]], assets_by_block: dict[str, list[dict[str, Any]]]) -> EmbeddingChunk | None:
        block_id = str(block.get("Id") or "")
        parts = self._visual_parts(block, assets_by_block.get(block_id, []))
        if not parts:
            return None
        text = "\n".join(parts)
        breadcrumbs = self._breadcrumbs(block_id, parent, by_id)
        prefix = self._context_prefix(metadata, breadcrumbs, block.get("Title") or block.get("BlockType") or "Visual")
        asset = next((item for item in assets_by_block.get(block_id, []) if item.get("Path")), {})
        attrs = self._attributes(block)
        visual_meta = {
            "breadcrumbs": breadcrumbs,
            "visual_node_type": str(block.get("BlockType") or "").lower(),
            "visual_object_type": self._visual_object_type(block, asset),
            "ocr_status": attrs.get("ocr_status"),
            "image_description_status": attrs.get("image_description_status") or asset.get("Status"),
            "image_description_is_inferred": bool(attrs.get("image_description_is_inferred")),
            "vlm_confidence": attrs.get("vlm_confidence") or asset.get("VlmConfidence"),
            "image_path": asset.get("Path") or attrs.get("saved_path"),
            "image_storage_status": attrs.get("image_storage_status"),
            "is_decorative": attrs.get("is_decorative") or asset.get("IsDecorative"),
            **self._source_metadata(metadata, block),
        }
        return self._new_chunk(metadata, ChunkType.VISUAL, block_id, [(block_id, text)], prefix, breadcrumbs, extra_metadata=visual_meta)

    def _narrative_chunks(self, metadata: dict[str, Any], blocks: list[dict[str, Any]], parent: dict[str, str],
                          by_id: dict[str, dict[str, Any]]) -> list[EmbeddingChunk]:
        parent_id = parent.get(str(blocks[0].get("Id") or "")) or str(blocks[0].get("Id") or "")
        breadcrumbs = self._breadcrumbs(str(blocks[0].get("Id") or ""), parent, by_id)
        prefix = self._context_prefix(metadata, breadcrumbs)
        limit = max(1, self._policy.max_chunk_characters - len(prefix))
        chunks: list[EmbeddingChunk] = []
        current: list[tuple[str, str]] = []
        overlap: list[tuple[str, str]] = []
        current_size = 0
        for block in blocks:
            block_id = str(block.get("Id") or "")
            for fragment in self._split_text(normalize_text(str(block.get("Text") or "")), limit):
                if current and current_size + len(fragment) + 1 > limit:
                    chunks.append(self._new_chunk(metadata, ChunkType.NARRATIVE, parent_id, current, prefix, breadcrumbs,
                        overlap_node_ids=[node_id for node_id, _ in overlap]))
                    overlap = self._overlap_nodes(current)
                    current = [*overlap]
                    current_size = sum(len(text) + 1 for _, text in current)
                current.append((block_id, fragment))
                current_size += len(fragment) + 1
        if current:
            chunks.append(self._new_chunk(metadata, ChunkType.NARRATIVE, parent_id, current, prefix, breadcrumbs,
                overlap_node_ids=[node_id for node_id, _ in overlap]))
        return chunks

    def _visual_parts(self, block: dict[str, Any], assets: list[dict[str, Any]]) -> list[str]:
        attrs = self._attributes(block)
        gate = attrs.get("vlm_gate") if isinstance(attrs.get("vlm_gate"), dict) else {}
        if attrs.get("is_decorative") or gate.get("reason") == "decorative_or_small_visual" or any(a.get("IsDecorative") for a in assets):
            return []
        parts: list[str] = []
        ocr_text = normalize_text(str(attrs.get("ocr_text") or next((a.get("OcrText") for a in assets if a.get("OcrText")), "") or ""))
        vlm_text = normalize_text(str(attrs.get("vlm_transcribed_text") or ""))
        description = normalize_text(str(attrs.get("image_description") or next((a.get("Description") for a in assets if a.get("Description")), "") or ""))
        nearby = normalize_text(str(attrs.get("nearby_text") or ""))
        text = normalize_text(str(block.get("Text") or ""))
        if text:
            parts.append(f"Visual block text: {text}")
        if ocr_text:
            parts.append(f"OCR visible text: {ocr_text}")
        if vlm_text and vlm_text != ocr_text:
            parts.append(f"VLM transcribed visible text: {vlm_text}")
        if description:
            parts.append(f"AI-inferred image description: {description}")
        if nearby:
            parts.append(f"Nearby source context: {nearby}")
        return parts

    def _table_headers(self, table: dict[str, Any], by_id: dict[str, dict[str, Any]], children: dict[str, list[str]]) -> list[str]:
        for child_id in children.get(str(table.get("Id") or ""), []):
            child = by_id.get(child_id, {})
            if child.get("BlockType") == "TABLE_HEADER":
                rows = self._attributes(child).get("header_rows") or []
                width = max((len(row) for row in rows if isinstance(row, list)), default=0)
                return [" / ".join(str(row[index]) for row in rows if isinstance(row, list) and index < len(row) and row[index])
                        for index in range(width)]
        columns = self._attributes(table).get("columns") or []
        return [str(column) for column in columns]

    def _row_text(self, row: dict[str, Any], by_id: dict[str, dict[str, Any]], children: dict[str, list[str]], headers: list[str]) -> str:
        pairs: list[str] = []
        for index, cell_id in enumerate(children.get(str(row.get("Id") or ""), [])):
            cell = by_id.get(cell_id, {})
            if cell.get("BlockType") != "CELL":
                continue
            attrs = self._attributes(cell)
            value = normalize_text(str(cell.get("Text") or attrs.get("value") or attrs.get("normalized_value") or ""))
            if value:
                label = headers[index] if index < len(headers) and headers[index] else str(attrs.get("column_name") or f"column_{index + 1}")
                pairs.append(f"{label}: {value}")
        if pairs:
            return " | ".join(pairs)
        return normalize_text(str(self._attributes(row).get("semantic_text") or ""))

    def _new_chunk(self, document_metadata: dict[str, Any], chunk_type: ChunkType, parent_node_id: str,
                   parts: list[tuple[str, str]], prefix: str, breadcrumbs: list[str], *,
                   metadata_fields: dict[str, object] | None = None, extra_metadata: dict[str, object] | None = None,
                   overlap_node_ids: list[str] | None = None) -> EmbeddingChunk:
        extra_metadata = extra_metadata if extra_metadata is not None else metadata_fields or {}
        content = "\n".join(text for _, text in parts if text).strip()
        node_ids = list(dict.fromkeys(node_id for node_id, _ in parts))
        combined_metadata = {
            "breadcrumbs": list(breadcrumbs),
            "overlap_node_ids": overlap_node_ids or [],
            "references": [],
            "answer_eligible_references": [],
            **self._document_metadata(document_metadata),
            **extra_metadata,
        }
        identity = "|".join((self._policy.policy_version, str(document_metadata.get("DocumentId") or ""), str(document_metadata.get("Version") or ""),
                             chunk_type.value, parent_node_id, *node_ids, content))
        return EmbeddingChunk(
            chunk_id=f"chk_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}",
            chunk_type=chunk_type,
            document_id=str(document_metadata.get("DocumentId") or ""),
            agent_id=str(document_metadata.get("AgentId") or ""),
            source_type=str(document_metadata.get("SourceType") or ""),
            source_version=str(document_metadata.get("Version") or ""),
            parent_node_id=parent_node_id,
            source_node_ids=node_ids,
            content_text=content,
            embedding_text=f"{prefix}{content}".strip(),
            metadata=combined_metadata,
        )

    def _document_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        return {
            "security_scope": metadata.get("SecurityScope") or {"classification": "internal", "allowed_groups": [], "allowed_users": [], "tags": []},
            "source_hash": metadata.get("SourceHash"),
        }

    def _source_metadata(self, metadata: dict[str, Any], block: dict[str, Any]) -> dict[str, object]:
        return {
            **self._document_metadata(metadata),
            "page_number": block.get("Page"),
            "slide_number": block.get("Slide"),
            "sheet_name": block.get("Sheet"),
            "cell_range": block.get("CellRange"),
        }

    def _context_prefix(self, metadata: dict[str, Any], breadcrumbs: list[str], extra: object | None = None) -> str:
        labels = [label for label in (*breadcrumbs, normalize_text(str(extra or ""))) if label]
        return f"Document: {metadata.get('Title') or metadata.get('SourceName') or metadata.get('FileName')}\nContext: {' > '.join(labels)}\nContent: "

    def _breadcrumbs(self, block_id: str, parent: dict[str, str], by_id: dict[str, dict[str, Any]]) -> list[str]:
        chain: list[str] = []
        cursor = block_id
        seen: set[str] = set()
        while cursor and cursor not in seen:
            seen.add(cursor)
            block = by_id.get(cursor, {})
            label = normalize_text(str(block.get("Title") or block.get("Text") or ""))
            if label:
                chain.append(label[:120])
            cursor = parent.get(cursor, "")
        return list(reversed(chain))

    def _relationship_ids(self, block: dict[str, Any], relation_type: str) -> list[str]:
        relationships = block.get("Relationships") if isinstance(block.get("Relationships"), list) else []
        for relation in relationships:
            if isinstance(relation, dict) and relation.get("Type") == relation_type and isinstance(relation.get("Ids"), list):
                return [str(item) for item in relation.get("Ids")]
        return []

    def _parent_map(self, children: dict[str, list[str]]) -> dict[str, str]:
        parent: dict[str, str] = {}
        for parent_id, child_ids in children.items():
            for child_id in child_ids:
                parent.setdefault(child_id, parent_id)
        return parent

    def _assets_by_block(self, assets: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for asset in assets:
            block_id = str(asset.get("BlockId") or "")
            if block_id:
                grouped.setdefault(block_id, []).append(asset)
        return grouped

    def _descendants_for_types(self, blocks: list[dict[str, Any]], children: dict[str, list[str]], block_types: set[str]) -> set[str]:
        by_id = {str(block.get("Id")): block for block in blocks if block.get("Id")}
        descendants: set[str] = set()
        for block in blocks:
            if block.get("BlockType") not in block_types:
                continue
            stack = list(children.get(str(block.get("Id") or ""), []))
            while stack:
                current = stack.pop()
                if current in descendants:
                    continue
                descendants.add(current)
                stack.extend(children.get(current, []))
        return descendants

    def _attributes(self, block: dict[str, Any]) -> dict[str, Any]:
        attrs = block.get("Attributes")
        return attrs if isinstance(attrs, dict) else {}

    def _requires_review(self, block: dict[str, Any]) -> bool:
        attrs = self._attributes(block)
        if attrs.get("retrieval_allowed") is False:
            return True
        reconciliation = attrs.get("reconciliation")
        events = reconciliation if isinstance(reconciliation, list) else [reconciliation]
        return bool(attrs.get("requires_review")) or any(isinstance(event, dict) and event.get("decision") == "conflict" for event in events)

    def _visual_object_type(self, block: dict[str, Any], asset: dict[str, Any]) -> str:
        fields = " ".join(str(value or "") for value in (
            block.get("BlockType"), block.get("Title"), asset.get("AssetType"), asset.get("Classification"),
            asset.get("ImageType"), self._attributes(block).get("classification"), self._attributes(block).get("image_type"),
            self._attributes(block).get("shape_name"), self._attributes(block).get("nearby_text"),
        )).lower()
        if asset.get("IsDecorative") or self._attributes(block).get("is_decorative"):
            return "icon"
        for name, keywords in {
            "formula": ("formula", "equation", "math"),
            "chart": ("chart", "graph", "plot"),
            "architecture_diagram": ("architecture",),
            "flow_diagram": ("flow", "process"),
            "cad_3d_visual": ("cad", "3d"),
            "diagram": ("diagram", "figure"),
            "screenshot": ("screenshot",),
            "table_snapshot": ("table_crop",),
        }.items():
            if any(keyword in fields for keyword in keywords):
                return name
        return "image" if block.get("BlockType") == "IMAGE" else "visual"

    def _split_text(self, text: str, limit: int) -> list[str]:
        text = normalize_text(text)
        if not text:
            return []
        if len(text) <= limit:
            return [text]
        sentences = re.split(r"(?<=[.!?])\s+", text)
        parts: list[str] = []
        current = ""
        for sentence in sentences:
            if current and len(current) + len(sentence) + 1 > limit:
                parts.append(current.strip())
                current = sentence
            else:
                current = f"{current} {sentence}".strip()
        if current:
            parts.append(current.strip())
        return parts or [text[:limit]]

    def _overlap_nodes(self, current: list[tuple[str, str]]) -> list[tuple[str, str]]:
        overlap: list[tuple[str, str]] = []
        size = 0
        for item in reversed(current):
            if size + len(item[1]) > self._policy.max_overlap_characters:
                break
            overlap.insert(0, item)
            size += len(item[1])
        return overlap

