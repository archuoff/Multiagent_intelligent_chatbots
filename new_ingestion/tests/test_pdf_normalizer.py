from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from new_ingestion.extraction.pdf_extractor import PdfExtractionResult
from new_ingestion.normalizers.pdf_normalizer import PdfNormalizationError, normalize_pdf
from new_ingestion.schema.schema_define import ElementType


class PdfNormalizerTests(unittest.TestCase):
    def result(self, exported: dict, artifact_dir: str | None = None) -> PdfExtractionResult:
        return PdfExtractionResult(
            source_path=Path("vehicle.pdf"), conversion_status="success",
            exported=exported,
            processing_configuration={"docling_version": "2.132.0", **({"artifact_dir": artifact_dir} if artifact_dir else {})},
        )

    def test_normalizes_pages_text_tables_and_visuals(self) -> None:
        raw = {
            "title": "Vehicle guide",
            "pages": [{"page_no": 1}],
            "texts": [
                {"label": "title", "text": "Brake system", "prov": [{"page_no": 1, "self_ref": "#/texts/0"}]},
                {"label": "paragraph", "text": "Use the specified torque.", "prov": [{"page_no": 1}]},
            ],
            "tables": [{"prov": [{"page_no": 1}], "data": {"grid": [["Part", "Value"], ["sensor", "10"]]}}],
            "pictures": [{"label": "diagram", "classification": "mounting", "prov": [{"page_no": 1}]}],
        }
        document = normalize_pdf(self.result(raw), document_id="doc-1", version_id="v-1", source_file_hash="A" * 64)
        self.assertEqual(document.document.sha256, "a" * 64)
        self.assertEqual(document.document.version_id, "v-1")
        self.assertEqual(document.elements["pdf-page-001"].child_ids,
                         ["pdf-page-001-heading-001", "pdf-page-001-paragraph-001",
                          "pdf-page-001-table-001", "pdf-page-001-diagram-001"])
        self.assertEqual(document.elements["pdf-page-001-table-001"].row_count, 2)
        self.assertEqual(document.elements["pdf-page-001-diagram-001"].type, ElementType.DIAGRAM)
        self.assertTrue(all(len(element.content_hash or "") == 64 for element in document.elements.values()))
        self.assertEqual(document.model_dump()["schema_version"], "0.2.0")

    def test_repeated_normalization_is_deterministic(self) -> None:
        raw = {"pages": [{"page_no": 1}], "texts": [{"label": "paragraph", "text": "same", "prov": [{"page_no": 1}]}]}
        first = normalize_pdf(self.result(raw), document_id="doc-1", version_id="v-1", source_file_hash="b" * 64)
        second = normalize_pdf(self.result(raw), document_id="doc-1", version_id="v-1", source_file_hash="b" * 64)
        self.assertEqual(first.model_dump(), second.model_dump())

    def test_saves_and_deduplicates_visual_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            raw = {"pages": [{"page_no": 1}, {"page_no": 2}], "pictures": [
                {"label": "image", "image": b"same", "prov": [{"page_no": 1}]},
                {"label": "image", "image": b"same", "prov": [{"page_no": 2}]},
            ]}
            document = normalize_pdf(self.result(raw, directory), document_id="doc-1", version_id="v-1", source_file_hash="c" * 64)
            self.assertEqual(len(document.assets), 1)
            asset = next(iter(document.assets.values()))
            self.assertTrue((Path(directory) / asset.relative_uri).is_file())
            self.assertEqual(len([e for e in document.elements.values() if e.type == ElementType.IMAGE]), 2)

    def test_saves_visual_assets_from_docling_base64_data_uri(self) -> None:
        # Docling nests picture bytes as image.uri, a base64 data URI, rather
        # than a flat bytes/path field.
        with tempfile.TemporaryDirectory() as directory:
            png_bytes = b"\x89PNG\r\n\x1a\nfake-png-bytes"
            data_uri = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
            raw = {"pages": [{"page_no": 1}], "pictures": [
                {"label": "image", "image": {"mimetype": "image/png", "dpi": 72,
                                              "size": {"width": 10, "height": 10}, "uri": data_uri},
                 "prov": [{"page_no": 1}]},
            ]}
            document = normalize_pdf(self.result(raw, directory), document_id="doc-1", version_id="v-1", source_file_hash="d" * 64)
            self.assertEqual(len(document.assets), 1)
            asset = next(iter(document.assets.values()))
            self.assertEqual(asset.mime_type, "image/png")
            saved_path = Path(directory) / asset.relative_uri
            self.assertTrue(saved_path.is_file())
            self.assertEqual(saved_path.read_bytes(), png_bytes)

    def test_rejects_invalid_input(self) -> None:
        with self.assertRaises(TypeError):
            normalize_pdf(object(), document_id="doc", version_id="v", source_file_hash="a" * 64)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            normalize_pdf(self.result({"pages": [{"page_no": 1}]}), document_id="doc", version_id="v", source_file_hash="not-a-hash")
        with self.assertRaises(PdfNormalizationError):
            normalize_pdf(self.result({}), document_id="doc", version_id="v", source_file_hash="a" * 64)


if __name__ == "__main__":
    unittest.main()
