from __future__ import annotations

import unittest
import hashlib
from pathlib import Path
import tempfile

from new_ingestion.extraction.excel_extractor import (
    ExcelCell, ExcelExtractionResult, ExcelImage, ExcelTable, ExcelWorksheet,
)
from new_ingestion.normalizers.xlsx_normalizer import (
    XlsxNormalizationError, _persist_xlsx_asset, normalize_xlsx,
)
from new_ingestion.schema.schema_define import ElementType


class XlsxNormalizerTests(unittest.TestCase):
    def result(self) -> ExcelExtractionResult:
        cells = [
            ExcelCell("A1", raw_value="Part", data_type="s"),
            ExcelCell("B1", raw_value="Value", data_type="s"),
            ExcelCell("A2", raw_value="Sensor", data_type="s"),
            ExcelCell("B2", raw_value=10, data_type="n", number_format="0"),
            ExcelCell("D1", raw_value=10, formula="=SUM(B2)", cached_value=10,
                      data_type="n", formula_data_type="n"),
            ExcelCell("D2", raw_value="#VALUE!", formula='=IMAGE("https://example.com/a.png")',
                      cached_value="#VALUE!", data_type="e", formula_data_type="e",
                      image_formula={"status": "formula_image_external_unresolved", "external_url": "https://example.com/a.png"}),
        ]
        sheet = ExcelWorksheet("Data", 0, "visible", "A1:D2", 2, 4, cells,
                               ["A1:B1"], [ExcelTable("Parts", "Parts", "A1:B2", ["Part", "Value"], {"name": "TableStyleMedium2"})])
        hidden = ExcelWorksheet("Lookup", 1, "hidden", "A1:A1", 1, 1, [ExcelCell("A1", "x")])
        very_hidden = ExcelWorksheet("Calc", 2, "veryHidden", "A1:A1", 1, 1, [ExcelCell("A1", 1)])
        image = ExcelImage("img-1", "embedded_extracted", "image/png", "a" * 64,
                           "Data", "F2", "G5", [{"start": "F2", "end": "G5"}], ["rId1"],
                           ["openpyxl"], 10, 10, "images/image-" + "a" * 64 + ".png", binary_data=b"image")
        return ExcelExtractionResult(
            {"filename": "vehicle.xlsx"}, {"properties": {"title": "Vehicle", "creator": "Engineering"}},
            [sheet, hidden, very_hidden], [image], ["one extractor warning"], [],
            {"openpyxl_version": "3.1.5"}, Path("vehicle.xlsx"),
        )

    def test_normalizes_workbook_tables_cells_sheets_and_images(self) -> None:
        document = normalize_xlsx(self.result(), document_id="doc-1", version_id="v-1", source_file_hash="A" * 64)
        self.assertEqual(document.document.source_type, "xlsx")
        self.assertEqual(document.document.sha256, "a" * 64)
        self.assertEqual(document.elements["xlsx-sheet-Data"].metadata["visibility"], "visible")
        self.assertEqual(document.elements["xlsx-sheet-Lookup"].metadata["visibility"], "hidden")
        self.assertEqual(document.elements["xlsx-sheet-Calc"].metadata["visibility"], "veryHidden")
        table = document.elements["xlsx-sheet-Data-table-Parts"]
        self.assertEqual(table.type, ElementType.TABLE)
        self.assertEqual(len(table.row_ids), 2)
        self.assertEqual(len(document.assets), 1)
        self.assertEqual(document.elements["xlsx-sheet-Data-image-001"].asset_id, next(iter(document.assets)))
        self.assertEqual(document.elements["xlsx-sheet-Data-cell-D2"].formula, '=IMAGE("https://example.com/a.png")')
        self.assertEqual(document.elements["xlsx-sheet-Data-cell-D2"].metadata["cached_value"], "#VALUE!")

    def test_determinism_and_repeated_rows(self) -> None:
        first = normalize_xlsx(self.result(), document_id="doc-1", version_id="v-1", source_file_hash="b" * 64)
        second = normalize_xlsx(self.result(), document_id="doc-1", version_id="v-1", source_file_hash="b" * 64)
        self.assertEqual(first.model_dump(), second.model_dump())
        self.assertEqual(len(first.elements["xlsx-sheet-Data-table-Parts"].row_ids), 2)

    def test_structure_order_follows_source_cells_and_image_anchor(self) -> None:
        extraction = self.result()
        extraction.worksheets[0].cells.extend([
            ExcelCell("C2", raw_value="before image"),
            ExcelCell("E2", raw_value="after image"),
        ])
        extraction.images[0].anchor_start = "D2"
        extraction.images[0].anchor_end = "E3"
        document = normalize_xlsx(
            extraction, document_id="doc-1", version_id="v-1", source_file_hash="b" * 64,
        )
        order = document.structure.element_order
        positions = {
            element_id: order.index(element_id)
            for element_id in (
                "xlsx-sheet-Data-cell-C2",
                "xlsx-sheet-Data-cell-D2",
                "xlsx-sheet-Data-image-001",
                "xlsx-sheet-Data-cell-E2",
            )
        }
        self.assertLess(positions["xlsx-sheet-Data-cell-C2"], positions["xlsx-sheet-Data-cell-D2"])
        self.assertLess(positions["xlsx-sheet-Data-cell-D2"], positions["xlsx-sheet-Data-image-001"])
        self.assertLess(positions["xlsx-sheet-Data-image-001"], positions["xlsx-sheet-Data-cell-E2"])

    def test_assets_are_persisted_once_and_reused_by_multiple_placements(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory) / "xlsx-artifacts"
            document = normalize_xlsx(
                self.result(), document_id="doc-1", version_id="v-1",
                source_file_hash="b" * 64, artifact_dir=artifact_dir,
            )
            asset = next(iter(document.assets.values()))
            written = artifact_dir / asset.relative_uri
            self.assertTrue(written.is_file())
            self.assertEqual(hashlib.sha256(written.read_bytes()).hexdigest(), asset.sha256)
            self.assertEqual(len(list((artifact_dir / "images").iterdir())), 1)

    def test_asset_persistence_rejects_absolute_and_traversal_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                _persist_xlsx_asset(b"x", str(root / "outside.bin"), root)
            with self.assertRaises(ValueError):
                _persist_xlsx_asset(b"x", "images/../../outside.bin", root)

    def test_unsafe_source_asset_reference_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            extraction = self.result()
            extraction.images[0].asset_reference = "images/../outside.png"
            document = normalize_xlsx(
                extraction, document_id="doc-1", version_id="v-1",
                source_file_hash="b" * 64, artifact_dir=Path(directory),
            )
            asset = next(iter(document.assets.values()))
            self.assertTrue(asset.relative_uri.startswith("images/image-"))
            self.assertTrue((Path(directory) / asset.relative_uri).is_file())

    def test_missing_artifact_dir_keeps_in_memory_behavior(self) -> None:
        document = normalize_xlsx(
            self.result(), document_id="doc-1", version_id="v-1", source_file_hash="b" * 64,
        )
        self.assertEqual(len(document.assets), 1)

    def test_invalid_input_and_duplicate_coordinates(self) -> None:
        with self.assertRaises(TypeError):
            normalize_xlsx(object(), document_id="doc", version_id="v", source_file_hash="a" * 64)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            normalize_xlsx(self.result(), document_id="doc", version_id="v", source_file_hash="bad")
        duplicate = self.result()
        duplicate.worksheets[0].cells.append(ExcelCell("A1", "duplicate"))
        with self.assertRaises(XlsxNormalizationError):
            normalize_xlsx(duplicate, document_id="doc", version_id="v", source_file_hash="a" * 64)


if __name__ == "__main__":
    unittest.main()
