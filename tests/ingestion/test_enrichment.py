"""Tests for UAT-driven field, source-reference, and visual-asset enrichment."""

import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from backend.ingestion.chunking import ChunkingService
from backend.ingestion.enrichment import CanonicalEnrichmentService
from backend.ingestion.enrichment.contracts import FieldPolicy, FieldUse
from backend.ingestion.enrichment.image_description import ImageDescriptionEnrichment
from backend.ingestion.enrichment.image_ocr import ImageOcrEnrichment
from backend.ingestion.enrichment.visual_assets import VisualAssetRegistry
from backend.ingestion.models import CanonicalDocument, CanonicalNode, DocumentFamily, DocumentMetadata, NodeType, ParserInfo, Provenance, SourceType
from backend.ingestion.service import IngestionService


def node(node_id, node_type, *, text=None, children=None, attributes=None, parent=None):
    """Builds minimal canonical source nodes with stable sheet provenance."""
    return CanonicalNode(node_id=node_id, node_type=node_type, text=text, children=children or [], attributes=attributes or {},
        provenance=Provenance(document_id="doc", source_type=SourceType.XLSX, version="v1", parent_node_id=parent, sheet_name="Sheet1"))


def document(children):
    """Builds a source document suitable for enrichment and chunking tests."""
    root = node("root", NodeType.DOCUMENT_ROOT, children=children)
    return CanonicalDocument(document_id="doc", agent_id="adas", source_type=SourceType.XLSX, file_name="input.xlsx",
        source_path="input.xlsx", source_name="input", version="v1", source_hash="sha256:test",
        ingested_at=datetime.now(timezone.utc), parser_info=ParserInfo(parser_name="test", parser_version="v1"),
        metadata=DocumentMetadata(document_family=DocumentFamily.WORKBOOK_BASED), root_nodes=[root])


def typed_document(children, source_type=SourceType.XLSX, family=DocumentFamily.WORKBOOK_BASED):
    """Builds a source document for non-Excel enrichment tests."""
    root = CanonicalNode(node_id="root", node_type=NodeType.DOCUMENT_ROOT, children=children,
        provenance=Provenance(document_id="doc", source_type=source_type, version="v1"))
    return CanonicalDocument(document_id="doc", agent_id="adas", source_type=source_type, file_name="input",
        source_path="input", source_name="input", version="v1", source_hash="sha256:test",
        ingested_at=datetime.now(timezone.utc), parser_info=ParserInfo(parser_name="test", parser_version="v1"),
        metadata=DocumentMetadata(document_family=family), root_nodes=[root])


