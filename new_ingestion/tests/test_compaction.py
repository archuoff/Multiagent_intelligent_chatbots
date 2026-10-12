from __future__ import annotations

import unittest

from new_ingestion.compaction.compactor import compact_document
from new_ingestion.schema.schema_define import (
    Asset, AssetType, CanonicalDocument, DocumentMetadata, DocumentStructure,
    ElementStatus, ExtractionMetadata, ImageElement, OcrEnrichment, SourceType,
    TableCellElement, TableElement, TableRowElement, VisualEnrichment,
    VlmEnrichment, WorksheetElement, XlsxSourceLocator,
)


def _document_metadata(**overrides) -> DocumentMetadata:
    fields = dict(
        document_id="doc-1", source_file_name="vehicle.xlsx", source_type=SourceType.XLSX,
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        extraction=ExtractionMetadata(extractor="test"),
    )
    fields.update(overrides)
    return DocumentMetadata(**fields)


def _cell(element_id, parent_id, row_index, column_index, *, content=None, typed_value=None,
          display_value=None, formula=None, unit=None) -> TableCellElement:
    return TableCellElement(
        element_id=element_id, parent_id=parent_id, row_index=row_index, column_index=column_index,
        content=content, typed_value=typed_value, display_value=display_value, formula=formula, unit=unit,
        source_locator=XlsxSourceLocator(sheet="Sheet1"),
    )


class FlatSheetCompactionTests(unittest.TestCase):
    """A worksheet with cells attached directly (no named Excel Table)."""

    def document(self) -> CanonicalDocument:
        worksheet_id = "xlsx-sheet-Sheet1"
        banner_id = f"{worksheet_id}-cell-A1"
        header_ids = [f"{worksheet_id}-cell-{col}2" for col in ("A", "B", "C")]
        data_ids = [f"{worksheet_id}-cell-{col}3" for col in ("A", "B", "C")]
        image_id = f"{worksheet_id}-image-001"

        elements = {
            worksheet_id: WorksheetElement(
                element_id=worksheet_id, child_ids=[banner_id, *header_ids, *data_ids, image_id],
                source_locator=XlsxSourceLocator(sheet="Sheet1"),
            ),
            banner_id: _cell(banner_id, worksheet_id, row_index=0, column_index=0,
                              content="Sensor Catalog", typed_value="Sensor Catalog"),
            header_ids[0]: _cell(header_ids[0], worksheet_id, 1, 0, content="Part", typed_value="Part"),
            header_ids[1]: _cell(header_ids[1], worksheet_id, 1, 1, content="Value", typed_value="Value"),
            header_ids[2]: _cell(header_ids[2], worksheet_id, 1, 2, content="Note", typed_value="Note"),
            data_ids[0]: _cell(data_ids[0], worksheet_id, 2, 0, content="Sensor A", typed_value="Sensor A"),
            data_ids[1]: _cell(data_ids[1], worksheet_id, 2, 1, typed_value=20, formula="=B2*2", unit="mm"),
            data_ids[2]: _cell(data_ids[2], worksheet_id, 2, 2, content="Spec A", typed_value="Spec A"),
            image_id: ImageElement(
                element_id=image_id, parent_id=worksheet_id, asset_id="asset-1",
                source_locator=XlsxSourceLocator(sheet="Sheet1", cell="A3"),
                visual_enrichment=VisualEnrichment(
                    ocr=OcrEnrichment(text="OCR TEXT"),
                    vlm=VlmEnrichment(description="A photo of a sensor"),
                ),
            ),
        }
        for element_id in (banner_id, *header_ids, *data_ids):
            elements[element_id] = elements[element_id].model_copy(update={"parent_id": worksheet_id})

        return CanonicalDocument(
            document=_document_metadata(),
            structure=DocumentStructure(
                root_element_ids=[worksheet_id],
                element_order=[worksheet_id, banner_id, *header_ids, *data_ids, image_id],
            ),
            elements=elements,
            assets={"asset-1": Asset(asset_id="asset-1", asset_type=AssetType.IMAGE, mime_type="image/png",
                                      relative_uri="images/asset-1.png", sha256="a" * 64,
                                      metadata={"drawing_xml_path": "xl/drawings/drawing1.xml"})},
        )

    def test_banner_row_becomes_its_own_title_unit(self) -> None:
        compact = compact_document(self.document())
        titles = [unit for unit in compact["content"] if unit["metadata"].get("role") == "title"]
        self.assertEqual(len(titles), 1)
        self.assertEqual(titles[0]["embedding_text"], "Sensor Catalog")

    def test_data_row_merges_cells_using_header_labels(self) -> None:
        compact = compact_document(self.document())
        rows = [unit for unit in compact["content"] if unit["id"].startswith("row-")]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["embedding_text"], "Part: Sensor A | Value: 20 | Note: Spec A")
        self.assertEqual(row["metadata"]["values"]["Value"], {"value": 20, "formula": "=B2*2", "unit": "mm"})
        self.assertEqual(row["metadata"]["values"]["Part"], "Sensor A")

    def test_image_links_to_its_row_and_gets_its_own_enrichment_unit(self) -> None:
        compact = compact_document(self.document())
        rows = [unit for unit in compact["content"] if unit["id"].startswith("row-")]
        self.assertIn("asset-1", rows[0]["images"])

        image_units = [unit for unit in compact["content"] if unit["id"].endswith("-image-001")]
        self.assertEqual(len(image_units), 1)
        self.assertEqual(image_units[0]["embedding_text"], "OCR text: OCR TEXT. Description: A photo of a sensor")

    def test_assets_section_drops_ooxml_internals_but_keeps_locator_fields(self) -> None:
        compact = compact_document(self.document())
        asset = compact["assets"]["asset-1"]
        self.assertEqual(asset, {"type": "image", "mime_type": "image/png",
                                  "path": "images/asset-1.png", "sha256": "a" * 64})


