"""Structure-aware canonical JSON chunk builder for retrieval and embeddings.

Narrative nodes are grouped only under their existing canonical parent. Table
rows become independent chunks with table/header context. The builder creates
parent links and security metadata but does not embed, index, or call an LLM.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from backend.ingestion.chunking.contracts import ChunkBuildResult
from backend.ingestion.chunking.contracts import ChunkRejection
from backend.ingestion.chunking.contracts import ChunkType
from backend.ingestion.chunking.contracts import ChunkingPolicy
from backend.ingestion.chunking.contracts import EmbeddingChunk
from backend.ingestion.models import CanonicalDocument
from backend.ingestion.models import CanonicalNode
from backend.ingestion.models import NodeType
from backend.ingestion.utils import normalize_text


@dataclass(frozen=True, slots=True)
class _NodeEntry:
    """Keeps traversal context private while chunks are planned."""

    node: CanonicalNode
    parent_node_id: str
    breadcrumbs: tuple[str, ...]
    blocked: bool


class CanonicalChunkBuilder:
    """Builds deterministic child chunks while preserving canonical parent relationships."""

    _NARRATIVE_TYPES = {
        NodeType.PARAGRAPH, NodeType.TEXT_BLOCK, NodeType.BULLET_BLOCK,
        NodeType.TITLE_BLOCK, NodeType.NOTE_BLOCK, NodeType.GUIDELINE_BLOCK,
        NodeType.REFERENCE_BLOCK, NodeType.MEASUREMENT_BLOCK,
        NodeType.LABEL_VALUE_BLOCK, NodeType.STATUS_BLOCK, NodeType.CAPTION,
    }

    def __init__(self, policy: ChunkingPolicy | None = None) -> None:
        """Configures stable limits rather than depending on a model tokeniser."""
        self._policy = policy or ChunkingPolicy()

    def build(self, document: CanonicalDocument) -> ChunkBuildResult:
        """Creates chunks or rejects a document whose quality state is not safe to embed."""
        result = ChunkBuildResult(policy_version=self._policy.policy_version)
        if self._policy.strict_quality_gate and document.quality.errors:
            result.rejected.append(ChunkRejection(reason="Document has blocking extraction or reconciliation errors.",
                severity="error"))
            return result
        entries = self._entries(document)
        table_node_ids = {entry.node.node_id for entry in entries if entry.node.node_type == NodeType.TABLE}
        for entry in entries:
            if entry.node.node_type != NodeType.TABLE:
                continue
            if entry.blocked:
                result.rejected.append(ChunkRejection(reason="Table is marked incomplete or conflicting and requires review.",
                    node_id=entry.node.node_id, severity="warning"))
                continue
            result.chunks.extend(self._table_chunks(document, entry))
        for entry in entries:
            if entry.node.node_type != NodeType.IMAGE:
                continue
            if entry.blocked:
                result.rejected.append(ChunkRejection(reason="Image-derived content requires OCR, VLM, or human review before embedding.",
                    node_id=entry.node.node_id, severity="warning"))
                continue
            visual_chunk = self._visual_chunk(document, entry)
            if visual_chunk:
                result.chunks.append(visual_chunk)
        narrative_groups: dict[str, list[_NodeEntry]] = {}
        for entry in entries:
            if entry.blocked or entry.node.node_type not in self._NARRATIVE_TYPES:
                continue
            if entry.parent_node_id in table_node_ids:
                continue
            if normalize_text(entry.node.text):
                narrative_groups.setdefault(entry.parent_node_id, []).append(entry)
        for group in narrative_groups.values():
            result.chunks.extend(self._narrative_chunks(document, group))
        return result

    def _entries(self, document: CanonicalDocument) -> list[_NodeEntry]:
        """Walks canonical nodes in source order and inherits unsafe ancestor status."""
        entries: list[_NodeEntry] = []

        def visit(node: CanonicalNode, parent_id: str, breadcrumbs: tuple[str, ...], inherited_block: bool) -> None:
            title = normalize_text(node.title)
            next_breadcrumbs = (*breadcrumbs, title) if title else breadcrumbs
            blocked = inherited_block or self._requires_review(node)
            entries.append(_NodeEntry(node=node, parent_node_id=parent_id, breadcrumbs=next_breadcrumbs, blocked=blocked))
            for child in node.children:
                visit(child, node.node_id, next_breadcrumbs, blocked)

        for root in document.root_nodes:
            visit(root, root.node_id, tuple(filter(None, [normalize_text(document.metadata.title)])), False)
        return entries

    def _requires_review(self, node: CanonicalNode) -> bool:
        """Prevents conflict-marked or unreadable canonical content from reaching embeddings."""
        if node.attributes.get("retrieval_allowed") is False:
            return True
        reconciliation = node.attributes.get("reconciliation")
        events = reconciliation if isinstance(reconciliation, list) else [reconciliation]
        return bool(node.attributes.get("requires_review")) or any(
            isinstance(event, dict) and event.get("decision") == "conflict" for event in events
        )

    def _narrative_chunks(self, document: CanonicalDocument, entries: list[_NodeEntry]) -> list[EmbeddingChunk]:
        """Groups ordered semantic nodes under one parent, with node-boundary overlap."""
        parent_id = entries[0].parent_node_id
        breadcrumb = entries[0].breadcrumbs
        prefix = self._context_prefix(document, breadcrumb)
        limit = max(1, self._policy.max_chunk_characters - len(prefix))
        chunks: list[EmbeddingChunk] = []
        current: list[tuple[str, str]] = []
        overlap: list[tuple[str, str]] = []
        current_size = 0
        for entry in entries:
            for fragment in self._split_text(normalize_text(entry.node.text) or "", limit):
                if current and current_size + len(fragment) + 1 > limit:
                    chunks.append(self._new_chunk(document, ChunkType.NARRATIVE, parent_id, current, prefix, breadcrumb,
                        overlap_node_ids=[node_id for node_id, _ in overlap]))
                    overlap = self._overlap_nodes(current)
                    current = [*overlap]
                    current_size = sum(len(text) + 1 for _, text in current)
                current.append((entry.node.node_id, fragment))
                current_size += len(fragment) + 1
        if current:
            chunks.append(self._new_chunk(document, ChunkType.NARRATIVE, parent_id, current, prefix, breadcrumb,
                overlap_node_ids=[node_id for node_id, _ in overlap]))
        return chunks

    def _visual_chunk(self, document: CanonicalDocument, entry: _NodeEntry) -> EmbeddingChunk | None:
        """Creates one retrieval chunk from OCR text and VLM-derived visual evidence."""
        node = entry.node
        parts = self._visual_parts(node)
        if not parts:
            return None
        text = "\n".join(parts)
        prefix = self._context_prefix(document, entry.breadcrumbs, node.title or "Image")
        metadata = {"breadcrumbs": list(entry.breadcrumbs), "visual_node_type": node.node_type.value,
            "ocr_status": node.attributes.get("ocr_status"), "image_description_status": node.attributes.get("image_description_status"),
            "image_description_is_inferred": bool(node.attributes.get("image_description_is_inferred")),
            "vlm_confidence": node.attributes.get("vlm_confidence"),
            "image_path": node.attributes.get("saved_path"),
            "image_storage_status": node.attributes.get("image_storage_status"),
            **self._source_metadata(document, node)}
        return self._new_chunk(document, ChunkType.VISUAL, node.node_id, [(node.node_id, text)], prefix,
            entry.breadcrumbs, metadata=metadata)

    def _visual_parts(self, node: CanonicalNode) -> list[str]:
        """Keeps OCR/source-like text separate from VLM-inferred description inside one chunk."""
        parts: list[str] = []
        ocr_text = normalize_text(str(node.attributes.get("ocr_text") or ""))
        vlm_text = normalize_text(str(node.attributes.get("vlm_transcribed_text") or ""))
        description = normalize_text(str(node.attributes.get("image_description") or ""))
        nearby = normalize_text(str(node.attributes.get("nearby_text") or ""))
        if ocr_text:
            parts.append(f"OCR visible text: {ocr_text}")
        if vlm_text and vlm_text != ocr_text:
            parts.append(f"VLM transcribed visible text: {vlm_text}")
        if description:
            parts.append(f"AI-inferred image description: {description}")
        if nearby:
            parts.append(f"Nearby source context: {nearby}")
        return parts

    def _table_chunks(self, document: CanonicalDocument, entry: _NodeEntry) -> list[EmbeddingChunk]:
        """Creates row-level chunks with header context for structured and Excel tables."""
        table = entry.node
        headers = self._table_headers(table)
        header_context = self._table_header_context(table, headers)
        prefix = self._context_prefix(document, entry.breadcrumbs, table.title or "Table")
        chunks: list[EmbeddingChunk] = []
        for row in (child for child in table.children if child.node_type == NodeType.TABLE_ROW):
            if self._requires_review(row):
                continue
            values = self._row_text(row, headers)
            if not values:
                continue
            for part_number, part in enumerate(self._split_text(values, self._policy.max_table_row_characters), start=1):
                metadata = {"breadcrumbs": list(entry.breadcrumbs), "table_title": table.title,
                    "header_context": header_context, "row_index": row.attributes.get("row_index"),
                    "part_number": part_number, "field_policies": self._field_policies(row),
                    **self._source_metadata(document, row)}
                chunks.append(self._new_chunk(document, ChunkType.TABLE_ROW, table.node_id,
                    [(row.node_id, part), *[(cell.node_id, "") for cell in row.children]], prefix,
                    entry.breadcrumbs, metadata=metadata))
        return chunks

    def _table_headers(self, table: CanonicalNode) -> list[str]:
        """Builds one search label per data column from normal or structured headers."""
        header = next((child for child in table.children if child.node_type == NodeType.TABLE_HEADER), None)
        if header:
            rows = [row for row in header.attributes.get("header_rows", []) if row]
            width = max((len(row) for row in rows), default=0)
            return [" / ".join(str(row[index]) for row in rows if index < len(row) and row[index])
                    for index in range(width)]
        flagged = [child.text or "" for row in table.children for child in row.children
                   if child.node_type == NodeType.TABLE_CELL and child.attributes.get("column_header")]
        return flagged or list(table.attributes.get("columns", []))

    def _table_header_context(self, table: CanonicalNode, headers: list[str]) -> list[str]:
        """Keeps full multi-row header evidence alongside per-column search labels."""
        header = next((child for child in table.children if child.node_type == NodeType.TABLE_HEADER), None)
        if header:
            return [" | ".join(str(value) for value in row) for row in header.attributes.get("header_rows", []) if row]
        return headers

    def _row_text(self, row: CanonicalNode, headers: list[str]) -> str:
        """Makes every table value searchable while retaining source cell identity separately."""
        cells = [cell for cell in row.children if cell.node_type == NodeType.TABLE_CELL]
        if cells:
            pairs = []
            for index, cell in enumerate(cells):
                if cell.attributes.get("formula_error") or cell.attributes.get("retrieval_allowed") is False:
                    continue
                policy = cell.attributes.get("field_policy", {})
                if isinstance(policy, dict) and not policy.get("retrieval_allowed", True):
                    continue
                value = normalize_text(cell.text) or normalize_text(str(cell.attributes.get("value", ""))) or ""
                if value:
                    label = headers[index] if index < len(headers) else str(cell.attributes.get("column_name") or f"column_{index + 1}")
                    pairs.append(f"{label}: {value}")
            return " | ".join(pairs)
        return normalize_text(str(row.attributes.get("semantic_text") or "")) or ""

    def _new_chunk(self, document: CanonicalDocument, chunk_type: ChunkType, parent_node_id: str,
                   parts: list[tuple[str, str]], prefix: str, breadcrumbs: tuple[str, ...], *,
                   metadata: dict[str, object] | None = None, overlap_node_ids: list[str] | None = None) -> EmbeddingChunk:
        """Creates a stable child record and keeps display text separate from embedding context."""
        content = "\n".join(text for _, text in parts if text).strip()
        node_ids = list(dict.fromkeys(node_id for node_id, _ in parts))
        combined_metadata = {"breadcrumbs": list(breadcrumbs), "overlap_node_ids": overlap_node_ids or [],
                             "references": self._references_for_nodes(document, node_ids),
                             **self._source_metadata(document), **(metadata or {})}
        combined_metadata["answer_eligible_references"] = [reference for reference in combined_metadata["references"]
            if reference.get("answer_eligible")]
        identity = "|".join((self._policy.policy_version, document.document_id, document.version, chunk_type.value,
                             parent_node_id, *node_ids, content))
        return EmbeddingChunk(chunk_id=f"chk_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}",
            chunk_type=chunk_type, document_id=document.document_id, agent_id=document.agent_id,
            source_type=document.source_type.value, source_version=document.version, parent_node_id=parent_node_id,
            source_node_ids=node_ids, content_text=content, embedding_text=f"{prefix}{content}".strip(), metadata=combined_metadata)

    def _context_prefix(self, document: CanonicalDocument, breadcrumbs: tuple[str, ...], extra: str | None = None) -> str:
        """Adds compact parent context without copying an entire source document into every chunk."""
        labels = [label for label in (*breadcrumbs, normalize_text(extra)) if label]
        return f"Document: {document.metadata.title or document.source_name}\nContext: {' > '.join(labels)}\nContent: "

    def _source_metadata(self, document: CanonicalDocument, node: CanonicalNode | None = None) -> dict[str, object]:
        """Carries retrieval filters and citation coordinates with every chunk."""
        provenance = node.provenance if node else None
        return {"security_scope": document.security_scope.model_dump(mode="json"), "source_hash": document.source_hash,
            "page_number": provenance.page_number if provenance else None, "slide_number": provenance.slide_number if provenance else None,
            "sheet_name": provenance.sheet_name if provenance else None, "cell_range": provenance.cell_range if provenance else None}

    def _field_policies(self, row: CanonicalNode) -> list[dict[str, object]]:
        """Carries display policy to answer generation without hiding source values from audit."""
        return [dict(cell.attributes.get("field_policy", {})) for cell in row.children
                if cell.node_type == NodeType.TABLE_CELL and isinstance(cell.attributes.get("field_policy"), dict)]

    def _references_for_nodes(self, document: CanonicalDocument, node_ids: list[str]) -> list[dict[str, object]]:
        """Copies registered references from contributing canonical nodes into chunk metadata."""
        wanted = set(node_ids)
        references: list[dict[str, object]] = []

        def visit(node: CanonicalNode) -> None:
            if node.node_id in wanted:
                values = node.attributes.get("references", [])
                if isinstance(values, list):
                    references.extend(value for value in values if isinstance(value, dict))
            for child in node.children:
                visit(child)

        for root in document.root_nodes:
            visit(root)
        return list({str(reference.get("reference_id")): reference for reference in references}.values())

    def _overlap_nodes(self, parts: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Uses complete preceding node fragments as bounded neighbour context."""
        selected: list[tuple[str, str]] = []
        size = 0
        for node_id, text in reversed(parts):
            if not text or size + len(text) > self._policy.max_overlap_characters:
                break
            selected.insert(0, (node_id, text))
            size += len(text) + 1
        return selected

    def _split_text(self, text: str, limit: int) -> list[str]:
        """Splits only oversized content, preferring paragraph and sentence boundaries."""
        if len(text) <= limit:
            return [text] if text else []
        units = [unit.strip() for unit in re.split(r"\n\s*\n|(?<=[.!?])\s+", text) if unit.strip()]
        parts: list[str] = []
        current = ""
        for unit in units:
            if len(unit) > limit:
                if current:
                    parts.append(current)
                    current = ""
                parts.extend(self._hard_split(unit, limit))
            elif current and len(current) + len(unit) + 1 > limit:
                parts.append(current)
                current = unit
            else:
                current = f"{current} {unit}".strip()
        if current:
            parts.append(current)
        return parts

    def _hard_split(self, text: str, limit: int) -> list[str]:
        """Uses a whitespace boundary only when one source block exceeds the configured limit."""
        parts: list[str] = []
        remaining = text
        while len(remaining) > limit:
            boundary = remaining.rfind(" ", 0, limit + 1)
            boundary = boundary if boundary > 0 else limit
            parts.append(remaining[:boundary].strip())
            remaining = remaining[boundary:].strip()
        return [*parts, remaining] if remaining else parts