class CanonicalEnrichmentTests(unittest.TestCase):
    """Checks policy labels and registries without any external link requests."""

    def test_field_policy_labels_standard_internal_and_reference_columns(self):
        """UAT-sensitive backend fields remain source-preserved but answer-hidden."""
        row = node("row", NodeType.TABLE_ROW, parent="table", children=[
            node("name", NodeType.TABLE_CELL, text="Radar", parent="row", attributes={"column_name": "Component", "value": "Radar"}),
            node("owner", NodeType.TABLE_CELL, text="Internal Team", parent="row", attributes={"column_name": "Internal Owner", "value": "Internal Team"}),
            node("link", NodeType.TABLE_CELL, text="https://intranet.jlr.example/spec.pdf", parent="row", attributes={"column_name": "Reference Link", "value": "https://intranet.jlr.example/spec.pdf"}),
        ])
        source = document([node("table", NodeType.TABLE, parent="root", children=[row])])
        CanonicalEnrichmentService().enrich(source)
        self.assertTrue(row.children[0].attributes["field_policy"]["answer_visible"])
        self.assertFalse(row.children[1].attributes["field_policy"]["answer_visible"])
        self.assertFalse(row.children[1].attributes["field_policy"]["retrieval_allowed"])
        self.assertEqual(row.children[2].attributes["field_policy"]["use"], "reference_link")
        self.assertEqual(row.children[2].attributes["references"][0]["status"], "unverified_remote")
        self.assertFalse(row.children[2].attributes["references"][0]["answer_eligible"])

    def test_reference_field_supports_windows_and_unc_paths_without_network_access(self):
        """Document locations are registered while network shares remain deliberately unverified."""
        row = node("row", NodeType.TABLE_ROW, parent="table", children=[
            node("path", NodeType.TABLE_CELL, text=r"\\server\share\design.pptx", parent="row", attributes={"column_name": "PPT Link", "value": r"\\server\share\design.pptx"}),
        ])
        source = document([node("table", NodeType.TABLE, parent="root", children=[row])])
        CanonicalEnrichmentService().enrich(source)
        reference = row.children[0].attributes["references"][0]
        self.assertEqual(reference["reference_type"], "file")
        self.assertEqual(reference["status"], "unverified_network_path")
        self.assertFalse(reference["answer_eligible"])

    def test_chunk_metadata_carries_field_and_reference_policy(self):
        """The future answer layer receives visibility rules and only approved references separately."""
        row = node("row", NodeType.TABLE_ROW, parent="table", children=[
            node("internal", NodeType.TABLE_CELL, text="Private", parent="row", attributes={"column_name": "Internal Note", "value": "Private"}),
            node("link", NodeType.TABLE_CELL, text="https://intranet.jlr.example/spec.pdf", parent="row", attributes={"column_name": "Reference Link", "value": "https://intranet.jlr.example/spec.pdf"}),
        ])
        source = document([node("table", NodeType.TABLE, parent="root", children=[row])])
        CanonicalEnrichmentService().enrich(source)
        chunk = ChunkingService().build(source).chunks[0]
        self.assertFalse(chunk.metadata["field_policies"][0]["answer_visible"])
        self.assertNotIn("Private", chunk.content_text)
        self.assertNotIn("Private", chunk.embedding_text)
        self.assertEqual(len(chunk.metadata["references"]), 1)
        self.assertEqual(chunk.metadata["answer_eligible_references"], [])

    def test_visual_metadata_receives_stable_asset_registry(self):
        """Image metadata is traceable now without claiming image understanding exists."""
        page = node("page", NodeType.PAGE, parent="root", attributes={"images": [{"xref": 17, "width": 640, "height": 480, "extension": "png"}]})
        source = document([page])
        CanonicalEnrichmentService().enrich(source)
        asset = page.attributes["visual_assets"][0]
        self.assertEqual(asset["status"], "metadata_only")
        self.assertEqual(asset["source_node_id"], "page")
        self.assertEqual(source.root_nodes[0].attributes["enrichment"]["visual_asset_count"], 1)

    def test_image_ocr_stores_asset_and_updates_node_metadata(self):
        """PPTX image OCR persists the source image and records OCR text in Master JSON."""
        image = CanonicalNode(node_id="image", node_type=NodeType.IMAGE,
            attributes={"shape_name": "Picture 5", "bbox": {"left": 10, "top": 20, "width": 30, "height": 40}},
            provenance=Provenance(document_id="doc", source_type=SourceType.PPTX, version="v1",
                                  parent_node_id="slide", slide_number=4))
        root = CanonicalNode(node_id="root", node_type=NodeType.DOCUMENT_ROOT, children=[image],
            provenance=Provenance(document_id="doc", source_type=SourceType.PPTX, version="v1"))
        source = CanonicalDocument(document_id="doc", agent_id="eds", source_type=SourceType.PPTX,
            file_name="source.pptx", source_path="source.pptx", source_name="source", version="v1",
            source_hash="sha256:test", ingested_at=datetime.now(timezone.utc),
            parser_info=ParserInfo(parser_name="test", parser_version="v1"),
            metadata=DocumentMetadata(document_family=DocumentFamily.SLIDE_BASED), root_nodes=[root])

        class FakeOcr:
            def __call__(self, image_bytes):
                return type("OcrResult", (), {"txts": ("FIXING CLIP 100-150 MM",), "scores": (0.97,), "boxes": None})()

        provider = lambda _: {
            ("pptx-image", 4, "Picture 5", 10, 20, 30, 40): {
                "blob": b"fake-image-bytes",
                "extension": "png",
                "hash": "abc123def4567890",
            }
        }
        def fake_persist(self, document, node, image):
            node.attributes.update({"saved_path": "storage/visual-assets/test.png", "image_storage_status": "stored"})

        with patch.object(ImageOcrEnrichment, "_persist_image", fake_persist):
            count = ImageOcrEnrichment(ocr_factory=FakeOcr, image_provider=provider).apply(source)
            self.assertEqual(count, 1)
            self.assertEqual(image.attributes["ocr_text"], "FIXING CLIP 100-150 MM")
            self.assertEqual(image.attributes["image_storage_status"], "stored")
            VisualAssetRegistry().apply(source)
            self.assertEqual(image.attributes["visual_assets"][0]["status"], "ocr_success")

    def test_image_description_adds_inferred_azure_metadata_without_overwriting_ocr(self):
        """Vision descriptions are optional inferred metadata, not source text replacements."""
        image = CanonicalNode(node_id="image", node_type=NodeType.IMAGE,
            attributes={"saved_path": "", "shape_name": "Picture 5", "ocr_text": "VISIBLE LABEL",
                        "needs_vision_description": True, "nearby_text": "Radar sensor diagram context."},
            provenance=Provenance(document_id="doc", source_type=SourceType.PPTX, version="v1",
                                  parent_node_id="slide", slide_number=4))

        class FakeVisionProvider:
            deployment = "gpt-4o-vision"

            def describe(self, image_path, *, context):
                self.context = context
                return "A labelled carbon-fiber bonnet diagram with visible callouts."

        image.attributes["saved_path"] = "storage/visual-assets/test.png"
        with patch.object(Path, "is_file", return_value=True):
            with patch.dict("os.environ", {"JLR_IMAGE_DESCRIPTION": "false"}):
                count = ImageDescriptionEnrichment(FakeVisionProvider()).apply(document([image]))
            self.assertEqual(count, 0)

            with patch.dict("os.environ", {"JLR_IMAGE_DESCRIPTION": "true"}):
                provider = FakeVisionProvider()
                count = ImageDescriptionEnrichment(provider).apply(document([image]))

            self.assertEqual(count, 1)
            self.assertEqual(image.attributes["ocr_text"], "VISIBLE LABEL")
            self.assertEqual(image.attributes["image_description_status"], "success")
            self.assertEqual(image.attributes["image_description_source"], "azure_openai_vision")
            self.assertTrue(image.attributes["image_description_is_inferred"])
            self.assertEqual(image.attributes["image_description_model"], "gpt-4o-vision")
            self.assertIn("VISIBLE LABEL", provider.context)

    def test_excel_image_candidate_receives_ocr_description_and_asset_status(self):
        """Excel image-heavy candidates keep OCR text and add optional VLM description metadata."""
        image = CanonicalNode(node_id="excel-image", node_type=NodeType.IMAGE,
            attributes={"saved_path": "storage/visual-assets/excel.png", "ocr_text": "RADAR PAINT AREA",
                        "needs_vision_description": True, "nearby_text": "Row 40: Radar Paint Requirement",
                        "image_storage_status": "stored"},
            provenance=Provenance(document_id="doc", source_type=SourceType.XLSX, version="v1",
                                  parent_node_id="sheet", sheet_name="Radar Sensor"))

        class FakeVisionProvider:
            deployment = "gpt-4o-vision"

            def describe(self, image_path, *, context):
                self.context = context
                return "Diagram shows radar paint requirement zones and visible labels."

        with patch.object(Path, "is_file", return_value=True):
            with patch.dict("os.environ", {"JLR_IMAGE_DESCRIPTION": "true",
                                           "JLR_IMAGE_DESCRIPTION_SCOPE": "vision_candidates"}):
                provider = FakeVisionProvider()
                count = ImageDescriptionEnrichment(provider).apply(document([image]))

        self.assertEqual(count, 1)
        self.assertEqual(image.attributes["ocr_text"], "RADAR PAINT AREA")
        self.assertEqual(image.attributes["image_description_status"], "success")
        self.assertIn("Radar Paint Requirement", provider.context)
        VisualAssetRegistry().apply(document([image]))
        self.assertEqual(image.attributes["visual_assets"][0]["status"], "description_success")

    def test_vlm_transcription_is_used_when_ocr_has_no_text(self):
        """VLM can supply visible image text when OCR fails or finds no text."""
        image = CanonicalNode(node_id="excel-image", node_type=NodeType.IMAGE,
            attributes={"saved_path": "storage/visual-assets/excel.png", "ocr_status": "no_text", "ocr_text": "",
                        "needs_vision_description": True, "nearby_text": "Row 40: Radar Paint Requirement"},
            provenance=Provenance(document_id="doc", source_type=SourceType.XLSX, version="v1",
                                  parent_node_id="sheet", sheet_name="Radar Sensor"))

        class FakeVisionProvider:
            deployment = "gpt-4o-vision"

            def describe(self, image_path, *, context):
                return {"visible_text": "RADAR PAINT REQUIREMENT ZONE",
                        "description": "Diagram shows radar paint area restrictions.",
                        "uncertainty": "", "confidence": "high"}

        with patch.object(Path, "is_file", return_value=True):
            with patch.dict("os.environ", {"JLR_IMAGE_DESCRIPTION": "true",
                                           "JLR_IMAGE_DESCRIPTION_SCOPE": "vision_candidates"}):
                count = ImageDescriptionEnrichment(FakeVisionProvider()).apply(document([image]))

        self.assertEqual(count, 1)
        self.assertEqual(image.attributes["ocr_text"], "")
        self.assertEqual(image.attributes["vlm_transcribed_text"], "RADAR PAINT REQUIREMENT ZONE")
        self.assertEqual(image.attributes["vlm_confidence"], "high")
        chunks = ChunkingService().build(document([image])).chunks
        self.assertEqual(chunks[0].chunk_type.value, "visual")
        self.assertIn("VLM transcribed visible text: RADAR PAINT REQUIREMENT ZONE", chunks[0].content_text)

    def test_required_vlm_candidate_is_blocked_when_description_is_disabled(self):
        """Image-heavy content is preserved but blocked from embedding when required VLM is unavailable."""
        image = CanonicalNode(node_id="excel-image", node_type=NodeType.IMAGE,
            attributes={"saved_path": "storage/visual-assets/excel.png", "ocr_text": "",
                        "needs_vision_description": True, "nearby_text": "Row 40: Radar Paint Requirement"},
            provenance=Provenance(document_id="doc", source_type=SourceType.XLSX, version="v1",
                                  parent_node_id="sheet", sheet_name="Radar Sensor"))
        source = document([image])
        with patch.dict("os.environ", {"JLR_IMAGE_DESCRIPTION": "false"}):
            self.assertEqual(ImageDescriptionEnrichment().apply(source), 0)

        self.assertTrue(image.attributes["requires_review"])
        self.assertFalse(image.attributes["retrieval_allowed"])
        result = ChunkingService().build(source)
        self.assertEqual(result.chunks, [])
        self.assertEqual(result.rejected[0].node_id, "excel-image")

    def test_image_description_loads_dotenv_before_enabled_check(self):
        """The VLM enable flag can come from .env, not only preloaded process env."""
        image = CanonicalNode(node_id="excel-image", node_type=NodeType.IMAGE,
            attributes={"saved_path": "storage/visual-assets/excel.png", "ocr_text": "",
                        "needs_vision_description": True},
            provenance=Provenance(document_id="doc", source_type=SourceType.XLSX, version="v1",
                                  parent_node_id="sheet", sheet_name="Radar Sensor"))

        class FakeVisionProvider:
            deployment = "gpt-4o-vision"

            def describe(self, image_path, *, context):
                return {"visible_text": "VISIBLE LABEL", "description": "Diagram description.",
                        "uncertainty": "", "confidence": "high"}

        with patch.object(Path, "is_file", return_value=True):
            with patch("backend.ingestion.enrichment.image_description.AzureOpenAIVisionDescriptionProvider._load_dotenv_if_available") as load_dotenv:
                with patch.dict("os.environ", {"JLR_IMAGE_DESCRIPTION": "true",
                                               "JLR_IMAGE_DESCRIPTION_SCOPE": "vision_candidates"}, clear=True):
                    count = ImageDescriptionEnrichment(FakeVisionProvider()).apply(document([image]))

        load_dotenv.assert_called_once()
        self.assertEqual(count, 1)
        self.assertEqual(image.attributes["vlm_transcribed_text"], "VISIBLE LABEL")

    def test_pptx_image_only_slide_uses_required_vlm_policy(self):
        """PowerPoint diagram-heavy slides follow the same OCR-failover-to-VLM rule."""
        image = CanonicalNode(node_id="ppt-image", node_type=NodeType.IMAGE,
            attributes={"saved_path": "storage/visual-assets/ppt.png", "ocr_status": "no_text", "ocr_text": ""},
            provenance=Provenance(document_id="doc", source_type=SourceType.PPTX, version="v1",
                                  parent_node_id="slide", slide_number=22))
        slide = CanonicalNode(node_id="slide", node_type=NodeType.SLIDE, title="Slide 22", children=[image],
            provenance=Provenance(document_id="doc", source_type=SourceType.PPTX, version="v1",
                                  parent_node_id="root", slide_number=22))
        source = typed_document([slide], SourceType.PPTX, DocumentFamily.SLIDE_BASED)

        with patch.dict("os.environ", {"JLR_IMAGE_DESCRIPTION": "false"}):
            ImageDescriptionEnrichment().apply(source)

        self.assertTrue(image.attributes["needs_vision_description"])
        self.assertTrue(image.attributes["requires_review"])
        self.assertFalse(image.attributes["retrieval_allowed"])

    def test_pdf_required_vlm_image_can_create_visual_chunk_after_success(self):
        """PDF images with weak local text can become visual chunks after VLM transcription."""
        image = CanonicalNode(node_id="pdf-image", node_type=NodeType.IMAGE,
            attributes={"saved_path": "storage/visual-assets/pdf.png", "ocr_status": "failed", "ocr_text": ""},
            provenance=Provenance(document_id="doc", source_type=SourceType.PDF, version="v1",
                                  parent_node_id="page", page_number=2))
        page = CanonicalNode(node_id="page", node_type=NodeType.PAGE, title="Page 2", children=[image],
            provenance=Provenance(document_id="doc", source_type=SourceType.PDF, version="v1",
                                  parent_node_id="root", page_number=2))
        source = typed_document([page], SourceType.PDF, DocumentFamily.HIERARCHICAL)

        class FakeVisionProvider:
            deployment = "gpt-4o-vision"

            def describe(self, image_path, *, context):
                return {"visible_text": "VOLUME RESISTIVITY 10^12 OHM CM",
                        "description": "Material datasheet figure containing electrical property labels.",
                        "uncertainty": "", "confidence": "high"}

        with patch.object(Path, "is_file", return_value=True):
            with patch.dict("os.environ", {"JLR_IMAGE_DESCRIPTION": "true",
                                           "JLR_IMAGE_DESCRIPTION_SCOPE": "vision_candidates"}):
                ImageDescriptionEnrichment(FakeVisionProvider()).apply(source)

        self.assertEqual(image.attributes["vlm_transcribed_text"], "VOLUME RESISTIVITY 10^12 OHM CM")
        self.assertNotIn("requires_review", image.attributes)
        chunks = ChunkingService().build(source).chunks
        self.assertEqual(chunks[0].chunk_type.value, "visual")
        self.assertIn("VOLUME RESISTIVITY", chunks[0].content_text)

    def test_pdf_image_ocr_persists_asset_before_standard_ocr(self):
        """PDF image nodes get saved_path before OCR/VLM enrichment uses them."""
        image = CanonicalNode(node_id="pdf-image", node_type=NodeType.IMAGE,
            attributes={"xref": 17, "extension": "png"},
            provenance=Provenance(document_id="doc", source_type=SourceType.PDF, version="v1",
                                  parent_node_id="page", page_number=2))
        source = typed_document([image], SourceType.PDF, DocumentFamily.HIERARCHICAL)

        class FakeOcr:
            def __call__(self, image_bytes):
                return type("OcrResult", (), {"txts": ("LOGO TEXT",), "scores": (0.94,), "boxes": None})()

        def fake_persist(self, document, image_nodes):
            image_nodes[0].attributes.update({
                "saved_path": "storage/visual-assets/adas/doc/v1/page_002_image_001_abcd.png",
                "image_storage_status": "stored",
                "image_persistence_method": "pdf_xref",
            })

        with patch.object(ImageOcrEnrichment, "_persist_pdf_images", fake_persist):
            with patch.object(Path, "is_file", return_value=True):
                with patch.object(Path, "read_bytes", return_value=b"fake-pdf-image"):
                    count = ImageOcrEnrichment(ocr_factory=FakeOcr).apply(source)

        self.assertEqual(count, 1)
        self.assertEqual(image.attributes["image_persistence_method"], "pdf_xref")
        self.assertEqual(image.attributes["ocr_text"], "LOGO TEXT")

    def test_explicit_policy_override_wins_over_generic_column_rules(self):
        """Each agent can allow a business-specific column without modifying shared code."""
        cell = node("owner", NodeType.TABLE_CELL, text="Supplier Team", parent="row", attributes={"column_name": "Owner", "value": "Supplier Team"})
        source = document([node("table", NodeType.TABLE, parent="root", children=[node("row", NodeType.TABLE_ROW, parent="table", children=[cell])])])
        override = FieldPolicy(field_name="Owner", use=FieldUse.STANDARD, retrieval_allowed=True, answer_visible=True, reason="Supplier data policy")
        CanonicalEnrichmentService(field_policy_overrides={"Owner": override}).enrich(source)
        self.assertTrue(cell.attributes["field_policy"]["answer_visible"])

    def test_ingestion_service_applies_enrichment_before_returning_canonical_json(self):
        """Normal application ingestion cannot bypass the UAT governance metadata."""
        cell = node("link", NodeType.TABLE_CELL, text="https://intranet.jlr.example/spec.pdf", parent="row", attributes={"column_name": "Reference Link", "value": "https://intranet.jlr.example/spec.pdf"})
        source = document([node("table", NodeType.TABLE, parent="root", children=[node("row", NodeType.TABLE_ROW, parent="table", children=[cell])])])
        parser = Mock()
        parser.parse.return_value = source
        service = IngestionService()
        service._registry = Mock()
        service._registry.resolve.return_value = parser
        result = service.ingest_source(agent_id="adas", source_type=SourceType.XLSX, file_path="input.xlsx")
        self.assertEqual(result.root_nodes[0].attributes["enrichment"]["reference_count"], 1)
        self.assertIn("references", cell.attributes)
