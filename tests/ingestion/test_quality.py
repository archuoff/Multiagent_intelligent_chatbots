"""Unit tests for deterministic canonical ingestion quality checks."""

from datetime import datetime, timezone
import unittest

from backend.ingestion.models import CanonicalDocument, CanonicalNode, DocumentFamily, DocumentMetadata, NodeType, ParserInfo, Provenance, SourceType
from backend.ingestion.validation.quality import CanonicalQualityValidator


def make_document(children: list[CanonicalNode]) -> CanonicalDocument:
    """Builds a minimal canonical document for validator tests."""
    return CanonicalDocument(
        document_id="doc-1", agent_id="agent", source_type=SourceType.PDF, file_name="test.pdf", source_path="test.pdf", source_name="test", version="v1", ingested_at=datetime.now(timezone.utc), source_hash="hash",
        parser_info=ParserInfo(parser_name="test", parser_version="1"), metadata=DocumentMetadata(document_family=DocumentFamily.PAGE_CENTRIC),
        root_nodes=[CanonicalNode(node_id="root", node_type=NodeType.DOCUMENT_ROOT, provenance=Provenance(document_id="doc-1", source_type=SourceType.PDF, version="v1"), children=children)],
    )


class CanonicalQualityValidatorTests(unittest.TestCase):
    """Covers quality rules without requiring Docling or external files."""

    def test_reports_duplicate_node_ids(self) -> None:
        """Ensures duplicate IDs are blocking quality errors."""
        nodes = [CanonicalNode(node_id="same", node_type=NodeType.PARAGRAPH, text="One", provenance=Provenance(document_id="doc-1", source_type=SourceType.PDF, version="v1")), CanonicalNode(node_id="same", node_type=NodeType.PARAGRAPH, text="Two", provenance=Provenance(document_id="doc-1", source_type=SourceType.PDF, version="v1"))]
        report = CanonicalQualityValidator().validate(make_document(nodes))
        self.assertFalse(report.is_valid)
        self.assertTrue(any(issue.code == "duplicate_node_id" for issue in report.issues))

    def test_reports_empty_page_and_duplicate_text(self) -> None:
        """Ensures retrieval-risk warnings are emitted for weak page content."""
        provenance = Provenance(document_id="doc-1", source_type=SourceType.PDF, version="v1")
        nodes = [CanonicalNode(node_id="page", node_type=NodeType.PAGE, provenance=provenance), CanonicalNode(node_id="one", node_type=NodeType.PARAGRAPH, text="Repeated", provenance=provenance), CanonicalNode(node_id="two", node_type=NodeType.PARAGRAPH, text="Repeated", provenance=provenance)]
        report = CanonicalQualityValidator().validate(make_document(nodes))
        codes = {issue.code for issue in report.issues}
        self.assertIn("empty_container", codes)
        self.assertIn("duplicate_content", codes)