class TableElementCompactionTests(unittest.TestCase):
    """A worksheet whose cells are wrapped in TableElement/TableRowElement (named Excel Table)."""

    def document(self) -> CanonicalDocument:
        worksheet_id = "xlsx-sheet-Sheet1"
        table_id = f"{worksheet_id}-table-Parts"
        header_row_id, data_row_id = f"{table_id}-row-0", f"{table_id}-row-1"
        header_cells = [f"{header_row_id}-cell-0", f"{header_row_id}-cell-1"]
        data_cells = [f"{data_row_id}-cell-0", f"{data_row_id}-cell-1"]

        elements = {
            worksheet_id: WorksheetElement(element_id=worksheet_id, child_ids=[table_id],
                                            source_locator=XlsxSourceLocator(sheet="Sheet1")),
            table_id: TableElement(element_id=table_id, parent_id=worksheet_id,
                                    child_ids=[header_row_id, data_row_id],
                                    row_ids=[header_row_id, data_row_id], header_row_ids=[header_row_id]),
            header_row_id: TableRowElement(element_id=header_row_id, parent_id=table_id, row_index=0,
                                            child_ids=header_cells, cell_ids=header_cells),
            data_row_id: TableRowElement(element_id=data_row_id, parent_id=table_id, row_index=1,
                                          child_ids=data_cells, cell_ids=data_cells),
            header_cells[0]: _cell(header_cells[0], header_row_id, 0, 0, content="Part", typed_value="Part"),
            header_cells[1]: _cell(header_cells[1], header_row_id, 0, 1, content="Value", typed_value="Value"),
            data_cells[0]: _cell(data_cells[0], data_row_id, 1, 0, content="Sensor B", typed_value="Sensor B"),
            data_cells[1]: _cell(data_cells[1], data_row_id, 1, 1, typed_value=5),
        }
        return CanonicalDocument(
            document=_document_metadata(),
            structure=DocumentStructure(
                root_element_ids=[worksheet_id],
                element_order=[worksheet_id, table_id, header_row_id, data_row_id, *header_cells, *data_cells],
            ),
            elements=elements,
        )

    def test_named_table_rows_are_merged_using_header_row(self) -> None:
        compact = compact_document(self.document())
        rows = [unit for unit in compact["content"] if unit["id"].startswith("row-")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["embedding_text"], "Part: Sensor B | Value: 5")


if __name__ == "__main__":
    unittest.main()
