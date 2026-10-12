from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from new_ingestion.extraction.excel_extractor import (
    ExcelCell, ExcelExtractionResult, ExcelWorksheet,
)
from new_ingestion.normalizers.ooxml_reconcilation import reconcile_xlsx
from new_ingestion.normalizers.xlsx_normalizer import normalize_xlsx


def _xlsx_fixture(path: Path, *, malformed_drawing: bool = False, orphan_same: bool = False) -> bytes:
    image = b"same-image"
    drawing_xml = (
        "<xdr:wsDr xmlns:xdr=\"http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing\" "
        "xmlns:a=\"http://schemas.openxmlformats.org/drawingml/2006/main\" "
        "xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\">"
        "<xdr:twoCellAnchor><xdr:from><xdr:col>0</xdr:col><xdr:row>0</xdr:row></xdr:from>"
        "<xdr:to><xdr:col>2</xdr:col><xdr:row>4</xdr:row></xdr:to>"
        "<xdr:pic><xdr:nvPicPr><xdr:cNvPr id=\"7\" name=\"Picture 7\"/></xdr:nvPicPr>"
        "<xdr:blipFill><a:blip r:embed=\"rId1\"/></xdr:blipFill></xdr:pic></xdr:twoCellAnchor>"
        "<xdr:twoCellAnchor><xdr:from><xdr:col>4</xdr:col><xdr:row>5</xdr:row></xdr:from>"
        "<xdr:to><xdr:col>6</xdr:col><xdr:row>8</xdr:row></xdr:to>"
        "<xdr:pic><xdr:nvPicPr><xdr:cNvPr id=\"8\" name=\"Picture 8\"/></xdr:nvPicPr>"
        "<xdr:blipFill><a:blip r:embed=\"rId1\"/></xdr:blipFill></xdr:pic></xdr:twoCellAnchor>"
        "</xdr:wsDr>"
    )
    if malformed_drawing:
        drawing_xml = "<broken>"
    sheet_xml = (
        "<worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" "
        "xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\">"
        "<sheetData>"
        "<row r=\"1\"><c r=\"A1\"><f>IMAGE(\"https://example.com/a.png\")</f><v>#VALUE!</v></c>"
        "<c r=\"B1\"><f>1+1</f><v>#VALUE!</v></c><c r=\"C1\"><v>#VALUE!</v></c></row>"
        "</sheetData><drawing r:id=\"rIdDrawing\"/></worksheet>"
    )
    workbook_xml = (
        "<workbook xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" "
        "xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\"><sheets>"
        "<sheet name=\"Visible\" sheetId=\"1\" r:id=\"rId1\"/>"
        "<sheet name=\"Hidden\" sheetId=\"2\" state=\"hidden\" r:id=\"rId2\"/>"
        "<sheet name=\"VeryHidden\" sheetId=\"3\" state=\"veryHidden\" r:id=\"rId3\"/>"
        "</sheets></workbook>"
    )
    content_types = (
        "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">"
        "<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>"
        "<Default Extension=\"xml\" ContentType=\"application/xml\"/>"
        "<Default Extension=\"png\" ContentType=\"image/png\"/>"
        "</Types>"
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\"><Relationship Id=\"rId1\" Target=\"worksheets/sheet1.xml\"/><Relationship Id=\"rId2\" Target=\"worksheets/sheet2.xml\"/><Relationship Id=\"rId3\" Target=\"worksheets/sheet3.xml\"/></Relationships>")
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        archive.writestr("xl/worksheets/sheet2.xml", sheet_xml.replace("<drawing r:id=\"rIdDrawing\"/>", ""))
        archive.writestr("xl/worksheets/sheet3.xml", sheet_xml.replace("<drawing r:id=\"rIdDrawing\"/>", ""))
        archive.writestr("xl/worksheets/_rels/sheet1.xml.rels", "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\"><Relationship Id=\"rIdDrawing\" Target=\"../drawings/drawing1.xml\"/></Relationships>")
        archive.writestr("xl/drawings/drawing1.xml", drawing_xml)
        archive.writestr("xl/drawings/_rels/drawing1.xml.rels", "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\"><Relationship Id=\"rId1\" Target=\"../media/image1.png\"/></Relationships>")
        archive.writestr("xl/media/image1.png", image)
        archive.writestr("xl/media/orphan.png", image if orphan_same else b"orphan-image")
    return image


