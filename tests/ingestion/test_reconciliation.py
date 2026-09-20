"""Tests for deterministic reconciliation of Docling and local recovery output."""

import unittest
from datetime import datetime, timezone

from backend.ingestion.extraction.contracts import ExtractedPage, ExtractionResult
from backend.ingestion.extraction.docling import DoclingContentAdapter
from backend.ingestion.extraction.merger import ExtractionResultMerger
from backend.ingestion.extraction.reconciliation import ExtractionReconciler
from backend.ingestion.models import CanonicalDocument, CanonicalNode, DocumentFamily, DocumentMetadata, NodeType, ParserInfo, Provenance, SourceType
from backend.ingestion.validation.quality import CanonicalQualityValidator


class ReconciliationTests(unittest.TestCase):
    """Exercises only deterministic source-content decisions, not LLM judgments."""

    def setUp(self):
        """Creates reusable rule and merge collaborators."""
        self.reconciler = ExtractionReconciler()
        self.merger = ExtractionResultMerger(self.reconciler)

    def test_identical_text_is_suppressed(self):
        """Whitespace and case differences cannot create a second block."""
        decision = self.reconciler.compare_text("Maximum operating temperature: 90 C", " maximum  operating TEMPERATURE: 90 c ")
        self.assertEqual(decision["decision"], "duplicate")

    def test_near_duplicate_with_same_technical_value_is_suppressed(self):
        """Formatting changes retain Docling when all technical values agree."""
        decision = self.reconciler.compare_text("The maximum operating temperature is 90 C for the sensor.", "Maximum operating temperature 90 C sensor")
        self.assertEqual(decision["decision"], "near_duplicate")

    def test_different_technical_values_are_a_conflict(self):
        """Similar prose with different values must remain reviewable evidence."""
        decision = self.reconciler.compare_text("Maximum operating temperature is 90 C for this sensor.", "Maximum operating temperature is 80 C for this sensor.")
        self.assertEqual(decision["decision"], "conflict")
        self.assertIn("90", decision["primary_technical_values"])
        self.assertIn("80", decision["fallback_technical_values"])

    def test_subset_technical_values_are_not_a_conflict(self):
        """A shorter extraction with matching values is incomplete, not contradictory."""
        decision = self.reconciler.compare_text("History includes 1879, 1958, 1963 and 2020 milestones.", "History includes 1879 and 1958 milestones.")
        self.assertEqual(decision["decision"], "near_duplicate")

    def test_new_text_is_added_as_recovery_evidence(self):
        """A meaningful fallback-only block is retained with its decision metadata."""
        primary = ExtractionResult("docling", "primary", pages=[ExtractedPage(1, text="Sensor mounting guidance.", text_blocks=[{"text": "Sensor mounting guidance."}])])
        fallback = ExtractionResult("pymupdf", "fallback", pages=[ExtractedPage(1, text_blocks=[{"text": "Use M6 bolts with a 12 Nm tightening torque."}])])
        merged = self.merger.merge(primary, fallback)
        self.assertEqual(len(merged.pages[0].text_blocks), 2)
        decisions = [event["decision"] for event in merged.pages[0].reconciliation]
        self.assertIn("selected_fallback", decisions)
        self.assertIn("fallback_only", decisions)
        self.assertEqual(merged.extraction_mode, "quality_reconciled")

    def test_conflicting_text_is_retained_and_blocks_review(self):
        """Conflicting recovery text is kept, but the result is not ready for chunking."""
        primary = ExtractionResult("docling", "primary", pages=[ExtractedPage(1, text="Maximum operating temperature is 90 C for this sensor.")])
        fallback = ExtractionResult("pymupdf", "fallback", pages=[ExtractedPage(1, text="Maximum operating temperature is 80 C for this sensor.")])
        merged = self.merger.merge(primary, fallback)
        self.assertTrue(merged.requires_review)
        self.assertEqual(len(merged.pages[0].text_blocks), 1)
        self.assertIn("conflict", [event["decision"] for event in merged.pages[0].reconciliation])

    def test_recovery_block_does_not_overwrite_primary_page_text(self):
        """A fallback block cannot replace stronger primary text when primary has no blocks."""
        primary = ExtractionResult("docling", "primary", pages=[ExtractedPage(1, text="A B C D E")])
        fallback = ExtractionResult("pymupdf", "fallback", pages=[ExtractedPage(1, text_blocks=[{"text": "C"}])])
        merged = self.merger.merge(primary, fallback)
        self.assertIn("A B C D E", merged.pages[0].text)
        self.assertNotEqual(merged.pages[0].text, "C")

    def test_fallback_can_replace_weaker_primary_page(self):
        """Quality wins over extractor name when fallback has materially stronger text."""
        primary = ExtractionResult("docling", "primary", pages=[ExtractedPage(1, text="Sensor overview.")])
        fallback = ExtractionResult("pymupdf", "fallback", pages=[ExtractedPage(1, text="Sensor overview. Mounting torque is 12 Nm. Operating voltage is 12 V.")])
        merged = self.merger.merge(primary, fallback)
        self.assertTrue(merged.pages[0].text.startswith("Sensor overview. Mounting torque"))
        self.assertFalse(merged.requires_review)
        self.assertEqual(merged.processing_details["quality_recovery"]["selected_fallback_pages"], 1)

    def test_same_cell_with_different_value_is_a_table_conflict(self):
        """Coordinates make table conflicts deterministic without embedding similarity."""
        decision = self.reconciler.compare_tables([["Material", "Temperature"], ["ASA", "90"]], [["Material", "Temperature"], ["ASA", "80"]])
        self.assertEqual(decision["decision"], "conflict")
        self.assertEqual(decision["conflicting_coordinates"], [[1, 1]])

    def test_matching_tables_are_not_appended_twice(self):
        """Equivalent simple table cells remain represented once."""
        primary = ExtractionResult("docling", "primary", pages=[ExtractedPage(1, tables=[[['Material', 'Temperature'], ['ASA', '90']]])])
        fallback = ExtractionResult("pdfplumber", "fallback", pages=[ExtractedPage(1, tables=[[['Material', 'Temperature'], ['ASA', '90']]])])
        merged = self.merger.merge(primary, fallback)
        self.assertEqual(len(merged.pages[0].tables), 1)
        self.assertIn("duplicate", [event["decision"] for event in merged.pages[0].reconciliation])

    def test_same_image_bbox_is_not_appended_twice(self):
        """Equivalent image boxes from Docling and python-pptx remain one image node."""
        primary = ExtractionResult("docling", "primary", pages=[ExtractedPage(1, images=[
            {"bbox": {"left": 3522900.0, "top": 5305667.0, "right": 8869183.0, "bottom": 798467.0}}
        ])])
        fallback = ExtractionResult("python-pptx", "fallback", pages=[ExtractedPage(1, images=[
            {"shape_name": "Graphic 10", "bbox": {"left": 3522900, "top": 798467, "width": 5346283, "height": 4507200}}
        ])])
        merged = self.merger.merge(primary, fallback)
        self.assertEqual(len(merged.pages[0].images), 1)
        self.assertEqual(merged.pages[0].images[0]["bbox"], {"left": 3522900.0, "top": 798467.0, "width": 5346283.0, "height": 4507200.0})
        self.assertIn("duplicate", [event["decision"] for event in merged.pages[0].reconciliation])

    def test_quality_validator_reports_canonical_conflict(self):
        """Canonical validation converts retained conflict evidence into a blocking issue."""
        root = CanonicalNode(node_id="root", node_type=NodeType.DOCUMENT_ROOT, provenance=Provenance(document_id="doc", source_type=SourceType.PDF, version="v1"))
        page = CanonicalNode(node_id="page", node_type=NodeType.PAGE, provenance=Provenance(document_id="doc", source_type=SourceType.PDF, version="v1", parent_node_id="root", page_number=1), attributes={"reconciliation": [{"decision": "conflict"}]})
        root.children.append(page)
        document = CanonicalDocument(document_id="doc", agent_id="agent", source_type=SourceType.PDF, file_name="test.pdf", source_path="test.pdf", source_name="test", version="v1", ingested_at=datetime.now(timezone.utc), source_hash="sha256:test", parser_info=ParserInfo(parser_name="test", parser_version="v1"), metadata=DocumentMetadata(document_family=DocumentFamily.PAGE_CENTRIC), root_nodes=[root])
        issues = CanonicalQualityValidator().validate(document).issues
        self.assertTrue(any(issue.code == "extraction_conflict" and issue.severity == "error" for issue in issues))


class DoclingPictureMetadataTests(unittest.TestCase):
    """Checks that Docling vision metadata survives into our raw image contract."""

    def test_picture_description_is_read_from_meta(self):
        """Docling picture descriptions are stored separately from captions."""
        description = type("Description", (), {"text": "Diagram showing fixing clip spacing of 100-150 mm."})()
        meta = type("Meta", (), {"description": description})()
        bbox = type("BBox", (), {"l": 10, "t": 90, "r": 50, "b": 20})()
        provenance = type("Prov", (), {"bbox": bbox})()
        picture = type("Picture", (), {"caption": None, "classification": "diagram", "meta": meta, "annotations": [], "prov": [provenance]})()
        metadata = DoclingContentAdapter()._picture_metadata(picture)
        self.assertEqual(metadata["description"], "Diagram showing fixing clip spacing of 100-150 mm.")
        self.assertEqual(metadata["bbox"], {"left": 10, "top": 20, "width": 40, "height": 70})
