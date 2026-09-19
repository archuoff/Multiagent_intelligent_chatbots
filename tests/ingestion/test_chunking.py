"""Tests for structure-aware, provider-neutral canonical chunking."""

import unittest
from datetime import datetime, timezone

from backend.ingestion.chunking import ChunkType, ChunkingPolicy, ChunkingService
from backend.ingestion.models import CanonicalDocument, CanonicalNode, DocumentFamily, DocumentMetadata, NodeType, ParserInfo, Provenance, QualityMetadata, SourceType


def node(node_id, node_type, *, text=None, title=None, children=None, attributes=None, parent=None, page=1):
    """Creates concise canonical nodes with traceable test provenance."""
    return CanonicalNode(node_id=node_id, node_type=node_type, text=text, title=title, children=children or [],
        attributes=attributes or {}, provenance=Provenance(document_id="doc", source_type=SourceType.PDF,
            version="v1", parent_node_id=parent, page_number=page))


def document(children, *, errors=None):
    """Creates one canonical document with an internal ACL for chunk tests."""
    root = node("root", NodeType.DOCUMENT_ROOT, title="Requirements", children=children, parent=None)
    return CanonicalDocument(document_id="doc", agent_id="adas", source_type=SourceType.PDF,
        file_name="requirements.pdf", source_path="requirements.pdf", source_name="requirements",
        version="v1", ingested_at=datetime.now(timezone.utc), source_hash="sha256:test",
        parser_info=ParserInfo(parser_name="test", parser_version="v1"),
        metadata=DocumentMetadata(title="ADAS Requirements", document_family=DocumentFamily.PAGE_CENTRIC),
        quality=QualityMetadata(errors=errors or []), root_nodes=[root])


class CanonicalChunkingTests(unittest.TestCase):
    """Covers narrative, table, quality, parent-link, and deterministic-ID behavior."""

    def test_groups_related_narrative_and_carries_parent_context(self):
        """Paragraphs under one section become one retrieval chunk with breadcrumbs."""
        section = node("section", NodeType.SECTION, title="PDC Sensor", parent="root", children=[
            node("p1", NodeType.PARAGRAPH, text="The sensor shall detect obstacles within 2 metres.", parent="section"),
            node("p2", NodeType.PARAGRAPH, text="The mounting bracket shall resist vibration.", parent="section"),
        ])
        result = ChunkingService().build(document([section]))
        self.assertEqual(len(result.chunks), 1)
        chunk = result.chunks[0]
        self.assertEqual(chunk.chunk_type, ChunkType.NARRATIVE)
        self.assertEqual(chunk.parent_node_id, "section")
        self.assertEqual(chunk.source_node_ids, ["p1", "p2"])
        self.assertIn("PDC Sensor", chunk.embedding_text)
        self.assertEqual(chunk.metadata["security_scope"]["classification"], "internal")

    def test_neighbour_overlap_uses_complete_prior_node(self):
        """A split retains bounded prior-node context rather than arbitrary repeated words."""
        section = node("section", NodeType.SECTION, title="Limits", parent="root", children=[
            node("p1", NodeType.PARAGRAPH, text="First requirement remains important.", parent="section"),
            node("p2", NodeType.PARAGRAPH, text="Second requirement needs its own retrieval chunk.", parent="section"),
        ])
        result = ChunkingService(ChunkingPolicy(max_chunk_characters=150, max_overlap_characters=80)).build(document([section]))
        self.assertEqual(len(result.chunks), 2)
        self.assertEqual(result.chunks[1].metadata["overlap_node_ids"], ["p1"])
        self.assertIn("First requirement", result.chunks[1].content_text)

    def test_sections_are_not_merged_into_one_chunk(self):
        """Canonical section boundaries always remain parent-document boundaries."""
        first = node("s1", NodeType.SECTION, title="Radar", parent="root", children=[node("r1", NodeType.PARAGRAPH, text="Radar requirement.", parent="s1")])
        second = node("s2", NodeType.SECTION, title="Camera", parent="root", children=[node("c1", NodeType.PARAGRAPH, text="Camera requirement.", parent="s2")])
        chunks = ChunkingService().build(document([first, second])).chunks
        self.assertEqual([chunk.parent_node_id for chunk in chunks], ["s1", "s2"])

    def test_table_rows_keep_headers_and_parent_table_link(self):
        """Structured table rows are searchable without flattening their header relationship."""
        header = node("header", NodeType.TABLE_HEADER, parent="table", attributes={"header_rows": [["Material", "Temperature"]]})
        row = node("row", NodeType.TABLE_ROW, parent="table", children=[
            node("cell1", NodeType.TABLE_CELL, text="ASA", parent="row"),
            node("cell2", NodeType.TABLE_CELL, text="90 C", parent="row"),
        ], attributes={"row_index": 2})
        table = node("table", NodeType.TABLE, title="Material limits", parent="root", children=[header, row])
        chunk = ChunkingService().build(document([table])).chunks[0]
        self.assertEqual(chunk.chunk_type, ChunkType.TABLE_ROW)
        self.assertEqual(chunk.parent_node_id, "table")
        self.assertIn("Material: ASA", chunk.content_text)
        self.assertIn("Temperature: 90 C", chunk.content_text)
        self.assertEqual(chunk.metadata["header_context"], ["Material | Temperature"])

    def test_document_quality_error_blocks_automatic_chunking(self):
        """Incomplete extraction never reaches automatic embedding in strict mode."""
        paragraph = node("p1", NodeType.PARAGRAPH, text="Do not embed this.", parent="root")
        result = ChunkingService().build(document([paragraph], errors=["[incomplete_extraction] review required"]))
        self.assertEqual(result.chunks, [])
        self.assertEqual(result.rejected[0].severity, "error")

    def test_local_conflict_excludes_only_affected_branch_when_not_globally_blocked(self):
        """A conflict-marked page does not contaminate unrelated canonical content."""
        safe = node("safe", NodeType.PARAGRAPH, text="Approved source content.", parent="root")
        conflicting = node("conflict", NodeType.PARAGRAPH, text="Conflicting source content.", parent="root",
            attributes={"reconciliation": [{"decision": "conflict"}]})
        result = ChunkingService().build(document([safe, conflicting]))
        self.assertEqual(len(result.chunks), 1)
        self.assertIn("Approved", result.chunks[0].content_text)

    def test_chunk_ids_are_deterministic_for_the_same_document_version(self):
        """Reprocessing unchanged canonical JSON produces index-safe stable IDs."""
        paragraph = node("p1", NodeType.PARAGRAPH, text="Stable ID requirement.", parent="root")
        source = document([paragraph])
        first = ChunkingService().build(source).chunks[0]
        second = ChunkingService().build(source).chunks[0]
        self.assertEqual(first.chunk_id, second.chunk_id)
