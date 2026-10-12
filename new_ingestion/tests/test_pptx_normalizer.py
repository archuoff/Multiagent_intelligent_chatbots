from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from new_ingestion.extraction.pptx_extractor import (
    PptxExtractionResult, PptxImage, PptxParagraph, PptxShape, PptxSlide,
    PptxTable, PptxTableCell, PptxTextFrame,
)
from new_ingestion.normalizers.pptx_normalizer import PptxNormalizationError, normalize_pptx
from new_ingestion.schema.schema_define import ElementType


class PptxNormalizerTests(unittest.TestCase):
    def result(self) -> PptxExtractionResult:
        shape = PptxShape(
            shape_id=5, name="Title", shape_type="PLACEHOLDER", order_index=0, z_order=0,
            left=0, top=0, width=100, height=50, rotation=0,
            placeholder={"is_placeholder": True, "type": "TITLE"},
            text_frame=PptxTextFrame("Brake system", [PptxParagraph("Brake system", 0)]),
        )
        table_shape = PptxShape(
            shape_id=6, name="Table", shape_type="TABLE", order_index=1, z_order=1,
            left=10, top=60, width=200, height=100, rotation=0,
            table=PptxTable(2, 2, 10, 60, 200, 100, [
                PptxTableCell(0, 0, "Part"), PptxTableCell(0, 1, "Value"),
                PptxTableCell(1, 0, "Sensor"), PptxTableCell(1, 1, "10"),
            ]),
        )
        image = PptxImage("sha256:" + "a" * 64, 1, 7, "a.png", "image/png", 20, 20, 20, 180,
                          "a" * 64, "images/image-" + "a" * 64 + ".png", binary_data=b"image")
        image.placements = [{"slide_number": 1, "shape_id": 7}]
        picture_shape = PptxShape(7, "Picture", "PICTURE", 2, 2, 20, 180, 20, 20, 0)
        return PptxExtractionResult(
            source_metadata={"filename": "vehicle.pptx"},
            presentation_metadata={"slide_count": 1, "slide_width": 1000, "slide_height": 800,
                                   "coordinate_unit": "EMU", "properties": {"title": "Vehicle guide", "author": "Engineering"}},
            slides=[PptxSlide(1, 0, "Title Slide", None, [shape, table_shape, picture_shape], "Notes")],
            images=[image], warnings=["chart not extracted"], errors=[],
            extraction_configuration={"python_pptx_version": "1.0.2"}, source_path=Path("vehicle.pptx"),
        )

    def test_normalizes_hierarchy_tables_text_and_assets(self) -> None:
        document = normalize_pptx(self.result(), document_id="doc-1", version_id="v-1", source_file_hash="A" * 64)
        self.assertEqual(document.document.source_type, "pptx")
        self.assertEqual(document.document.sha256, "a" * 64)
        self.assertEqual(document.document.title, "Vehicle guide")
        self.assertEqual(document.elements["pptx-slide-001"].child_ids,
                         ["pptx-slide-001-heading-001", "pptx-slide-001-table-001", "pptx-slide-001-image-001"])
        table = document.elements["pptx-slide-001-table-001"]
        self.assertEqual(table.type, ElementType.TABLE)
        self.assertEqual(len(table.row_ids), 2)
        self.assertEqual(len(document.assets), 1)
        self.assertEqual(len(document.elements["pptx-slide-001-heading-001"].child_ids), 1)
        self.assertTrue(all(len(item.content_hash or "") == 64 for item in document.elements.values()))

    def test_repeated_normalization_is_deterministic(self) -> None:
        first = normalize_pptx(self.result(), document_id="doc-1", version_id="v-1", source_file_hash="b" * 64)
        second = normalize_pptx(self.result(), document_id="doc-1", version_id="v-1", source_file_hash="b" * 64)
        self.assertEqual(first.model_dump(), second.model_dump())

    def test_invalid_inputs_and_empty_content(self) -> None:
        with self.assertRaises(TypeError):
            normalize_pptx(object(), document_id="doc", version_id="v", source_file_hash="a" * 64)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            normalize_pptx(self.result(), document_id="doc", version_id="v", source_file_hash="bad")
        empty = self.result(); empty.slides = None  # type: ignore[assignment]
        with self.assertRaises(PptxNormalizationError):
            normalize_pptx(empty, document_id="doc", version_id="v", source_file_hash="a" * 64)


if __name__ == "__main__":
    unittest.main()
