"""Focused tests for Canonical Document Schema 0.2.0 ID references."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from new_ingestion.schema.schema_define import (
    Asset,
    AssetType,
    AutomotiveSemantic,
    BoundingBox,
    CanonicalDocument,
    Diagnostics,
    DocumentMetadata,
    DocumentStructure,
    ElementType,
    ExtractionMetadata,
    ExtractionStatus,
    EnrichmentStatus,
    FigureElement,
    ChartElement,
    DiagramElement,
    HeadingElement,
    ImageElement,
    LayoutInfo,
    NormalizationStatus,
    PageElement,
    ParagraphElement,
    PdfSourceLocator,
    ProvenanceRecord,
    Relationship,
    RelationshipType,
    SCHEMA_VERSION,
    SectionElement,
    SlideElement,
    SourceType,
    TableCellElement,
    TableElement,
    TableRowElement,
    Tolerance,
    OcrEnrichment,
    VlmEnrichment,
    VisualEnrichment,
    WorksheetElement,
    XlsxSourceLocator,
)


class CanonicalSchemaTests(unittest.TestCase):
    def _metadata(self) -> DocumentMetadata:
        return DocumentMetadata(
            document_id="doc-001",
            domain="automotive",
            document_kind="engineering_specification",
            source_file_name="vehicle.pdf",
            source_type=SourceType.PDF,
            mime_type="application/pdf",
            sha256="a" * 64,
            title="Vehicle System Specification",
            author="Engineering",
            language="en",
            revision="A",
            vehicle_program="Program-X",
            vehicle_system="braking",
            subsystem="pressure_sensor",
            extraction=ExtractionMetadata(
                extractor="docling",
                extractor_version="2.132.0",
                extracted_at=datetime.now(timezone.utc),
                status=ExtractionStatus.SUCCESS,
                configuration={"ocr": True},
            ),
        )

    def _document(self) -> CanonicalDocument:
        page = PageElement(
            element_id="page-1",
            child_ids=["heading-1", "paragraph-1", "table-1", "image-1"],
            source_locator=PdfSourceLocator(page=1, native_reference="page:1"),
            layout=LayoutInfo(
                bounding_box=BoundingBox(x=0, y=0, width=800, height=1000),
                coordinate_unit="pt",
            ),
        )
        heading = HeadingElement(
            element_id="heading-1",
            parent_id="page-1",
            level=1,
            content="Vehicle System",
        )
        paragraph = ParagraphElement(
            element_id="paragraph-1",
            parent_id="page-1",
            content="A paragraph",
        )
        nested_paragraph = ParagraphElement(
            element_id="cell-paragraph-1",
            parent_id="cell-1",
            content="nested text",
        )
        cell = TableCellElement(
            element_id="cell-1",
            parent_id="row-1",
            child_ids=["cell-paragraph-1"],
            row_index=0,
            column_index=0,
            row_span=2,
            column_span=1,
            cell_role="header",
            typed_value=12.5,
            display_value="12.5 mm",
            unit="mm",
            tolerance=Tolerance(lower=-0.1, upper=0.1, unit="mm"),
        )
        row = TableRowElement(
            element_id="row-1",
            parent_id="table-1",
            child_ids=["cell-1"],
            cell_ids=["cell-1"],
            row_index=0,
        )
        table = TableElement(
            element_id="table-1",
            parent_id="page-1",
            child_ids=["row-1"],
            row_count=1,
            column_count=1,
            table_kind="measurement",
            header_row_ids=["row-1"],
            row_ids=["row-1"],
        )
        image = ImageElement(
            element_id="image-1",
            parent_id="page-1",
            asset_id="asset-1",
        )
        elements = {
            item.element_id: item
            for item in [page, heading, paragraph, nested_paragraph, cell, row, table, image]
        }
        return CanonicalDocument(
            document=self._metadata(),
            structure=DocumentStructure(
                root_element_ids=["page-1"],
                element_order=list(elements),
            ),
            elements=elements,
            assets={
                "asset-1": Asset(
                    asset_id="asset-1",
                    asset_type=AssetType.IMAGE,
                    mime_type="image/png",
                    sha256="b" * 64,
                    relative_uri="images/image-b.png",
                    file_size=123,
                )
            },
            relationships=[
                Relationship(
                    relationship_id="rel-1",
                    type=RelationshipType.PLACED_ON,
                    source_id="image-1",
                    target_id="page-1",
                    confidence=0.99,
                    provenance=[ProvenanceRecord(source="pptx", evidence="slide placement")],
                ),
                Relationship(
                    relationship_id="rel-2",
                    type=RelationshipType.REFERENCES,
                    source_id="page-1",
                    target_id="asset-1",
                ),
            ],
            diagnostics=Diagnostics(normalization_status=NormalizationStatus.NOT_STARTED),
        )

    def test_canonical_document_creation_and_registries(self) -> None:
        document = self._document()
        self.assertEqual(document.schema_version, "0.2.0")
        self.assertEqual(document.document.document_id, "doc-001")
        self.assertEqual(document.document.vehicle_system, "braking")
        self.assertEqual(document.elements["page-1"].type, ElementType.PAGE)
        self.assertEqual(document.assets["asset-1"].asset_type, AssetType.IMAGE)

    def test_supported_page_slide_worksheet_and_section_elements(self) -> None:
        document = self._document()
        document.elements["slide-1"] = SlideElement(element_id="slide-1")
        document.elements["worksheet-1"] = WorksheetElement(
            element_id="worksheet-1",
            source_locator=XlsxSourceLocator(sheet="Sheet1", cell="A1"),
        )
        document.elements["section-1"] = SectionElement(element_id="section-1")
        self.assertEqual(document.elements["slide-1"].type, ElementType.SLIDE)
        self.assertEqual(document.elements["worksheet-1"].type, ElementType.WORKSHEET)

    def test_id_based_table_nesting_spans_and_nested_elements(self) -> None:
        document = self._document()
        page = document.elements["page-1"]
        table = document.elements["table-1"]
        row = document.elements["row-1"]
        cell = document.elements["cell-1"]
        self.assertEqual(page.child_ids, ["heading-1", "paragraph-1", "table-1", "image-1"])
        self.assertEqual(table.row_ids, ["row-1"])
        self.assertEqual(table.header_row_ids, ["row-1"])
        self.assertEqual(row.cell_ids, ["cell-1"])
        self.assertEqual(cell.child_ids, ["cell-paragraph-1"])
        self.assertEqual(cell.row_span, 2)
        self.assertEqual(cell.column_span, 1)

    def test_automotive_semantics_and_all_source_locators(self) -> None:
        semantic = AutomotiveSemantic(
            domain="automotive",
            role="measurement",
            component_references=["brake-caliper"],
            sensor_references=["sensor-1"],
            connector_references=["connector-A"],
            coordinate_system="vehicle_xyz",
            quantity_type="pressure",
            axis="Z",
            unit="bar",
            confidence=0.8,
            semantic_source="manual_mapping",
        )
        self.assertEqual(semantic.coordinate_system, "vehicle_xyz")
        self.assertEqual(PdfSourceLocator(page=2).source_type, SourceType.PDF)

    def test_json_serialization_is_safe(self) -> None:
        payload = self._document().model_dump(mode="json")
        serialized = json.dumps(payload)
        self.assertIn('"schema_version": "0.2.0"', serialized)
        self.assertEqual(payload["elements"]["table-1"]["row_ids"], ["row-1"])
        self.assertEqual(payload["assets"]["asset-1"]["relative_uri"], "images/image-b.png")

    def test_invalid_values_and_registry_keys_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            PageElement(element_id="bad id")
        with self.assertRaises(ValidationError):
            BoundingBox(x=0, y=0, width=-1, height=1)
        with self.assertRaises(ValidationError):
            AutomotiveSemantic(confidence=1.5)
        with self.assertRaises(ValidationError):
            CanonicalDocument(schema_version="0.1.0", document=self._metadata())
        with self.assertRaises(ValidationError):
            CanonicalDocument(document=self._metadata(), elements={"wrong": PageElement(element_id="right")})
        with self.assertRaises(ValidationError):
            CanonicalDocument(document=self._metadata(), assets={"wrong": Asset(
                asset_id="right", asset_type=AssetType.IMAGE, relative_uri="images/x.png",
            )})

    def test_invalid_parent_child_references_are_rejected(self) -> None:
        valid = self._document()
        broken_child = valid.elements["paragraph-1"].model_copy(update={"parent_id": "missing"})
        elements = dict(valid.elements)
        elements["paragraph-1"] = broken_child
        with self.assertRaises(ValidationError):
            CanonicalDocument(
                document=valid.document,
                structure=valid.structure,
                elements=elements,
                assets=valid.assets,
            )

    def test_invalid_structure_references_are_rejected(self) -> None:
        valid = self._document()
        with self.assertRaises(ValidationError):
            CanonicalDocument(
                document=valid.document,
                structure=DocumentStructure(root_element_ids=["missing"]),
                elements=valid.elements,
                assets=valid.assets,
            )
        with self.assertRaises(ValidationError):
            CanonicalDocument(
                document=valid.document,
                structure=DocumentStructure(element_order=["page-1", "page-1"]),
                elements=valid.elements,
                assets=valid.assets,
            )
        broken_parent = valid.elements["page-1"].model_copy(update={"child_ids": ["missing"]})
        elements = dict(valid.elements)
        elements["page-1"] = broken_parent
        with self.assertRaises(ValidationError):
            CanonicalDocument(
                document=valid.document,
                structure=valid.structure,
                elements=elements,
                assets=valid.assets,
            )

    def test_invalid_table_and_asset_references_are_rejected(self) -> None:
        valid = self._document()
        invalid_table = valid.elements["table-1"].model_copy(update={"row_ids": ["paragraph-1"]})
        elements = dict(valid.elements)
        elements["table-1"] = invalid_table
        with self.assertRaises(ValidationError):
            CanonicalDocument(document=valid.document, structure=valid.structure, elements=elements, assets=valid.assets)
        invalid_image = valid.elements["image-1"].model_copy(update={"asset_id": "missing-asset"})
        elements = dict(valid.elements)
        elements["image-1"] = invalid_image
        with self.assertRaises(ValidationError):
            CanonicalDocument(document=valid.document, structure=valid.structure, elements=elements, assets=valid.assets)

    def test_invalid_relationship_references_and_duplicates_are_rejected(self) -> None:
        valid = self._document()
        with self.assertRaises(ValidationError):
            CanonicalDocument(
                document=valid.document,
                structure=valid.structure,
                elements=valid.elements,
                assets=valid.assets,
                relationships=[Relationship(
                    relationship_id="rel-invalid",
                    type=RelationshipType.CONTAINS,
                    source_id="page-1",
                    target_id="missing",
                )],
            )
        with self.assertRaises(ValidationError):
            CanonicalDocument(
                document=valid.document,
                structure=valid.structure,
                elements=valid.elements,
                assets=valid.assets,
                relationships=[Relationship(
                    relationship_id="rel-self",
                    type=RelationshipType.REFERENCES,
                    source_id="page-1",
                    target_id="page-1",
                )],
            )

    def test_schema_version_constant(self) -> None:
        self.assertEqual(SCHEMA_VERSION, "0.2.0")

    def test_visual_enrichment_models_and_visual_element_attachment(self) -> None:
        enrichment = VisualEnrichment(
            ocr=OcrEnrichment(
                text="  Sensor US1  ", confidence=0.94, engine=" rapidocr ",
                engine_version="1.0", status=EnrichmentStatus.AVAILABLE,
                provenance=[ProvenanceRecord(source="ocr", extractor="rapidocr")],
            ),
            vlm=VlmEnrichment(
                description="  A sensor mounting diagram. ",
                labels=[" sensor ", "mounting", "sensor"], diagram_kind="mounting",
                confidence=0.91, model=" azure-model ", model_version="production",
                status=EnrichmentStatus.VALIDATED,
                provenance=[ProvenanceRecord(source="vlm", extractor="azure-model")],
            ),
        )
        self.assertEqual(enrichment.ocr.text, "Sensor US1")
        self.assertEqual(enrichment.ocr.engine, "rapidocr")
        self.assertEqual(enrichment.vlm.labels, ["sensor", "mounting"])
        self.assertEqual(enrichment.vlm.diagram_kind, "mounting")
        for element in (
            ImageElement(element_id="image-enriched", visual_enrichment=enrichment),
            FigureElement(element_id="figure-enriched", visual_enrichment=enrichment),
            ChartElement(element_id="chart-enriched", visual_enrichment=enrichment),
            DiagramElement(element_id="diagram-enriched", visual_enrichment=enrichment),
        ):
            self.assertIsNotNone(element.visual_enrichment)

    def test_visual_enrichment_validation_and_serialization(self) -> None:
        self.assertEqual(OcrEnrichment().status, EnrichmentStatus.NOT_PROCESSED)
        self.assertEqual(VisualEnrichment().model_dump(), {"ocr": None, "vlm": None})
        with self.assertRaises(ValidationError):
            OcrEnrichment(text="   ")
        with self.assertRaises(ValidationError):
            OcrEnrichment(confidence=1.1)
        with self.assertRaises(ValidationError):
            VlmEnrichment(description="   ")
        with self.assertRaises(ValidationError):
            VlmEnrichment(labels=["ok", "  "])
        with self.assertRaises(ValidationError):
            VlmEnrichment(diagram_kind="unknown")
        with self.assertRaises(ValidationError):
            VlmEnrichment(status="invalid")
        with self.assertRaises(ValidationError):
            OcrEnrichment(provenance=[{"source": ""}])

        element = DiagramElement(
            element_id="diagram-001",
            visual_enrichment=VisualEnrichment(
                ocr=OcrEnrichment(text="US1 x y z", confidence=0.94, engine="rapidocr", status="available"),
                vlm=VlmEnrichment(
                    description="A sensor mounting diagram showing coordinate axes and sensor placement.",
                    labels=["sensor", "coordinate_system", "mounting"], diagram_kind="mounting",
                    confidence=0.91, model="azure-multimodal-model", model_version="production",
                    status="validated",
                ),
            ),
        )
        payload = element.model_dump(mode="json")
        restored = DiagramElement.model_validate(payload)
        self.assertEqual(restored, element)

    def test_non_visual_elements_do_not_receive_visual_enrichment_by_default(self) -> None:
        self.assertNotIn("visual_enrichment", ParagraphElement.model_fields)


if __name__ == "__main__":
    unittest.main()