def _rich_value_fixture(path: Path, *, invalid_vm: bool = False) -> None:
    _xlsx_fixture(path)
    with ZipFile(path, "r") as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    sheet = entries["xl/worksheets/sheet1.xml"].decode("utf-8")
    extra = '<row r="2"><c r="B2" t="e" vm="99"><v>#VALUE!</v></c></row>' if invalid_vm else '<row r="2"><c r="B2" t="e" vm="1"><v>#VALUE!</v></c></row>'
    entries["xl/worksheets/sheet1.xml"] = sheet.replace("</sheetData>", extra + "</sheetData>").encode("utf-8")
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        archive.writestr("xl/metadata.xml", """
            <metadata xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <metadataTypes><metadataType name="XLRICHVALUE"/></metadataTypes>
              <futureMetadata name="XLRICHVALUE"><bk/></futureMetadata>
              <valueMetadata><bk><rc t="1" v="0"/></bk><bk><rc t="1" v="1"/></bk></valueMetadata>
            </metadata>
        """)
        archive.writestr("xl/richData/rdrichvaluestructure.xml", """
            <rvStructures xmlns="http://schemas.microsoft.com/office/spreadsheetml/2017/richdata">
              <s t="_localImage"><k n="CalcOrigin" t="i"/><k n="_rvRel:LocalImageIdentifier" t="i"/></s>
            </rvStructures>
        """)
        archive.writestr("xl/richData/rdrichvalue.xml", """
            <rvData xmlns="http://schemas.microsoft.com/office/spreadsheetml/2017/richdata">
              <rv s="0"><v>0</v><v>0</v></rv><rv s="0"><v>123</v><v>1</v></rv>
            </rvData>
        """)
        archive.writestr("xl/richData/richValueRel.xml", """
            <richValueRels xmlns="http://schemas.microsoft.com/office/spreadsheetml/2022/richvaluerel"
              xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <rel r:id="rId1"/><rel r:id="rId2"/>
            </richValueRels>
        """)
        archive.writestr("xl/richData/_rels/richValueRel.xml.rels", """
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/image1.png"/>
              <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/image2.png"/>
            </Relationships>
        """)
        archive.writestr("xl/media/image2.png", b"rich-image")


class OoxmlReconciliationTests(unittest.TestCase):
    def test_floating_images_formulas_media_inventory_and_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.xlsx"
            image = _xlsx_fixture(path)
            result = reconcile_xlsx(path)
            self.assertEqual(len(result.images), 2)
            self.assertEqual(result.images[0].status, "mapped_floating_image")
            self.assertEqual(result.images[1].status, "duplicate_asset")
            self.assertEqual(result.images[0].worksheet_name, "Visible")
            self.assertEqual(result.images[0].anchor_start, "A1")
            self.assertEqual(result.images[0].anchor_end, "C5")
            self.assertEqual(result.images[0].relationship_ids, ["rIdDrawing", "rId1"])
            self.assertEqual(result.images[0].sha256, hashlib.sha256(image).hexdigest())
            self.assertEqual(len(result.media_inventory), 2)
            self.assertTrue(any("unmapped media" in warning and "orphan.png" in warning for warning in result.warnings))

            formulas = {item.coordinate: item for item in result.formulas if item.worksheet_name == "Visible"}
            self.assertEqual(formulas["A1"].classification, "external_formula_image")
            self.assertEqual(formulas["A1"].status, "image_formula_unresolved")
            self.assertEqual(formulas["A1"].external_url, "https://example.com/a.png")
            self.assertEqual(formulas["B1"].classification, "formula_error")
            self.assertEqual(formulas["C1"].classification, "literal_error_value")

    def test_reconciliation_integrates_without_broken_canonical_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.xlsx"
            _xlsx_fixture(path)
            extraction = ExcelExtractionResult(
                {"filename": "fixture.xlsx"}, {"properties": {}},
                [ExcelWorksheet("Visible", 0, "visible", "A1:C1", 1, 3, [
                    ExcelCell("A1", formula='=IMAGE("https://example.com/a.png")', cached_value="#VALUE!", raw_value="#VALUE!", data_type="e"),
                    ExcelCell("B1", formula="=1+1", cached_value="#VALUE!", raw_value="#VALUE!", data_type="e"),
                    ExcelCell("C1", raw_value="#VALUE!", data_type="e"),
                ]), ExcelWorksheet("Hidden", 1, "hidden", "A1:A1", 1, 1), ExcelWorksheet("VeryHidden", 2, "veryHidden", "A1:A1", 1, 1)],
                [], [], [], {"package_media_inspection": True}, path,
            )
            document = normalize_xlsx(extraction, document_id="doc", version_id="v", source_file_hash="a" * 64)
            self.assertEqual(len(document.assets), 2)
            self.assertEqual(len([item for item in document.elements.values() if item.type == "image"]), 2)
            orphan_asset = next(asset for asset in document.assets.values() if asset.metadata["association_status"] == "unmapped_media")
            self.assertEqual(orphan_asset.metadata["media_path"], "xl/media/orphan.png")
            self.assertTrue(any("unmapped media" in warning for warning in document.diagnostics.warnings))
            self.assertEqual(document.elements["xlsx-sheet-Visible-cell-A1"].metadata["ooxml_reconciliation"]["status"], "image_formula_unresolved")
            self.assertTrue(any("worksheet=Visible, cell=A1" in warning for warning in document.diagnostics.warnings))
            self.assertEqual(document.elements["xlsx-sheet-Visible-cell-B1"].metadata["ooxml_reconciliation"]["classification"], "formula_error")
            self.assertEqual(document.elements["xlsx-sheet-Visible-cell-C1"].metadata["ooxml_reconciliation"]["classification"], "literal_error_value")
            self.assertEqual(document.elements["xlsx-sheet-Hidden"].metadata["visibility"], "hidden")
            self.assertEqual(document.elements["xlsx-sheet-VeryHidden"].metadata["visibility"], "veryHidden")

    def test_malformed_drawing_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "malformed.xlsx"
            _xlsx_fixture(path, malformed_drawing=True)
            result = reconcile_xlsx(path)
            self.assertTrue(any("malformed XML" in warning for warning in result.warnings))
            self.assertEqual(len(result.media_inventory), 2)

    def test_rich_cell_image_uses_vm_structure_and_relation_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rich.xlsx"
            _rich_value_fixture(path)
            result = reconcile_xlsx(path)
            rich_images = [image for image in result.images if image.image_kind == "rich_cell_image"]
            self.assertEqual(len(rich_images), 1)
            image = rich_images[0]
            self.assertEqual(image.source_cell, "B2")
            self.assertEqual(image.status, "mapped_rich_cell_image")
            self.assertEqual(image.media_path, "xl/media/image2.png")
            self.assertEqual(image.relationship_ids, ["rId2"])
            self.assertEqual(image.provenance, ["ooxml_reconciliation", "excel_rich_value", "local_image"])
            self.assertEqual(sum(item.status == "mapped_rich_cell_image" for item in result.media_inventory), 1)
            formula = next(item for item in result.formulas if item.coordinate == "B2")
            self.assertEqual(formula.classification, "mapped_rich_cell_image")
            self.assertEqual(formula.rich_value_index, 1)

            extraction = ExcelExtractionResult(
                {"filename": "rich.xlsx"}, {"properties": {}},
                [ExcelWorksheet("Visible", 0, "visible", "A1:B2", 2, 2,
                    [ExcelCell("B2", raw_value="#VALUE!", cached_value="#VALUE!", data_type="e")])],
                [], [], [], {}, path,
            )
            artifact_dir = Path(directory) / "artifacts"
            document = normalize_xlsx(
                extraction, document_id="doc", version_id="v", source_file_hash="c" * 64,
                artifact_dir=artifact_dir,
            )
            rich_elements = [item for item in document.elements.values() if item.metadata.get("image_kind") == "rich_cell_image"]
            self.assertEqual(len(rich_elements), 1)
            self.assertEqual(rich_elements[0].source_locator.cell, "B2")
            self.assertEqual(len(document.assets), 3)
            self.assertEqual(document.elements["xlsx-sheet-Visible-cell-B2"].metadata["ooxml_reconciliation"]["classification"], "mapped_rich_cell_image")
            rich_asset = next(
                asset for asset in document.assets.values()
                if asset.metadata.get("association_status") == "mapped_rich_cell_image"
            )
            rich_file = artifact_dir / rich_asset.relative_uri
            self.assertTrue(rich_file.is_file())
            self.assertEqual(hashlib.sha256(rich_file.read_bytes()).hexdigest(), rich_asset.sha256)

    def test_invalid_rich_vm_warns_and_does_not_create_placement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid-rich.xlsx"
            _rich_value_fixture(path, invalid_vm=True)
            result = reconcile_xlsx(path)
            self.assertFalse(any(image.image_kind == "rich_cell_image" for image in result.images))
            self.assertTrue(any("invalid" in warning and "vm=99" in warning for warning in result.warnings))

    def test_duplicate_media_files_reuse_one_canonical_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate-media.xlsx"
            _xlsx_fixture(path, orphan_same=True)
            extraction = ExcelExtractionResult(
                {"filename": "duplicate-media.xlsx"}, {"properties": {}},
                [ExcelWorksheet("Visible", 0, "visible", "A1:A1", 1, 1)],
                [], [], [], {}, path,
            )
            document = normalize_xlsx(extraction, document_id="doc", version_id="v", source_file_hash="b" * 64)
            self.assertEqual(len(document.assets), 1)
            self.assertEqual(len([item for item in document.elements.values() if item.type == "image"]), 2)
            asset = next(iter(document.assets.values()))
            self.assertIn("xl/media/image1.png", asset.metadata["media_paths"])
            self.assertIn("xl/media/orphan.png", asset.metadata["media_paths"])


if __name__ == "__main__":
    unittest.main()
